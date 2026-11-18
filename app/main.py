import asyncio
from pathlib import Path

from blacksheep import Application, Content, Cookie, Request, Response
from blacksheep.server.responses import redirect
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    SCOPES,
    gap_state,
    get_batch,
    get_task,
    list_batches,
    list_reports,
    list_tasks,
    list_templates,
    replay_task,
    run_backfill,
    run_gap_report,
    stamp_batch,
    trace_downstream,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-watermark-session")

app = Application()
app.serve_files(str(STATIC), root_path="/static")


@app.on_start
async def _on_start(_app) -> None:
    init_db()


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("watermark_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(
        status,
        content=Content(
            b"application/json",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ),
    )


def html_response(path: Path) -> Response:
    return Response(
        200,
        content=Content(b"text/html; charset=utf-8", path.read_bytes()),
    )


@app.router.get("/")
async def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return redirect("/app/")
    return redirect("/login")


@app.router.get("/login")
async def login_page(request: Request):
    return html_response(STATIC / "login.html")


@app.router.get("/app/")
async def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return redirect("/login")
    return html_response(STATIC / "index.html")


@app.router.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_response({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_response({"ok": True, "user": user})
    resp.set_cookie(
        Cookie(
            "watermark_session",
            serializer.dumps({"user": user}),
            http_only=True,
            path="/",
        )
    )
    return resp


@app.router.post("/api/logout")
async def api_logout():
    resp = json_response({"ok": True})
    resp.unset_cookie("watermark_session")
    return resp


@app.router.get("/api/meta")
async def api_meta(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(
        {
            "lab_name": Config.LAB_NAME,
            "product_line": "cc-watermark-lab",
            "chain": ["水印模板", "批次盖印", "下游追索", "缺口对账"],
        }
    )


@app.router.get("/api/templates")
async def api_templates(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_templates(status=status_val)})


@app.router.get("/api/batches")
async def api_batches(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_batches(status=status_val)})


@app.router.get("/api/batches/{batch_id}")
async def api_batch_detail(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    batch_id = int(request.route_values["batch_id"])
    batch = get_batch(batch_id)
    if not batch:
        return json_response({"error": "批次不存在"}, 404)
    return json_response({"batch": batch})


@app.router.post("/api/batches/{batch_id}/stamp")
async def api_stamp(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
        body = await request.json()
        batch = stamp_batch(batch_id, str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "batch": batch})


@app.router.post("/api/trace")
async def api_trace(request: Request):
    try:
        require_user(request)
        body = await request.json()
        result = trace_downstream(str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, **result})


@app.router.get("/api/gap-reports")
async def api_gap_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_reports()})


@app.router.get("/api/gaps")
async def api_gaps(request: Request):
    """缺口明细：列表计数、行级明细与补盖写库同一取数口径。"""
    try:
        require_user(request)
        raw = request.query.get("as_of_date") or request.query.get("date")
        state = gap_state(raw[0] if raw else None)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"ok": True, **state})


@app.router.post("/api/gap-reports")
async def api_gap_run(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    body = await request.json()
    report = run_gap_report(str(body.get("as_of_date", "2026-11-16")))
    return json_response({"ok": True, "report": report})


@app.router.get("/api/backfills")
@app.router.get("/api/backfill")
@app.router.get("/api/backfill-tasks")
async def api_backfill_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_tasks(), "scopes": list(SCOPES)})


@app.router.post("/api/backfills")
@app.router.post("/api/backfill")
@app.router.post("/api/gaps/backfill")
@app.router.post("/api/gap-backfill")
async def api_backfill_run(request: Request):
    """点补盖：按选定范围一次落库；失败返回白话原因。"""
    try:
        require_user(request)
        body = await request.json()
        task_id = body.get("task_id")
        scope = body.get("scope")
        as_of = body.get("as_of_date") or body.get("date") or body.get("as_of")
        rounds = body.get("rounds", 2)
        if scope is None and body.get("mode") is not None:
            scope = body.get("mode")
        # 允许两种入口：带 task_id 回放既有任务，或直接给出日期与范围新建补盖。
        # 补盖是同步写库，放到线程里执行，避免 SQLite 等待阻塞事件循环。
        if task_id is not None and not scope:
            result = await asyncio.to_thread(replay_task, int(task_id), rounds)
        else:
            result = await asyncio.to_thread(run_backfill, as_of, scope)
    except ServiceError as exc:
        payload = {"ok": False, "error": exc.message}
        task_id = getattr(exc, "task_id", None)
        if task_id is not None:
            payload["task_id"] = task_id
        return json_response(payload, exc.status_code)
    except (TypeError, ValueError):
        return json_response({"ok": False, "error": "补盖参数无效。"}, 400)
    return json_response({"ok": True, **result})


@app.router.get("/api/backfills/{task_id}")
@app.router.get("/api/backfill/{task_id}")
@app.router.get("/api/backfill-tasks/{task_id}")
async def api_backfill_detail(request: Request):
    try:
        require_user(request)
        task_id = int(request.route_values["task_id"])
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    except (TypeError, ValueError):
        return json_response({"error": "补盖任务编号无效。"}, 400)
    detail = get_task(task_id)
    if not detail:
        return json_response({"ok": False, "error": "补盖任务不存在。"}, 404)
    return json_response({"ok": True, **detail})


@app.router.post("/api/backfills/{task_id}/replay")
@app.router.post("/api/backfill/{task_id}/replay")
@app.router.post("/api/backfill-tasks/{task_id}/replay")
async def api_backfill_replay(request: Request):
    try:
        require_user(request)
        task_id = int(request.route_values["task_id"])
        body = await request.json()
        detail = await asyncio.to_thread(
            replay_task, task_id, body.get("rounds", 2)
        )
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    except (TypeError, ValueError):
        return json_response({"ok": False, "error": "回放参数无效。"}, 400)
    return json_response({"ok": True, **detail})



