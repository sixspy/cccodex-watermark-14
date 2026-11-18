from app.db import get_db, get_write_db
from app.services.errors import ServiceError


def list_batches(db_path: str | None = None, status: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM run_batches WHERE status=? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_batches ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]


def get_batch(batch_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None


def stamp_batch(
    batch_id: int,
    watermark_value: str,
    db_path: str | None = None,
) -> dict:
    value = (watermark_value or "").strip()
    if not value:
        raise ServiceError("水印值不能为空")
    # 与补盖共用排他写事务：补盖进行中时单批盖印会等待写锁，
    # 不会与补盖各自读到旧状态后重复写同一行。
    with get_write_db(db_path) as conn:
        batch = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ServiceError("批次不存在", 404)
        if batch["status"] == "已盖印":
            raise ServiceError("批次已盖印", 409)
        if batch["status"] != "待盖印":
            raise ServiceError("仅待盖印批次可盖印", 409)
        tpl = conn.execute(
            "SELECT * FROM watermark_templates WHERE table_name=? AND status='现行'",
            (batch["table_name"],),
        ).fetchone()
        if not tpl:
            raise ServiceError("无现行水印模板", 409)
        cur = conn.execute(
            "UPDATE run_batches SET status='已盖印', watermark_value=?, "
            "stamped_at=datetime('now') WHERE id=? AND status='待盖印'",
            (value, batch_id),
        )
        if cur.rowcount != 1:
            raise ServiceError("批次状态已变化，请刷新后重试", 409)
        row = conn.execute(
            "SELECT * FROM run_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row)
