from app.db import get_db

DEFAULT_AS_OF = "2026-11-16"

# 应盖 / 已盖 / 缺口共用同一取数口径：先取出“应盖印批次”（未作废），
# 再在其中按状态拆分。补盖写库时复用这个口径，避免“列表说缺口 2 行、
# 实际却按别的范围写库”的同源性问题。
EXPECTED_WHERE = "biz_date<=? AND status!='已作废'"
_EXPECTED_SQL = f"SELECT * FROM run_batches WHERE {EXPECTED_WHERE} ORDER BY id"


def normalize_day(as_of_date: str | None) -> str:
    return (as_of_date or "").strip() or DEFAULT_AS_OF


def select_state(conn, as_of_date: str | None):
    """返回 (日期, 应盖行, 已盖行, 缺口行)。缺口行 = 应盖行中仍为待盖印的行。"""
    day = normalize_day(as_of_date)
    expected = conn.execute(_EXPECTED_SQL, (day,)).fetchall()
    stamped = [r for r in expected if r["status"] == "已盖印"]
    gaps = [r for r in expected if r["status"] == "待盖印"]
    return day, expected, stamped, gaps


def _state_payload(day, expected, stamped, gaps) -> dict:
    return {
        "as_of_date": day,
        "expected_count": len(expected),
        "stamped_count": len(stamped),
        "gap_count": len(gaps),
        "gap_rows": [dict(r) for r in gaps],
        "rows": [
            {**dict(r), "is_gap": r["status"] == "待盖印"} for r in expected
        ],
        "summary": (
            f"截至 {day}：应盖印 {len(expected)}，已盖印 {len(stamped)}，"
            f"缺口 {len(gaps)}"
        ),
    }


def state_in_conn(conn, as_of_date: str | None) -> dict:
    """在调用方事务内取缺口快照（补盖开事务时与写库同源）。"""
    return _state_payload(*select_state(conn, as_of_date))


def gap_state(as_of_date: str | None, db_path: str | None = None) -> dict:
    """缺口列表：行级明细与计数同源。"""
    with get_db(db_path) as conn:
        return state_in_conn(conn, as_of_date)


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gap_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def run_gap_report(as_of_date: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        state = state_in_conn(conn, as_of_date)
        cur = conn.execute(
            "INSERT INTO gap_reports(as_of_date, expected_count, stamped_count, gap_count, summary) "
            "VALUES (?,?,?,?,?)",
            (
                state["as_of_date"],
                state["expected_count"],
                state["stamped_count"],
                state["gap_count"],
                state["summary"],
            ),
        )
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
