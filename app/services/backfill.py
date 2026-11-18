"""缺口补盖引擎。

两种补盖范围只能二选一，且写库与缺口列表共用同一取数口径：

- ``仅缺口``：只给仍为「待盖印」的缺口行写新印，已盖印行原样保留；
- ``整批重落``：先把本批应盖印行整笔回滚成待盖印，再全量重写水印。

读取缺口、写入补盖、复核结果都在同一个 ``BEGIN IMMEDIATE`` 排他事务里完成。
补盖前另取一行全局任务锁，两名仓管同时点补盖时后到者会收到白话提示，不会
各自读到同一批缺口再把同一行写两遍。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

from app.db import connect, get_db, get_write_db
from app.services.errors import ServiceError
from app.services.gaps import (
    EXPECTED_WHERE,
    normalize_day,
    select_state,
    state_in_conn,
)

SCOPE_GAP = "仅缺口"
SCOPE_ALL = "整批重落"
SCOPES = (SCOPE_GAP, SCOPE_ALL)

SCOPE_ALIASES = {
    "仅缺口": SCOPE_GAP,
    "缺口": SCOPE_GAP,
    "补缺口": SCOPE_GAP,
    "只补缺口": SCOPE_GAP,
    "仅补缺口": SCOPE_GAP,
    "部分": SCOPE_GAP,
    "部分提交": SCOPE_GAP,
    "增量": SCOPE_GAP,
    "gap": SCOPE_GAP,
    "gaps": SCOPE_GAP,
    "only_gap": SCOPE_GAP,
    "only-gap": SCOPE_GAP,
    "onlygap": SCOPE_GAP,
    "partial": SCOPE_GAP,
    "patched": SCOPE_GAP,
    "incremental": SCOPE_GAP,
    "整批重落": SCOPE_ALL,
    "整批": SCOPE_ALL,
    "整批重盖": SCOPE_ALL,
    "整批回滚": SCOPE_ALL,
    "回滚重落": SCOPE_ALL,
    "回滚整批": SCOPE_ALL,
    "全量": SCOPE_ALL,
    "全量重落": SCOPE_ALL,
    "全量重盖": SCOPE_ALL,
    "all": SCOPE_ALL,
    "full": SCOPE_ALL,
    "full_all": SCOPE_ALL,
    "rebuild": SCOPE_ALL,
    "full_rebuild": SCOPE_ALL,
    "full-rebuild": SCOPE_ALL,
    "rollback": SCOPE_ALL,
    "reset": SCOPE_ALL,
}

LOCK_KEY = "backfill"
LOCK_TTL_SECONDS = 300
DEFAULT_REPLAY_ROUNDS = 2


def normalize_scope(scope: str | None) -> str:
    raw = (scope or "").strip()
    if not raw:
        # 默认走安全侧：只补缺口，绝不碰已盖印行。
        return SCOPE_GAP
    key = raw.lower()
    resolved = SCOPE_ALIASES.get(raw) or SCOPE_ALIASES.get(key)
    if not resolved:
        raise ServiceError(
            "补盖范围无效：只能选择“仅缺口”或“整批重落”。", 400
        )
    return resolved


def scope_chip(scope: str) -> str:
    return f"范围：{scope}"


def _claim_lock(owner: str, db_path: str | None = None) -> None:
    """原子抢全局补盖锁；同一时刻只允许一个补盖任务。

    这里用 0 等待的短连接：另一名仓管正在持有排他写事务时立刻失败并提示，
    不会排在锁后面等待，也就不会出现两人先后各补一遍、把同一行写两次。
    """
    now = int(time.time())
    stale_before = now - LOCK_TTL_SECONDS
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 0")
        try:
            cur = conn.execute(
                "INSERT INTO backfill_locks(lock_key, owner, acquired_epoch) "
                "VALUES (?,?,?) "
                "ON CONFLICT(lock_key) DO UPDATE SET "
                "owner=excluded.owner, acquired_epoch=excluded.acquired_epoch "
                "WHERE backfill_locks.acquired_epoch <= ?",
                (LOCK_KEY, owner, now, stale_before),
            )
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise ServiceError("另一名仓管正在补盖，请稍后再试。", 409) from exc
            raise
        if cur.rowcount != 1:
            raise ServiceError("另一名仓管正在补盖，请稍后再试。", 409)
        conn.commit()
    finally:
        conn.close()


def _release_lock(owner: str, db_path: str | None = None) -> None:
    try:
        with get_db(db_path) as conn:
            conn.execute(
                "DELETE FROM backfill_locks WHERE lock_key=? AND owner=?",
                (LOCK_KEY, owner),
            )
    except sqlite3.Error:
        # 释放锁失败不应覆盖补盖结果；锁会在 TTL 后失效。
        pass


def _create_task(day: str, scope: str, db_path: str | None = None) -> int:
    with get_db(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO backfill_tasks(as_of_date, scope, status, message) "
            "VALUES (?,?,?,?)",
            (day, scope, "执行中", "补盖执行中"),
        )
        return int(cur.lastrowid)


def _fail_task(task_id: int, message: str, db_path: str | None = None) -> None:
    try:
        with get_db(db_path) as conn:
            conn.execute(
                "UPDATE backfill_tasks SET status='失败', message=?, "
                "updated_at=datetime('now') WHERE id=?",
                (message, task_id),
            )
    except sqlite3.Error:
        pass


def _watermark_for(
    row: sqlite3.Row, scope: str, task_id: int, round_no: int
) -> str:
    """补盖水印带任务/轮次标识，保证「整批重落」确实把已盖印行换成新印。

    仅缺口只补缺口行，水印按任务编号生成，便于与已有印区分；
    整批重落每轮再带轮次，回放时每轮都是真实替换而不是同名重写。
    """
    base = f"WM-{(row['run_code'] or '').upper()}"
    if scope == SCOPE_ALL:
        return f"{base}-RL{task_id}.{round_no}"
    return f"{base}-G{task_id}"


def _empty_stats(expected: int, stamped: int, gap: int, total: int) -> dict:
    return {
        "expected_count": expected,
        "stamped_before": stamped,
        "gap_before": gap,
        "new_count": 0,
        "preserved_count": 0,
        "replaced_count": 0,
        "skipped_count": 0,
        "total_count": total,
    }


def _fate_detail(before_rows, after_by_id, scope: str) -> list[dict]:
    items = []
    for before in before_rows:
        row = dict(before)
        after = after_by_id.get(row["id"], {})
        before_status = row["status"]
        before_wm = row["watermark_value"]
        after_status = after.get("status", before_status)
        after_wm = after.get("watermark_value", before_wm)
        if scope == SCOPE_GAP and before_status == "已盖印":
            fate = "保留"
        elif scope == SCOPE_ALL and before_status == "已盖印":
            fate = "替换"
        elif before_status == "待盖印" and after_status == "已盖印":
            fate = "新盖"
        else:
            fate = "跳过"
        items.append(
            {
                "batch_id": row["id"],
                "run_code": row["run_code"],
                "table_name": row["table_name"],
                "biz_date": row["biz_date"],
                "before_status": before_status,
                "before_watermark": before_wm,
                "after_status": after_status,
                "after_watermark": after_wm,
                "replaced": after_wm != before_wm,
                "fate": fate,
            }
        )
    return items


def _is_aligned(scope: str, stats: dict) -> bool:
    total = stats["total_count"]
    if scope == SCOPE_GAP:
        return (
            stats["new_count"]
            + stats["preserved_count"]
            + stats["skipped_count"]
            == total
            and stats["replaced_count"] == 0
        )
    return (
        stats["new_count"]
        + stats["replaced_count"]
        + stats["skipped_count"]
        == total
        and stats["preserved_count"] == 0
    )


def _apply_round(
    conn, day: str, scope: str, task_id: int, round_no: int
) -> tuple[dict, list[dict]]:
    """在调用方的排他事务内按范围写库并自检。

    自检不通过会直接抛错，调用方事务回滚，已盖印行不会被留在被替换状态。
    """
    day, expected, stamped, gaps = select_state(conn, day)
    total = len(expected)
    if not expected:
        raise ServiceError(f"截至 {day} 没有应盖印批次，补盖未执行。", 409)

    targets = gaps if scope == SCOPE_GAP else expected
    tables = sorted({r["table_name"] for r in targets})
    if tables:
        placeholders = ",".join("?" for _ in tables)
        active = {
            r["table_name"]
            for r in conn.execute(
                f"SELECT table_name FROM watermark_templates "
                f"WHERE status='现行' AND table_name IN ({placeholders})",
                tables,
            ).fetchall()
        }
        missing = [t for t in tables if t not in active]
        if missing:
            raise ServiceError(
                f"补盖失败：表 {'、'.join(missing)} 无现行水印模板，"
                "本次未写入任何行，请先恢复模板。",
                409,
            )

    before_rows = [dict(r) for r in expected]
    before_by_id = {r["id"]: r for r in before_rows}

    if scope == SCOPE_ALL:
        # 整笔回滚整批印：已盖印行也清空，随后全量重落。回滚范围与缺口列表
        # 使用同一条 EXPECTED_WHERE，保证“看见的缺口”与“回滚的行”同一批。
        conn.execute(
            "UPDATE run_batches SET status='待盖印', watermark_value=NULL, stamped_at=NULL "
            f"WHERE {EXPECTED_WHERE}",
            (day,),
        )

    for row in targets:
        value = _watermark_for(row, scope, task_id, round_no)
        cur = conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE id=? AND status='待盖印'",
            (value, row["id"]),
        )
        if cur.rowcount != 1:
            raise ServiceError(
                f"补盖失败：缺口行 {row['run_code']} 已被改动（可能他人已补盖），"
                "本次已整笔回滚，请刷新缺口后重试。",
                409,
            )

    after_rows = conn.execute(
        f"SELECT * FROM run_batches WHERE {EXPECTED_WHERE} ORDER BY id",
        (day,),
    ).fetchall()
    after_by_id = {r["id"]: dict(r) for r in after_rows}

    # 自检一：应盖印集合不能变，且所有行最终都应是已盖印。
    if set(after_by_id) != set(before_by_id):
        raise ServiceError("补盖失败：应盖印批次集合发生变化，本次已整笔回滚。", 409)
    not_stamped = [
        after_by_id[i]["run_code"]
        for i in before_by_id
        if after_by_id[i]["status"] != "已盖印"
    ]
    if not_stamped:
        raise ServiceError(
            f"补盖失败：{ '、'.join(not_stamped) } 未完成盖印，本次已整笔回滚。",
            409,
        )

    if scope == SCOPE_GAP:
        # 自检二：已盖印行的水印与盖印时间必须原样保留。
        touched = [
            rid
            for rid, before in before_by_id.items()
            if before["status"] == "已盖印"
            and (
                after_by_id[rid]["watermark_value"] != before["watermark_value"]
                or after_by_id[rid]["stamped_at"] != before["stamped_at"]
                or after_by_id[rid]["status"] != "已盖印"
            )
        ]
        if touched:
            codes = "、".join(before_by_id[rid]["run_code"] for rid in touched)
            raise ServiceError(
                f"补盖失败：已盖印行被误替换（{codes}），本次已整笔回滚。",
                409,
            )

    stats = _empty_stats(len(expected), len(stamped), len(gaps), total)
    detail = _fate_detail(expected, after_by_id, scope)
    for item in detail:
        if item["fate"] == "新盖":
            stats["new_count"] += 1
        elif item["fate"] == "保留":
            stats["preserved_count"] += 1
        elif item["fate"] == "替换":
            stats["replaced_count"] += 1
        else:
            stats["skipped_count"] += 1

    if not _is_aligned(scope, stats):
        raise ServiceError(
            "补盖失败：写库范围与缺口口径对不上，本次已整笔回滚。", 409
        )

    # 自检三：补盖后的缺口必须与实印同源，即按同一口径重算缺口应为 0。
    after_state = state_in_conn(conn, day)
    if after_state["gap_count"] != 0:
        raise ServiceError(
            f"补盖失败：仍有 {after_state['gap_count']} 行缺口未落印，"
            "本次已整笔回滚。",
            409,
        )

    return stats, detail


def _insert_round(
    conn,
    task_id: int,
    round_no: int,
    scope: str,
    status: str,
    stats: dict,
    message: str,
    detail: list[dict],
) -> None:
    conn.execute(
        "INSERT INTO backfill_rounds("
        "task_id, round_no, status, expected_count, stamped_before, gap_before, "
        "new_count, preserved_count, replaced_count, skipped_count, total_count, "
        "message, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            round_no,
            status,
            stats["expected_count"],
            stats["stamped_before"],
            stats["gap_before"],
            stats["new_count"],
            stats["preserved_count"],
            stats["replaced_count"],
            stats["skipped_count"],
            stats["total_count"],
            message,
            json.dumps(detail, ensure_ascii=False),
        ),
    )


def _success_message(scope: str, stats: dict) -> str:
    if scope == SCOPE_GAP:
        if stats["new_count"] == 0 and stats["total_count"]:
            return (
                f"补盖完成：没有缺口可补，已盖印 {stats['preserved_count']} 行保持原样。"
            )
        return (
            f"补盖完成：仅缺口 {stats['new_count']} 行写入新印，"
            f"已盖印 {stats['preserved_count']} 行保持原样。"
        )
    return (
        f"补盖完成：整批回滚后全量重落 {stats['total_count']} 行，"
        f"其中替换已盖印 {stats['replaced_count']} 行、新盖 {stats['new_count']} 行。"
    )


def _round_payload(row: sqlite3.Row, task_scope: str) -> dict:
    item = dict(row)
    try:
        detail = json.loads(item.get("detail") or "[]")
    except (TypeError, ValueError):
        detail = []
    stats = {
        "expected_count": item["expected_count"],
        "stamped_before": item["stamped_before"],
        "gap_before": item["gap_before"],
        "new_count": item["new_count"],
        "preserved_count": item["preserved_count"],
        "replaced_count": item["replaced_count"],
        "skipped_count": item["skipped_count"],
        "total_count": item["total_count"],
    }
    item["detail"] = detail
    item["scope"] = task_scope
    item["scope_chip"] = scope_chip(task_scope)
    item["fate_chips"] = [
        f"新盖 {stats['new_count']}",
        f"保留 {stats['preserved_count']}",
        f"替换 {stats['replaced_count']}",
        f"跳过 {stats['skipped_count']}",
    ]
    item["aligned"] = item["status"] == "成功" and _is_aligned(task_scope, stats)
    return item


def get_task(task_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        task = conn.execute(
            "SELECT * FROM backfill_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not task:
            return None
        rounds = conn.execute(
            "SELECT * FROM backfill_rounds WHERE task_id=? ORDER BY round_no",
            (task_id,),
        ).fetchall()
        task_item = dict(task)
        try:
            task_item["detail"] = json.loads(task_item.get("detail") or "[]")
        except (TypeError, ValueError):
            task_item["detail"] = []
        task_item["scope_chip"] = scope_chip(task_item["scope"])
        round_items = [_round_payload(r, task_item["scope"]) for r in rounds]
        succeeded = bool(round_items) and task_item["status"] == "成功"
        aligned = succeeded and all(r["aligned"] for r in round_items)
        return {
            "task": task_item,
            "rounds": round_items,
            "scope_chip": task_item["scope_chip"],
            "aligned": aligned,
            "replay_count": task_item.get("replay_count", 0),
        }


def list_tasks(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM backfill_tasks ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def run_backfill(
    as_of_date: str | None,
    scope: str | None,
    db_path: str | None = None,
) -> dict:
    scope_val = normalize_scope(scope)
    day = normalize_day(as_of_date)
    owner = uuid.uuid4().hex
    _claim_lock(owner, db_path)
    task_id = None
    try:
        task_id = _create_task(day, scope_val, db_path)
        _apply_with_round(task_id, day, scope_val, 1, db_path)
        return get_task(task_id, db_path)
    except ServiceError as exc:
        if task_id is not None:
            _fail_task(task_id, exc.message, db_path)
            _record_failed_round(task_id, 1, exc.message, db_path)
            exc.task_id = task_id
        raise
    except sqlite3.Error as exc:
        message = f"补盖失败：本机库写入异常（{exc}），本次未写入任何行。"
        if task_id is not None:
            _fail_task(task_id, message, db_path)
        err = ServiceError(message, 500)
        err.task_id = task_id
        raise err from exc
    finally:
        _release_lock(owner, db_path)


def _apply_with_round(
    task_id: int,
    day: str,
    scope: str,
    round_no: int,
    db_path: str | None = None,
) -> tuple[dict, list[dict]]:
    with get_write_db(db_path) as conn:
        stats, detail = _apply_round(conn, day, scope, task_id, round_no)
        message = _success_message(scope, stats)
        _insert_round(
            conn, task_id, round_no, scope, "成功", stats, message, detail
        )
        if round_no == 1:
            conn.execute(
                "UPDATE backfill_tasks SET status='成功', expected_count=?, "
                "stamped_before=?, gap_before=?, new_count=?, preserved_count=?, "
                "replaced_count=?, skipped_count=?, total_count=?, message=?, "
                "detail=?, updated_at=datetime('now') WHERE id=?",
                (
                    stats["expected_count"],
                    stats["stamped_before"],
                    stats["gap_before"],
                    stats["new_count"],
                    stats["preserved_count"],
                    stats["replaced_count"],
                    stats["skipped_count"],
                    stats["total_count"],
                    message,
                    json.dumps(detail, ensure_ascii=False),
                    task_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE backfill_tasks SET status='成功', "
                "replay_count=replay_count+1, message=?, updated_at=datetime('now') "
                "WHERE id=?",
                (message, task_id),
            )
    return stats, detail


def replay_task(
    task_id: int,
    rounds: int = DEFAULT_REPLAY_ROUNDS,
    db_path: str | None = None,
) -> dict:
    """按原任务范围回放补盖，逐轮落库；两轮数字与范围芯片可对齐核对。"""
    try:
        rounds = int(rounds)
    except (TypeError, ValueError):
        raise ServiceError("回放轮数必须是 1 到 5 的整数。", 400)
    if rounds < 1 or rounds > 5:
        raise ServiceError("回放轮数必须是 1 到 5 的整数。", 400)

    task = get_task(task_id, db_path)
    if not task:
        raise ServiceError("补盖任务不存在。", 404)
    if task["task"]["status"] == "执行中":
        raise ServiceError("该补盖任务仍在执行，暂不能回放。", 409)

    day = task["task"]["as_of_date"]
    scope = task["task"]["scope"]
    owner = uuid.uuid4().hex
    _claim_lock(owner, db_path)
    next_no = _next_round_no(task_id, db_path)
    failed_no = next_no
    try:
        for offset in range(rounds):
            failed_no = next_no + offset
            _apply_with_round(task_id, day, scope, failed_no, db_path)
        return get_task(task_id, db_path)
    except ServiceError as exc:
        # 回放失败只记一条失败轮次，保留原任务的成功结果与数字。
        _record_failed_round(task_id, failed_no, exc.message, db_path)
        raise
    except sqlite3.Error as exc:
        message = f"回放失败：本机库写入异常（{exc}），本次未写入任何行。"
        _record_failed_round(task_id, failed_no, message, db_path)
        raise ServiceError(message, 500) from exc
    finally:
        _release_lock(owner, db_path)


def _next_round_no(task_id: int, db_path: str | None = None) -> int:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(round_no) AS n FROM backfill_rounds WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return int(row["n"] or 0) + 1


def _record_failed_round(
    task_id: int, round_no: int, message: str, db_path: str | None = None
) -> None:
    try:
        with get_db(db_path) as conn:
            task = conn.execute(
                "SELECT * FROM backfill_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if not task:
                return
            stats = _empty_stats(
                task["expected_count"],
                task["stamped_before"],
                task["gap_before"],
                task["total_count"],
            )
            _insert_round(
                conn, task_id, round_no, task["scope"], "失败", stats, message, []
            )
            conn.execute(
                "UPDATE backfill_tasks SET message=?, updated_at=datetime('now') "
                "WHERE id=?",
                (message, task_id),
            )
    except sqlite3.Error:
        pass
