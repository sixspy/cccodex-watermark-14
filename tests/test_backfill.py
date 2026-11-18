import threading

import pytest

from app.db import connect
from app.services import (
    gap_state,
    get_task,
    replay_task,
    run_backfill,
)
from app.services.errors import ServiceError


def _row(db, run_code):
    conn = connect(db)
    try:
        return dict(
            conn.execute(
                "SELECT * FROM run_batches WHERE run_code=?", (run_code,)
            ).fetchone()
        )
    finally:
        conn.close()


@pytest.fixture()
def db(tmp_path):
    from app.db import init_db

    path = str(tmp_path / "backfill.db")
    init_db(path)
    return path


def test_gap_scope_only_fills_gaps_and_keeps_stamped(db):
    before = _row(db, "ord-20261113-01")
    assert before["status"] == "已盖印"
    state = gap_state("2026-11-16", db_path=db)
    assert state["gap_count"] == len(state["gap_rows"]) == 3

    task = run_backfill("2026-11-16", "仅缺口", db_path=db)

    after = _row(db, "ord-20261113-01")
    assert after["status"] == "已盖印"
    assert after["watermark_value"] == before["watermark_value"]
    assert after["stamped_at"] == before["stamped_at"]

    gap_rows = {r["run_code"] for r in state["gap_rows"]}
    for code in gap_rows:
        assert _row(db, code)["status"] == "已盖印"

    assert task["task"]["status"] == "成功"
    assert task["task"]["scope"] == "仅缺口"
    assert task["scope_chip"] == "范围：仅缺口"
    assert task["task"]["new_count"] == 3
    assert task["task"]["preserved_count"] == 1
    assert task["task"]["replaced_count"] == 0
    assert task["aligned"] is True

    assert gap_state("2026-11-16", db_path=db)["gap_count"] == 0
    # 缺口清零后，已盖印计数与应盖印计数必须同源对齐。
    fresh = gap_state("2026-11-16", db_path=db)
    assert fresh["stamped_count"] == fresh["expected_count"]


def test_full_scope_replaces_stamped_watermark(db):
    before = _row(db, "ord-20261113-01")
    task = run_backfill("2026-11-16", "整批重落", db_path=db)
    after = _row(db, "ord-20261113-01")
    assert after["watermark_value"] != before["watermark_value"]
    assert task["task"]["replaced_count"] == 1
    assert task["task"]["preserved_count"] == 0
    assert task["task"]["total_count"] == 4
    assert gap_state("2026-11-16", db_path=db)["gap_count"] == 0


def test_scope_must_be_explicit_value(db):
    with pytest.raises(ServiceError) as exc:
        run_backfill("2026-11-16", "随便补补", db_path=db)
    assert "范围" in exc.value.message
    # 范围无效时不得写入任何行。
    assert _row(db, "ord-20261114-01")["status"] == "待盖印"


def test_failure_rolls_back_everything(db, monkeypatch):
    from app.services import backfill as engine

    # 让自检在写库之后失败，验证整笔回滚。
    monkeypatch.setattr(engine, "_is_aligned", lambda scope, stats: False)
    with pytest.raises(ServiceError) as exc:
        run_backfill("2026-11-16", "仅缺口", db_path=db)
    assert "失败" in exc.value.message
    assert _row(db, "ord-20261114-01")["status"] == "待盖印"
    assert _row(db, "ord-20261113-01")["watermark_value"] == "WM-ORD-131"
    tasks = get_task(1, db_path=db)
    assert tasks["task"]["status"] == "失败"
    assert tasks["task"]["message"]


def test_replay_two_rounds_chips_align(db):
    task = run_backfill("2026-11-16", "仅缺口", db_path=db)
    task_id = task["task"]["id"]
    replayed = replay_task(task_id, 2, db_path=db)
    assert replayed["task"]["replay_count"] == 2
    assert len(replayed["rounds"]) == 3
    for rnd in replayed["rounds"]:
        total = rnd["total_count"]
        assert (
            rnd["new_count"]
            + rnd["preserved_count"]
            + rnd["replaced_count"]
            + rnd["skipped_count"]
            == total
        )
        assert rnd["aligned"] is True
        assert rnd["scope_chip"] == "范围：仅缺口"
        assert rnd["fate_chips"]
    # 仅缺口回放不得让已盖印行被替换。
    assert all(r["replaced_count"] == 0 for r in replayed["rounds"])


def test_concurrent_backfill_is_exclusive(db, monkeypatch):
    from app.services import backfill as engine

    results = []
    started = threading.Event()

    real_apply = engine._apply_round

    def slow_apply(conn, day, scope, task_id, round_no):
        started.set()
        # 让第一名仓管在排他事务内停留，确保第二名在其持有锁期间发起补盖。
        threading.Event().wait(0.4)
        return real_apply(conn, day, scope, task_id, round_no)

    monkeypatch.setattr(engine, "_apply_round", slow_apply)

    def worker():
        try:
            run_backfill("2026-11-16", "仅缺口", db_path=db)
            results.append("ok")
        except ServiceError as exc:
            results.append(("err", exc.status_code, exc.message))

    first = threading.Thread(target=worker)
    first.start()
    assert started.wait(5)

    second = threading.Thread(target=worker)
    second.start()
    second.join(20)
    first.join(20)

    ok = [r for r in results if r == "ok"]
    err = [r for r in results if isinstance(r, tuple)]
    assert len(ok) == 1
    assert len(err) == 1
    assert err[0][1] == 409
    assert "补盖" in err[0][2]

    conn = connect(db)
    try:
        tasks = conn.execute("SELECT COUNT(*) AS c FROM backfill_tasks").fetchone()["c"]
        locks = conn.execute("SELECT COUNT(*) AS c FROM backfill_locks").fetchone()["c"]
    finally:
        conn.close()
    # 只有抢到排他锁的那一名仓管创建了任务，且锁已释放。
    assert tasks == 1
    assert locks == 0


@pytest.mark.asyncio
async def test_backfill_api_round_trip(authed):
    gaps = await (await authed.get("/api/gaps")).json()
    assert gaps["ok"] is True
    assert gaps["gap_count"] == len(gaps["gap_rows"]) == 3

    resp = await authed.post(
        "/api/backfills", json={"as_of_date": "2026-11-16", "scope": "仅缺口"}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["task"]["status"] == "成功"
    assert body["scope_chip"] == "范围：仅缺口"

    task_id = body["task"]["id"]
    detail = await (await authed.get(f"/api/backfills/{task_id}")).json()
    assert detail["rounds"][0]["scope_chip"] == "范围：仅缺口"
    assert detail["rounds"][0]["fate_chips"]

    replay = await authed.post(
        f"/api/backfills/{task_id}/replay", json={"rounds": 2}
    )
    assert replay.status == 200
    rbody = await replay.json()
    assert len(rbody["rounds"]) == 3
    assert rbody["aligned"] is True

    gaps_after = await (await authed.get("/api/gaps")).json()
    assert gaps_after["gap_count"] == 0
    assert gaps_after["stamped_count"] == gaps_after["expected_count"]


@pytest.mark.asyncio
async def test_backfill_api_bad_scope_plain_message(authed):
    resp = await authed.post(
        "/api/backfills", json={"as_of_date": "2026-11-16", "scope": "全都要"}
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["ok"] is False
    assert "仅缺口" in body["error"] and "整批重落" in body["error"]
