import base64
import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from playwright.async_api import async_playwright
from sqlalchemy import desc
from sqlalchemy.orm import Session

from config import settings
from database import (
    AppConfig,
    Content,
    Keyword,
    PushLog,
    RiskEvent,
    ScrapeTask,
    ScheduleConfig,
    ScheduleSlot,
    SessionLocal,
    get_db,
    init_db,
)
from models import (
    CollectRequest,
    ContentResponse,
    KeywordCreate,
    KeywordResponse,
    DashboardBootstrapResponse,
    FeedItemResponse,
    HistoryListResponse,
    HistorySummary,
    RiskEventResponse,
    RiskArtifactTokenResponse,
    RiskStatsResponse,
    RuntimeStatusResponse,
    ScheduleMultiResponse,
    ScheduleMultiUpdate,
    ScheduleSlotItem,
    PushLogResponse,
    ScrapeTaskResponse,
    ScrapeTriggerRequest,
    ScrapeTriggerResponse,
    SystemConfigResponse,
    SystemConfigUpdate,
)
from scheduler import (
    configure_daily_scrapes,
    enqueue_scrape_task,
    ensure_task_runner_started,
    get_login_block_state,
    get_task_runner_state,
    get_scrape_task,
    list_scrape_tasks,
    set_login_blocked,
    setup_scheduler,
)
from push.feishu import send_ops_text as push_send_ops_text
from security_tokens import generate_risk_artifact_token, verify_risk_artifact_token
from scrapers.registry import supported_platforms
from scrapers.xiaohongshu import XiaohongshuScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="选题助手 API", version="0.1.0")
_scheduler = None
_recent_feishu_event_ids: set[str] = set()
_recent_feishu_signatures: dict[str, int] = {}
_runtime_probe_task: Optional[asyncio.Task] = None
_runtime_probe_state: dict[str, Any] = {
    "local_health_code": 0,
    "public_health_code": 0,
    "tunnel_last_reachable_at": "",
    "last_checked_at": "",
}
_cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
_dashboard_file = Path(__file__).resolve().parent / "static" / "dashboard.html"
_feed_file = Path(__file__).resolve().parent / "static" / "feed.html"
_supported_platforms = supported_platforms()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ERROR_HINTS = {
    "AUTH_INVALID": "请检查 API Key 是否正确，并确认前端已同步最新配置。",
    "BAD_REQUEST": "请检查请求参数是否完整且格式正确。",
    "NOT_FOUND": "目标资源不存在，可能已删除或 ID 错误。",
    "SCRAPE_EXEC_FAILED": "抓取执行失败，请查看任务详情与后端日志。",
    "LOGIN_BLOCKED": "检测到登录风控，系统已暂停抓取。请扫码登录后在飞书发送“已登录”恢复。",
    "FEISHU_SIGNATURE_INVALID": "飞书签名校验失败，请检查回调配置和密钥。",
    "SYSTEM_ERROR": "系统内部异常，请稍后重试并提供 request_id 排查。",
}


def _error_payload(code: str, message: str, request_id: str = "") -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "hint": ERROR_HINTS.get(code, ""),
        "request_id": request_id or "",
    }


def _request_id_from_request(request: Optional[Request]) -> str:
    if request is None:
        return ""
    return str(getattr(request.state, "request_id", "") or "")


def raise_api_error(status_code: int, code: str, message: str, request: Optional[Request] = None) -> None:
    raise HTTPException(
        status_code=status_code,
        detail=_error_payload(code=code, message=message, request_id=_request_id_from_request(request)),
    )


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request_id = uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        payload = dict(exc.detail)
        if not payload.get("request_id"):
            payload["request_id"] = _request_id_from_request(request)
        return JSONResponse(status_code=exc.status_code, content=payload)

    if exc.status_code == 401:
        payload = _error_payload("AUTH_INVALID", str(exc.detail or "Unauthorized"), _request_id_from_request(request))
    elif exc.status_code == 404:
        payload = _error_payload("NOT_FOUND", str(exc.detail or "Not found"), _request_id_from_request(request))
    else:
        payload = _error_payload("BAD_REQUEST", str(exc.detail or "Request error"), _request_id_from_request(request))
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception):
    logging.exception("Unhandled exception: %s", exc)
    payload = _error_payload("SYSTEM_ERROR", "Internal server error", _request_id_from_request(request))
    return JSONResponse(status_code=500, content=payload)


@app.get("/api/health")
def health_check():
    return {"ok": True, "timestamp": datetime.utcnow().isoformat() + "Z"}


async def _runtime_probe_loop() -> None:
    while True:
        await _run_runtime_probe_once()
        await asyncio.sleep(30)


async def _run_runtime_probe_once() -> None:
    local_code = 0
    public_code = 0
    tunnel_last = _runtime_probe_state.get("tunnel_last_reachable_at", "")
    checked_at = datetime.utcnow().isoformat() + "Z"
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            local_resp = await client.get("http://127.0.0.1:8000/api/health")
            local_code = int(local_resp.status_code)
    except Exception:
        local_code = 0

    public_base = settings.public_base_url.strip().rstrip("/")
    if public_base:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                public_resp = await client.get(f"{public_base}/api/health")
                public_code = int(public_resp.status_code)
        except Exception:
            public_code = 0

    if public_code == 200:
        tunnel_last = checked_at
    _runtime_probe_state.update(
        {
            "local_health_code": local_code,
            "public_health_code": public_code,
            "tunnel_last_reachable_at": tunnel_last,
            "last_checked_at": checked_at,
        }
    )


@app.post("/feishu/event_callback")
@app.post("/feishu/event-callback")
async def feishu_event_callback(request: Request):
    """
    飞书事件回调入口（应用事件订阅）。
    - URL 验证：返回 challenge
    - 事件推送：先 ACK，后续再按事件类型扩展业务处理
    """
    raw_body = await request.body()
    if not _verify_feishu_callback_security(request, raw_body):
        raise_api_error(
            status_code=401,
            code="FEISHU_SIGNATURE_INVALID",
            message="Invalid feishu callback signature",
            request=request,
        )
    try:
        body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except Exception:
        body = {}

    if isinstance(body, dict) and body.get("type") == "url_verification":
        challenge = str(body.get("challenge") or "")
        logging.info("Feishu callback url_verification received")
        return {"challenge": challenge}

    header = body.get("header") if isinstance(body, dict) else {}
    event = body.get("event") if isinstance(body, dict) else {}
    event_type = ""
    if isinstance(header, dict):
        event_type = str(header.get("event_type") or "")
    logging.info(
        "Feishu callback received: event_type=%s has_event=%s",
        event_type or "-",
        bool(event),
    )
    if event_type:
        asyncio.create_task(_handle_feishu_event(event_type, event, header))
    return {"code": 0}


def _verify_feishu_callback_security(request: Request, raw_body: bytes) -> bool:
    secret = settings.feishu_event_signing_secret.strip()
    if not secret:
        return True

    ts = request.headers.get("X-Lark-Request-Timestamp", "").strip()
    nonce = request.headers.get("X-Lark-Request-Nonce", "").strip()
    signature = request.headers.get("X-Lark-Signature", "").strip()
    if not ts or not nonce or not signature:
        return False
    try:
        now = int(time.time())
        ts_int = int(ts)
        if abs(now - ts_int) > 600:
            return False
    except Exception:
        return False

    body_text = raw_body.decode("utf-8", errors="ignore")
    payload_candidates = [
        f"{ts}{nonce}{body_text}",
        f"{ts}{nonce}{secret}{body_text}",
    ]
    accepted_signatures: set[str] = set()
    for payload in payload_candidates:
        digest_hex = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        accepted_signatures.add(digest_hex)
        accepted_signatures.add("sha256=" + digest_hex)

    if not any(hmac.compare_digest(signature, candidate) for candidate in accepted_signatures):
        return False

    # 重放防护：同 timestamp+nonce 在窗口期仅接受一次
    now = int(time.time())
    expired_keys = [k for k, v in _recent_feishu_signatures.items() if now - v > 600]
    for k in expired_keys:
        _recent_feishu_signatures.pop(k, None)
    replay_key = f"{ts}:{nonce}"
    if replay_key in _recent_feishu_signatures:
        return False
    _recent_feishu_signatures[replay_key] = now
    return True


async def _handle_feishu_event(event_type: str, event: Any, header: Any) -> None:
    try:
        if not isinstance(event, dict):
            return
        if not isinstance(header, dict):
            header = {}
        event_id = str(header.get("event_id") or "").strip()
        if event_id:
            if event_id in _recent_feishu_event_ids:
                return
            _recent_feishu_event_ids.add(event_id)
            if len(_recent_feishu_event_ids) > 300:
                _recent_feishu_event_ids.clear()
                _recent_feishu_event_ids.add(event_id)

        if event_type != "im.message.receive_v1":
            return
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        chat_id = str(message.get("chat_id") or "")
        chat_type = str(message.get("chat_type") or "")
        _remember_feishu_chat_id(chat_id, chat_type)
        message_type = str(message.get("message_type") or "").lower()
        if message_type != "text":
            return

        raw_command = _extract_feishu_text_command(message.get("content"))
        command = _normalize_feishu_command(raw_command)
        if not command:
            return
        logging.info(
            "Feishu command parsed: raw=%s normalized=%s chat_id=%s chat_type=%s",
            raw_command,
            command,
            chat_id or "-",
            chat_type or "-",
        )

        if command in {"帮助", "help", "/help", "菜单", "命令"}:
            await _send_feishu_ops_text(
                "可用指令：\n"
                "1) 帮助\n"
                "2) 状态\n"
                "3) 立即抓取\n"
                "4) 已登录（扫码完成后发送，解除风控暂停并自动重试）\n"
                "示例：发送 `立即抓取` 后会触发一次全量关键词抓取。"
            )
            return

        if command in {"状态", "status", "/status"}:
            await _send_feishu_ops_text(_build_runtime_status_text())
            return

        if (
            command in {"已登录", "已登陆", "登录完成", "恢复抓取", "继续抓取", "resume", "/resume"}
            or "已登录" in command
            or "已登陆" in command
            or "登录完成" in command
        ):
            await _resume_after_login_from_feishu(raw_command or command)
            return

        if command in {"抓取", "立即抓取", "开始抓取", "run", "/run"}:
            await _trigger_scrape_from_feishu(raw_command or command)
            return
    except Exception:
        logging.exception("Handle feishu event failed")


def _extract_feishu_text_command(raw_content: Any) -> str:
    if isinstance(raw_content, dict):
        text = str(raw_content.get("text") or "")
        return text.strip()
    if not isinstance(raw_content, str):
        return ""
    content = raw_content.strip()
    if not content:
        return ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            text = str(parsed.get("text") or "")
            return text.strip()
    except Exception:
        pass
    return content


def _normalize_feishu_command(text: str) -> str:
    command = str(text or "").strip().lower()
    command = re.sub(r"\s+", "", command)
    command = re.sub(r"^@[^ ]+", "", command).strip()
    return command


def _build_runtime_status_text() -> str:
    db = SessionLocal()
    try:
        enabled_keywords = db.query(Keyword).filter(Keyword.enabled == True).count()
        latest_push = db.query(PushLog).order_by(desc(PushLog.pushed_at)).first()
        queued = db.query(ScrapeTask).filter(ScrapeTask.status == "queued").count()
        running_row = (
            db.query(ScrapeTask)
            .filter(ScrapeTask.status == "running")
            .order_by(desc(ScrapeTask.started_at))
            .first()
        )
        latest = "暂无执行记录"
        if latest_push:
            latest = (
                f"{latest_push.pushed_at.strftime('%Y-%m-%d %H:%M:%S')} | "
                f"{latest_push.keyword} | {latest_push.status} | {latest_push.content_count}条"
            )
        schedule = _get_or_create_schedule_config(db)
        slots = _get_schedule_slots(db)
        slot_text = "、".join(f"{h:02d}:{m:02d}" for h, m in slots) if slots else "-"
        running = f"运行中（{running_row.task_id[:8]}）" if running_row else "空闲"
        lock_state = get_login_block_state()
        lock_text = "正常"
        if lock_state.get("blocked"):
            lock_text = (
                f"已暂停（关键词：{lock_state.get('keyword') or '-'}，"
                f"原因：{lock_state.get('reason') or '-'}，"
                f"时间：{lock_state.get('blocked_at') or '-'}）"
            )
        return (
            "系统状态：\n"
            f"- 手动任务：{running}\n"
            f"- 等待队列：{queued}\n"
            f"- 启用关键词：{enabled_keywords}\n"
            f"- 定时任务：{'启用' if schedule.enabled else '停用'}（{slot_text}）\n"
            f"- 登录锁：{lock_text}\n"
            f"- 最近一次：{latest}"
        )
    finally:
        db.close()


async def _trigger_scrape_from_feishu(trigger_text: str) -> None:
    lock_state = get_login_block_state()
    if lock_state.get("blocked"):
        await _send_feishu_ops_text(
            "当前处于登录风控暂停状态，已阻止新抓取任务。\n"
            "请先扫码登录小红书，然后发送 `已登录`。"
        )
        return
    queued = enqueue_scrape_task(trigger_source="feishu", keyword_ids=None, scrape_headless=None)
    ensure_task_runner_started()
    await _send_feishu_ops_text(
        f"已收到指令：{trigger_text}\n"
        f"任务已入队：{queued['task_id'][:8]}（前方 {max(0, int(queued['position']) - 1)} 个）"
    )


async def _resume_after_login_from_feishu(trigger_text: str) -> None:
    unlocked, previous_lock_state, queued_existing = _resume_after_login_unlock_and_resume_queue()
    if not unlocked:
        await _send_feishu_ops_text(
            f"已收到指令：{trigger_text}\n"
            "当前未处于登录风控暂停状态，可直接发送 `立即抓取`。"
        )
        return

    previous_keyword = str(previous_lock_state.get("keyword") or "")
    await _send_feishu_ops_text(
        f"已收到指令：{trigger_text}\n"
        f"已解除登录风控暂停（上次关键词：{previous_keyword or '-'}）。\n"
        f"检测到待执行任务 {int(queued_existing)} 个，系统将按队列顺序继续执行。\n"
        "如需新增任务，请发送 `立即抓取`。"
    )


async def _send_feishu_ops_text(text: str) -> bool:
    return await push_send_ops_text(text)


async def _probe_xhs_login_state(keyword: str) -> dict[str, Any]:
    """
    在手动触发前做一次小红书登录态探测：
    - 若仍处于登录风控，会在探测流程内产出“最新二维码”并走飞书告警
    - 若已恢复登录，返回 login_required=False
    """
    probe_keyword = str(keyword or "").strip() or "AIGC"
    scraper = XiaohongshuScraper()
    scraper.enable_login_recover_wait(True)
    result = {
        "probe_ok": False,
        "login_required": False,
        "alert_sent": False,
        "risk_stage": "",
        "risk_flags": [],
        "risk_screenshot": "",
        "error": "",
    }
    try:
        probe_timeout = max(40, int(settings.xhs_login_recover_wait_seconds) + 20)
        await asyncio.wait_for(
            scraper.search_and_wrap(probe_keyword, max_results=1),
            timeout=probe_timeout,
        )
        result["probe_ok"] = True
    except Exception as exc:
        logging.exception("Manual login probe failed: keyword=%s", probe_keyword)
        result["error"] = str(exc)

    debug = scraper.last_search_debug if isinstance(getattr(scraper, "last_search_debug", None), dict) else {}
    result["login_required"] = bool(debug.get("login_required"))
    result["alert_sent"] = bool(debug.get("login_alert_sent"))
    result["risk_stage"] = str(debug.get("risk_stage") or "")
    result["risk_flags"] = list(debug.get("risk_flags") or [])
    result["risk_screenshot"] = str(debug.get("risk_screenshot") or "")
    return result


def _remember_feishu_chat_id(chat_id: str, chat_type: str = "") -> None:
    value = chat_id.strip()
    if not value:
        return
    normalized_type = str(chat_type or "").strip().lower()
    # 允许私聊(p2p)和群聊(group)两种会话绑定，按最近一次指令会话为准。
    if normalized_type and normalized_type not in {"group", "p2p"}:
        logging.info("Skip feishu chat binding for unsupported chat type: chat_id=%s type=%s", value, normalized_type)
        return
    db = SessionLocal()
    try:
        cfg = _get_or_create_app_config(db)
        if str(cfg.feishu_chat_id or "").strip() == value:
            return
        cfg.feishu_chat_id = value
        db.commit()
        logging.info("Feishu chat_id remembered: %s", value)
    except Exception:
        logging.exception("Remember feishu chat_id failed")
    finally:
        db.close()


@app.get("/api/platforms", response_model=list[str])
def list_supported_platforms():
    return _supported_platforms


@app.get("/", include_in_schema=False)
@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    if not _dashboard_file.exists():
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    return FileResponse(
        _dashboard_file,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/feed", include_in_schema=False)
def feed_page():
    if not _feed_file.exists():
        raise HTTPException(status_code=404, detail="feed.html not found")
    return FileResponse(_feed_file)


@app.on_event("startup")
def startup():
    global _scheduler, _runtime_probe_task
    init_db()
    _ensure_runtime_config_defaults()
    _ensure_schedule_defaults()
    _ensure_xhs_cookie_seeded()
    _scheduler = setup_scheduler()
    cfg, slots = _load_schedule_state()
    configure_daily_scrapes(_scheduler, cfg.enabled, slots)
    _scheduler.start()
    ensure_task_runner_started()
    try:
        asyncio.get_event_loop().create_task(_run_runtime_probe_once())
        _runtime_probe_task = asyncio.get_event_loop().create_task(_runtime_probe_loop())
    except Exception:
        logging.exception("Failed to start runtime probe loop")


@app.on_event("shutdown")
def shutdown():
    global _scheduler, _runtime_probe_task
    if _runtime_probe_task and not _runtime_probe_task.done():
        _runtime_probe_task.cancel()
    _runtime_probe_task = None
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def verify_api_key(
    request: Request,
    x_api_key: str = Header(default=""),
    db: Session = Depends(get_db),
):
    configured_key = _load_runtime_api_key(db)
    if configured_key and x_api_key != configured_key:
        raise_api_error(status_code=401, code="AUTH_INVALID", message="Invalid API key", request=request)


@app.get("/api/runtime/status", response_model=RuntimeStatusResponse)
def get_runtime_status(_: str = Depends(verify_api_key)):
    scheduler_ok = bool(_scheduler and _scheduler.running)
    runner = get_task_runner_state()
    lock_state = get_login_block_state()
    task_runner_ok = bool(runner.get("runner_task_alive"))
    backend_ok = int(_runtime_probe_state.get("local_health_code") or 0) == 200
    public_code = int(_runtime_probe_state.get("public_health_code") or 0)
    return RuntimeStatusResponse(
        backend_ok=backend_ok,
        scheduler_ok=scheduler_ok,
        task_runner_ok=task_runner_ok,
        local_health_code=int(_runtime_probe_state.get("local_health_code") or 0),
        public_health_code=public_code,
        login_blocked=bool(lock_state.get("blocked")),
        login_blocked_at=str(lock_state.get("blocked_at") or ""),
        login_block_reason=str(lock_state.get("reason") or ""),
        login_block_keyword=str(lock_state.get("keyword") or ""),
        tunnel_last_reachable_at=str(_runtime_probe_state.get("tunnel_last_reachable_at") or ""),
        last_checked_at=str(_runtime_probe_state.get("last_checked_at") or datetime.utcnow().isoformat() + "Z"),
    )


@app.post("/api/collect", response_model=ContentResponse)
def collect_content(
    req: CollectRequest,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """手动收藏一条内容（浏览器插件 / iOS 快捷指令调用）"""
    existing = (
        db.query(Content)
        .filter(Content.platform == req.platform, Content.url == req.url)
        .first()
    )
    if existing:
        return existing

    content = Content(
        platform=req.platform or _guess_platform(req.url),
        title=req.title,
        author=req.author,
        url=req.url,
        likes=req.likes,
        comments=req.comments,
        shares=req.shares,
        keyword=req.keyword,
        source=req.source,
    )
    db.add(content)
    db.commit()
    db.refresh(content)
    return content


@app.get("/api/contents", response_model=list[ContentResponse])
def list_contents(
    platform: str = Query(default="", description="按平台筛选"),
    keyword: str = Query(default="", description="按关键词筛选"),
    source: str = Query(default="", description="auto 或 manual"),
    days: int = Query(default=7, description="最近 N 天"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """查询已收藏的内容列表"""
    q = db.query(Content)
    if platform:
        q = q.filter(Content.platform == platform)
    if keyword:
        q = q.filter(Content.keyword.contains(keyword))
    if source:
        q = q.filter(Content.source == source)
    since = datetime.utcnow() - timedelta(days=days)
    q = q.filter(Content.collected_at >= since)
    return q.order_by(desc(Content.likes)).limit(limit).all()


@app.get("/api/feed", response_model=list[FeedItemResponse])
def list_feed_items(
    keyword: str = Query(default="", description="按关键词筛选"),
    platform: str = Query(default="xiaohongshu", description="按平台筛选"),
    min_likes: int = Query(default=0, ge=0),
    days: int = Query(default=7, ge=1, le=365),
    only_unread: bool = Query(default=False),
    only_starred: bool = Query(default=False),
    sort: str = Query(default="likes_desc", description="likes_desc/newest"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(Content)
    if platform:
        q = q.filter(Content.platform == platform)
    if keyword:
        q = q.filter(Content.keyword.contains(keyword))
    if min_likes > 0:
        q = q.filter(Content.likes >= min_likes)
    if only_unread:
        q = q.filter(Content.is_read == False)
    if only_starred:
        q = q.filter(Content.is_starred == True)

    since = datetime.utcnow() - timedelta(days=days)
    q = q.filter(Content.collected_at >= since)

    if sort == "newest":
        q = q.order_by(desc(Content.collected_at), desc(Content.likes))
    else:
        q = q.order_by(desc(Content.likes), desc(Content.collected_at))

    return q.offset(offset).limit(limit).all()


@app.post("/api/feed/{content_id}/read")
def mark_feed_read(
    content_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    row = db.query(Content).filter(Content.id == content_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Content not found")
    row.is_read = True
    row.viewed_at = datetime.utcnow()
    db.commit()
    return {"detail": "ok", "id": content_id, "is_read": True}


@app.post("/api/feed/{content_id}/star")
def mark_feed_star(
    content_id: int,
    starred: bool = Query(default=True),
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    row = db.query(Content).filter(Content.id == content_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Content not found")
    row.is_starred = bool(starred)
    db.commit()
    return {"detail": "ok", "id": content_id, "is_starred": row.is_starred}


@app.get("/api/feed/{content_id}/resolve-video")
async def resolve_feed_video(
    content_id: int,
    db: Session = Depends(get_db),
):
    row = db.query(Content).filter(Content.id == content_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Content not found")

    return {
        "id": row.id,
        "resolved": bool(row.video_url),
        "video_url": row.video_url or "",
        "publish_time": row.publish_time or "",
        "thumbnail": row.thumbnail or "",
        "updated": False,
    }


@app.get("/api/feed/{content_id}/stream")
async def stream_feed_video(
    content_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    row = db.query(Content).filter(Content.id == content_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Video not found")
    if not row.video_url:
        raise HTTPException(status_code=404, detail="Video not found")

    upstream_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.xiaohongshu.com/",
    }
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    client = httpx.AsyncClient(timeout=40.0, follow_redirects=True)
    req = client.build_request("GET", row.video_url, headers=upstream_headers)
    try:
        resp = await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        raise HTTPException(status_code=502, detail="Video source request failed")

    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"Video source unavailable (status={resp.status_code})",
        )

    passthrough = {}
    for key in (
        "content-type",
        "content-length",
        "accept-ranges",
        "content-range",
        "cache-control",
        "etag",
        "last-modified",
    ):
        if key in resp.headers:
            passthrough[key] = resp.headers[key]

    async def _iter():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(_iter(), status_code=resp.status_code, headers=passthrough)


@app.post("/api/keywords", response_model=KeywordResponse)
def create_keyword(
    req: KeywordCreate,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    """添加一个关注的关键词"""
    normalized_platforms = _normalize_requested_platforms(req.platforms)
    existing = db.query(Keyword).filter(Keyword.keyword == req.keyword).first()
    if existing:
        existing.platforms = json.dumps(normalized_platforms, ensure_ascii=False)
        existing.min_likes = req.min_likes
        existing.enabled = req.enabled
        db.commit()
        db.refresh(existing)
        return _keyword_to_response(existing)

    kw = Keyword(
        keyword=req.keyword,
        platforms=json.dumps(normalized_platforms, ensure_ascii=False),
        min_likes=req.min_likes,
        enabled=req.enabled,
    )
    db.add(kw)
    db.commit()
    db.refresh(kw)
    return _keyword_to_response(kw)


@app.get("/api/keywords", response_model=list[KeywordResponse])
def list_keywords(db: Session = Depends(get_db)):
    """查询所有关键词配置"""
    rows = db.query(Keyword).all()
    return [_keyword_to_response(r) for r in rows]


@app.delete("/api/keywords/{keyword_id}")
def delete_keyword(
    keyword_id: int,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    kw = db.query(Keyword).filter(Keyword.id == keyword_id).first()
    if not kw:
        raise HTTPException(status_code=404, detail="Keyword not found")
    db.delete(kw)
    db.commit()
    return {"detail": "deleted"}


def _keyword_to_response(kw: Keyword) -> KeywordResponse:
    platforms = _parse_stored_platforms(kw.platforms)
    return KeywordResponse(
        id=kw.id,
        keyword=kw.keyword,
        platforms=platforms,
        min_likes=kw.min_likes,
        enabled=kw.enabled,
        created_at=kw.created_at,
    )


@app.get("/api/dashboard/bootstrap", response_model=DashboardBootstrapResponse)
def dashboard_bootstrap(db: Session = Depends(get_db)):
    cfg = _get_or_create_schedule_config(db)
    slots = _get_schedule_slots(db)
    runtime_cfg = _get_or_create_app_config(db)
    return DashboardBootstrapResponse(
        api_key=runtime_cfg.api_key,
        feishu_webhook_url=runtime_cfg.feishu_webhook_url or "",
        feishu_chat_id=runtime_cfg.feishu_chat_id or "",
        schedule=ScheduleMultiResponse(
            enabled=cfg.enabled,
            slots=[ScheduleSlotItem(hour=h, minute=m, enabled=True) for h, m in slots],
            updated_at=cfg.updated_at,
        ),
        scrape_headless=bool(runtime_cfg.scrape_headless),
    )


@app.get("/api/system-config", response_model=SystemConfigResponse)
def get_system_config(db: Session = Depends(get_db)):
    cfg = _get_or_create_app_config(db)
    return SystemConfigResponse(
        api_key=cfg.api_key,
        feishu_webhook_url=cfg.feishu_webhook_url or "",
        feishu_chat_id=cfg.feishu_chat_id or "",
        updated_at=cfg.updated_at,
        scrape_headless=bool(cfg.scrape_headless),
    )


@app.put("/api/system-config", response_model=SystemConfigResponse)
def update_system_config(
    req: SystemConfigUpdate,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    if not req.api_key.strip():
        raise HTTPException(status_code=400, detail="API Key 不能为空")

    cfg = _get_or_create_app_config(db)
    cfg.api_key = req.api_key.strip()
    cfg.feishu_webhook_url = req.feishu_webhook_url.strip()
    cfg.feishu_chat_id = req.feishu_chat_id.strip()
    if req.scrape_headless is not None:
        cfg.scrape_headless = bool(req.scrape_headless)
    db.commit()
    db.refresh(cfg)
    return SystemConfigResponse(
        api_key=cfg.api_key,
        feishu_webhook_url=cfg.feishu_webhook_url or "",
        feishu_chat_id=cfg.feishu_chat_id or "",
        updated_at=cfg.updated_at,
        scrape_headless=bool(cfg.scrape_headless),
    )


@app.get("/api/schedule", response_model=ScheduleMultiResponse)
def get_schedule(db: Session = Depends(get_db)):
    cfg = _get_or_create_schedule_config(db)
    slots = _get_schedule_slots(db)
    return ScheduleMultiResponse(
        enabled=cfg.enabled,
        slots=[ScheduleSlotItem(hour=h, minute=m, enabled=True) for h, m in slots],
        updated_at=cfg.updated_at,
    )


@app.put("/api/schedule", response_model=ScheduleMultiResponse)
def update_schedule(
    req: ScheduleMultiUpdate,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    normalized = _normalize_slots(req.slots)
    if not normalized:
        raise HTTPException(status_code=400, detail="至少保留一个有效时间点")

    cfg = _get_or_create_schedule_config(db)
    cfg.enabled = req.enabled
    # 兼容旧字段：记录第一个时间点
    cfg.hour = normalized[0][0]
    cfg.minute = normalized[0][1]

    db.query(ScheduleSlot).delete()
    for hour, minute in normalized:
        db.add(ScheduleSlot(hour=hour, minute=minute, enabled=True))

    db.commit()
    db.refresh(cfg)

    if _scheduler:
        configure_daily_scrapes(_scheduler, cfg.enabled, normalized)

    return ScheduleMultiResponse(
        enabled=cfg.enabled,
        slots=[ScheduleSlotItem(hour=h, minute=m, enabled=True) for h, m in normalized],
        updated_at=cfg.updated_at,
    )


@app.post("/api/scrape/trigger", response_model=ScrapeTriggerResponse)
async def trigger_scrape(
    request: Request,
    req: ScrapeTriggerRequest = Body(default_factory=ScrapeTriggerRequest),
    _: str = Depends(verify_api_key),
):
    """手动触发一次全量抓取+推送"""
    lock_state = get_login_block_state()
    if lock_state.get("blocked"):
        blocked_kw = str(lock_state.get("keyword") or "").strip() or "AIGC"
        probe = await _probe_xhs_login_state(blocked_kw)
        if probe.get("probe_ok") and (not probe.get("login_required")):
            set_login_blocked(False)
            try:
                await _send_feishu_ops_text(
                    "检测到你在 Dashboard 点击了“立即执行”，系统已先做登录探测并确认当前可用，"
                    "现已自动解除登录暂停并继续执行本次抓取。"
                )
            except Exception:
                logging.exception("Send login-recovered text to feishu failed")
        else:
            try:
                if not probe.get("alert_sent"):
                    blocked_at = str(lock_state.get("blocked_at") or "-")
                    blocked_reason = str(lock_state.get("reason") or "-")
                    await _send_feishu_ops_text(
                        "检测到你在 Dashboard 点击了“立即执行”，系统已先做登录探测，当前仍处于登录风控状态。\n"
                        f"- 关键词：{blocked_kw}\n"
                        f"- 原因：{blocked_reason}\n"
                        f"- 时间：{blocked_at}\n"
                        "请先扫码登录小红书，然后在飞书发送“已登录”自动恢复。"
                    )
            except Exception:
                logging.exception("Send blocked reminder to feishu failed")
            raise_api_error(
                status_code=409,
                code="LOGIN_BLOCKED",
                message=(
                    "Scrape blocked by login risk. "
                    "本次已进行登录探测；若仍未恢复，飞书会发送最新二维码。"
                ),
                request=request,
            )
    keyword_ids = sorted({int(i) for i in req.keyword_ids if int(i) > 0})
    scrape_headless = req.scrape_headless
    # 手动触发若指定了模式，则先更新 Dashboard 全局抓取模式；
    # 后续任务会在“每个关键词执行前”读取该配置，保证执行中改配置可及时生效。
    if scrape_headless is not None:
        db = SessionLocal()
        try:
            cfg = _get_or_create_app_config(db)
            cfg.scrape_headless = bool(scrape_headless)
            db.commit()
        finally:
            db.close()
    queued = enqueue_scrape_task(
        trigger_source="manual",
        keyword_ids=keyword_ids or None,
        scrape_headless=None,
        request_id=_request_id_from_request(request),
    )
    ensure_task_runner_started()
    return ScrapeTriggerResponse(
        detail="Scrape job triggered in background",
        task_id=queued["task_id"],
        status=queued["status"],
        queued_at=queued["queued_at"],
        position=int(queued["position"]),
        scope="selected" if keyword_ids else "enabled_default",
        keyword_count=len(keyword_ids),
        scrape_headless=scrape_headless,
    )


@app.get("/api/tasks/{task_id}", response_model=ScrapeTaskResponse)
def get_task(
    task_id: str,
    request: Request,
    _: str = Depends(verify_api_key),
):
    row = get_scrape_task(task_id)
    if not row:
        raise_api_error(404, "NOT_FOUND", "Task not found", request)
    return _task_to_response(row)


@app.get("/api/tasks", response_model=list[ScrapeTaskResponse])
def get_tasks(
    status: str = Query(default="", description="queued/running/success/failed/partial"),
    limit: int = Query(default=20, ge=1, le=200),
    _: str = Depends(verify_api_key),
):
    rows = list_scrape_tasks(status=status, limit=limit)
    return [_task_to_response(r) for r in rows]


def _task_to_response(row: ScrapeTask) -> ScrapeTaskResponse:
    summary = _parse_summary_json(row.result_summary or "")
    queue_position = 0
    retry_count = _derive_retry_count(summary)
    failed_log_id = _derive_failed_history_log_id(summary)
    failure_category = _derive_failure_category(row.error_code or "", row.error_message or "", summary)

    if (row.status or "") == "queued":
        db = SessionLocal()
        try:
            queue_position = (
                db.query(ScrapeTask)
                .filter(ScrapeTask.status == "queued", ScrapeTask.id <= row.id)
                .count()
            )
        finally:
            db.close()

    return ScrapeTaskResponse(
        task_id=row.task_id,
        trigger_source=row.trigger_source or "manual",
        status=row.status or "queued",
        requested_keyword_ids=_safe_parse_int_list(row.requested_keyword_ids),
        resolved_keyword_ids=_safe_parse_int_list(row.resolved_keyword_ids),
        scrape_headless=row.scrape_headless,
        queued_at=row.queued_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code or "",
        error_message=row.error_message or "",
        result_summary=row.result_summary or "",
        request_id=row.request_id or "",
        queue_position=int(queue_position),
        retry_count=int(retry_count),
        failure_category=failure_category,
        failed_history_log_id=failed_log_id,
    )


def _safe_parse_int_list(raw: str) -> list[int]:
    try:
        values = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(values, list):
        return []
    out: list[int] = []
    for val in values:
        try:
            i = int(val)
            if i > 0:
                out.append(i)
        except Exception:
            continue
    return sorted(set(out))


def _parse_summary_json(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _derive_retry_count(summary: dict[str, Any]) -> int:
    max_used = int(summary.get("max_attempts_used") or 0)
    if max_used > 0:
        return max(0, max_used - 1)
    per_platform = summary.get("per_platform") if isinstance(summary, dict) else None
    if not isinstance(per_platform, dict):
        return 0
    max_attempt = 0
    for v in per_platform.values():
        if not isinstance(v, dict):
            continue
        attempts = int(v.get("attempts") or 0)
        if attempts > max_attempt:
            max_attempt = attempts
    return max(0, max_attempt - 1)


def _derive_failed_history_log_id(summary: dict[str, Any]) -> Optional[int]:
    log_ids = summary.get("failed_log_ids") if isinstance(summary, dict) else None
    if not isinstance(log_ids, list) or not log_ids:
        return None
    try:
        valid = [int(v) for v in log_ids if int(v) > 0]
    except Exception:
        return None
    if not valid:
        return None
    return max(valid)


def _derive_failure_category(error_code: str, error_message: str, summary: dict[str, Any]) -> str:
    text = f"{error_code} {error_message}".lower()
    if "auth" in text:
        return "AUTH"
    if "login" in text:
        return "LOGIN_REQUIRED"
    if "risk" in text or "captcha" in text:
        return "RISK"
    if "timeout" in text or "connect" in text or "network" in text:
        return "NETWORK"
    if error_code:
        return "SYSTEM"
    if int(summary.get("login_required_count") or 0) > 0:
        return "LOGIN_REQUIRED"
    return ""


@app.get("/api/history", response_model=HistoryListResponse)
def list_history(
    keyword: str = Query(default="", description="按关键词筛选"),
    status: str = Query(default="", description="success/failed/partial"),
    days: int = Query(default=30, ge=1, le=365),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(days=days)
    q = db.query(PushLog).filter(PushLog.pushed_at >= since)
    if keyword:
        q = q.filter(PushLog.keyword.contains(keyword))
    if status:
        q = q.filter(PushLog.status == status)

    total = q.count()
    rows = q.order_by(desc(PushLog.pushed_at)).offset(offset).limit(limit).all()

    sq = db.query(PushLog).filter(PushLog.pushed_at >= since)
    if keyword:
        sq = sq.filter(PushLog.keyword.contains(keyword))
    if status:
        sq = sq.filter(PushLog.status == status)
    summary_rows = sq.all()
    success_runs = sum(1 for r in summary_rows if r.status == "success")
    failed_runs = sum(1 for r in summary_rows if r.status == "failed")
    total_contents = sum(r.content_count for r in summary_rows)

    return HistoryListResponse(
        total=total,
        offset=offset,
        limit=limit,
        summary=HistorySummary(
            total_runs=len(summary_rows),
            success_runs=success_runs,
            failed_runs=failed_runs,
            total_contents=total_contents,
        ),
        items=[
            PushLogResponse(
                id=r.id,
                pushed_at=r.pushed_at,
                keyword=r.keyword,
                content_count=r.content_count,
                status=r.status,
                detail=r.detail or "",
            )
            for r in rows
        ],
    )


@app.get("/api/history/{log_id}", response_model=PushLogResponse)
def get_history_detail(log_id: int, db: Session = Depends(get_db)):
    row = db.query(PushLog).filter(PushLog.id == log_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="History not found")
    return PushLogResponse(
        id=row.id,
        pushed_at=row.pushed_at,
        keyword=row.keyword,
        content_count=row.content_count,
        status=row.status,
        detail=row.detail or "",
    )


@app.get("/api/risk/stats", response_model=RiskStatsResponse)
def get_risk_stats(db: Session = Depends(get_db)):
    """获取风控事件统计"""
    all_events = db.query(RiskEvent).all()
    total_events = len(all_events)

    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)
    recent_events = [e for e in all_events if e.detected_at >= day_ago]
    recent_events_24h = len(recent_events)

    last_event = (
        db.query(RiskEvent)
        .order_by(desc(RiskEvent.detected_at))
        .first()
    )
    last_event_at = last_event.detected_at if last_event else None

    platform_breakdown: dict[str, int] = {}
    for event in all_events:
        platform_breakdown[event.platform] = platform_breakdown.get(event.platform, 0) + 1

    return RiskStatsResponse(
        total_events=total_events,
        recent_events_24h=recent_events_24h,
        last_event_at=last_event_at,
        platform_breakdown=platform_breakdown,
    )


@app.get("/api/risk/events", response_model=list[RiskEventResponse])
def list_risk_events(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """获取最近的风控事件列表"""
    rows = (
        db.query(RiskEvent)
        .order_by(desc(RiskEvent.detected_at))
        .limit(limit)
        .all()
    )
    return [
        RiskEventResponse(
            id=r.id,
            platform=r.platform,
            keyword=r.keyword,
            detected_at=r.detected_at,
            flags=r.flags or "",
            stage=r.stage or "",
            screenshot_path=r.screenshot_path or "",
            html_path=r.html_path or "",
            screenshot_url=(_build_risk_artifact_url(db, int(r.id), "screenshot") if r.screenshot_path else ""),
            html_url=(_build_risk_artifact_url(db, int(r.id), "html") if r.html_path else ""),
            recovery_hint=_build_risk_recovery_hint(r),
            artifact_token_supported=True,
        )
        for r in rows
    ]


def _build_risk_recovery_hint(event: RiskEvent) -> str:
    stage = (event.stage or "").lower()
    if stage in {"pre_capture", "login", "login_required"}:
        return "检测到登录态失效：请扫码重新登录小红书，再手动触发一次抓取验证。"
    if stage in {"api_empty", "risk_page", "captcha"}:
        return "疑似触发风控：建议改用有头模式重试，并降低触发频率。"
    return "建议先查看截图与 HTML 快照定位页面状态，再执行恢复操作。"


def _risk_token_secret(db: Session) -> str:
    preferred = settings.risk_artifact_token_secret.strip()
    if preferred:
        return preferred
    runtime_api_key = _load_runtime_api_key(db).strip()
    if runtime_api_key:
        return runtime_api_key
    return settings.api_key.strip() or "topic-assistant-risk-token"


def _count_queued_scrape_tasks() -> int:
    db = SessionLocal()
    try:
        return int(db.query(ScrapeTask).filter(ScrapeTask.status == "queued").count())
    finally:
        db.close()


def _resume_after_login_unlock_and_resume_queue() -> tuple[bool, dict[str, Any], int]:
    lock_state = get_login_block_state()
    queued_existing = _count_queued_scrape_tasks()
    if not lock_state.get("blocked"):
        return False, lock_state, queued_existing

    # 解锁后只恢复既有队列，不额外插入新任务。
    set_login_blocked(False)
    ensure_task_runner_started()
    return True, lock_state, queued_existing


def _build_risk_artifact_url(
    db: Session,
    event_id: int,
    kind: str,
) -> str:
    secret = _risk_token_secret(db)
    token, _ = generate_risk_artifact_token(
        event_id=event_id,
        kind=kind,
        secret=secret,
        ttl_seconds=max(60, int(settings.risk_artifact_token_ttl_seconds)),
    )
    return f"/api/risk/events/{event_id}/artifact?kind={kind}&token={token}"


@app.get("/api/risk/events/{event_id}/artifact-token", response_model=RiskArtifactTokenResponse)
def get_risk_event_artifact_token(
    event_id: int,
    kind: str = Query(default="screenshot", description="screenshot/html"),
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key),
):
    row = db.query(RiskEvent).filter(RiskEvent.id == event_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Risk event not found")
    if kind not in {"screenshot", "html"}:
        raise HTTPException(status_code=400, detail="Invalid kind")
    file_path = row.screenshot_path if kind == "screenshot" else row.html_path
    if not file_path:
        raise HTTPException(status_code=404, detail="Artifact not found")
    secret = _risk_token_secret(db)
    token, exp_ts = generate_risk_artifact_token(
        event_id=event_id,
        kind=kind,
        secret=secret,
        ttl_seconds=max(60, int(settings.risk_artifact_token_ttl_seconds)),
    )
    return RiskArtifactTokenResponse(
        event_id=event_id,
        kind=kind,
        token=token,
        expires_at=datetime.utcfromtimestamp(exp_ts).isoformat() + "Z",
    )


@app.get("/api/risk/events/{event_id}/artifact")
def get_risk_event_artifact(
    event_id: int,
    kind: str = Query(default="screenshot", description="screenshot/html"),
    token: str = Query(default="", description="短时访问令牌（推荐）"),
    api_key: str = Query(default="", description="可选：用于浏览器直链鉴权"),
    x_api_key: str = Header(default=""),
    db: Session = Depends(get_db),
):
    if kind not in {"screenshot", "html"}:
        raise HTTPException(status_code=400, detail="Invalid kind")

    secret = _risk_token_secret(db)
    supplied_token = str(token or "").strip()
    if supplied_token:
        token_result = verify_risk_artifact_token(
            token=supplied_token,
            secret=secret,
            event_id=event_id,
            kind=kind,
        )
        if not token_result.get("ok"):
            raise HTTPException(status_code=401, detail="Invalid or expired token")
    else:
        # 兼容期：允许 api_key 旧链路访问，默认 7 天
        grace_days = max(0, int(settings.risk_artifact_legacy_api_key_days))
        if grace_days <= 0:
            raise HTTPException(status_code=401, detail="token is required")
        configured_key = _load_runtime_api_key(db)
        supplied_key = (api_key or x_api_key or "").strip()
        if configured_key and supplied_key != configured_key:
            raise HTTPException(status_code=401, detail="Invalid API key")

    row = db.query(RiskEvent).filter(RiskEvent.id == event_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Risk event not found")

    file_path = row.screenshot_path if kind == "screenshot" else row.html_path
    if not file_path:
        raise HTTPException(status_code=404, detail="Artifact not found")

    path_obj = Path(file_path)
    if not path_obj.exists() or not path_obj.is_file():
        raise HTTPException(status_code=404, detail="Artifact file missing")

    media_type = "image/png" if kind == "screenshot" else "text/html; charset=utf-8"
    filename = path_obj.name
    return FileResponse(path_obj, media_type=media_type, filename=filename)


@app.get("/api/login/ack", include_in_schema=False)
def login_ack_by_token(
    event_id: int = Query(..., ge=1),
    token: str = Query(default=""),
):
    db = SessionLocal()
    try:
        secret = _risk_token_secret(db)
    finally:
        db.close()

    token_result = verify_risk_artifact_token(
        token=(token or "").strip(),
        secret=secret,
        event_id=int(event_id),
        kind="resume",
    )
    if not token_result.get("ok"):
        return HTMLResponse(
            content=(
                "<html><body style='font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; padding: 24px;'>"
                "<h2>恢复失败</h2>"
                "<p>登录恢复链接已失效或无效，请重新触发风控告警后使用最新链接。</p>"
                "</body></html>"
            ),
            status_code=401,
        )

    unlocked, previous_lock_state, queued_existing = _resume_after_login_unlock_and_resume_queue()
    previous_keyword = str(previous_lock_state.get("keyword") or "")

    if not unlocked:
        return HTMLResponse(
            content=(
                "<html><body style='font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; padding: 24px;'>"
                "<h2>已是可运行状态</h2>"
                "<p>当前未处于登录风控暂停，系统可直接继续抓取。</p>"
                "</body></html>"
            ),
            status_code=200,
        )

    base = settings.public_base_url.strip().rstrip("/")
    dashboard_url = (f"{base}/dashboard" if base else "/dashboard")
    if int(queued_existing) > 0:
        queue_tip = f"检测到待执行任务 {int(queued_existing)} 个，系统将按队列顺序继续执行。"
    else:
        queue_tip = "当前没有待执行任务，请回 Dashboard 点击“立即执行”发起抓取。"

    body = (
        "<html><body style='font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; padding: 24px;'>"
        "<h2>恢复成功</h2>"
        f"<p>已解除登录风控暂停（上次关键词：{previous_keyword or '-'}）。</p>"
        f"<p>{queue_tip}</p>"
        f"<p><a href='{dashboard_url}'>打开 Dashboard</a></p>"
        "</body></html>"
    )
    return HTMLResponse(content=body, status_code=200)


async def _resolve_xiaohongshu_content_metadata(content: Content) -> bool:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.xiaohongshu.com/",
    }
    cookie_header = _build_xhs_cookie_header()
    if cookie_header:
        headers["Cookie"] = cookie_header

    note = None
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(content.url, headers=headers)
            resp.raise_for_status()
            html = resp.text
        state = _extract_initial_state_from_html(html)
        if isinstance(state, dict):
            note = _extract_note_from_state(state)
    except Exception:
        logging.info("http resolve failed, fallback to playwright: url=%s", content.url)

    if isinstance(note, dict):
        changed = _apply_note_metadata_to_content(content, note)
        if changed:
            return True

    # HTTP 方案容易命中 302/404 风控，兜底用浏览器上下文解析
    return await _resolve_xiaohongshu_content_metadata_via_playwright(content)


async def _resolve_xiaohongshu_content_metadata_via_playwright(content: Content) -> bool:
    state = None
    video_from_dom = ""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            await _load_xhs_cookies_to_context(context)
            page = await context.new_page()
            await page.goto(content.url, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(3000)
            state_raw = await page.evaluate(
                "() => { try { return JSON.stringify(window.__INITIAL_STATE__ || {}); } catch (e) { return ''; } }"
            )
            if isinstance(state_raw, str) and state_raw:
                try:
                    state = json.loads(state_raw)
                except Exception:
                    state = None
            video_from_dom = await page.evaluate(
                "() => { const v = document.querySelector('video'); return (v && (v.currentSrc || v.src)) || ''; }"
            )
            await browser.close()
    except Exception:
        logging.exception("playwright resolve failed: url=%s", content.url)
        return False

    note = _extract_note_from_state(state or {})
    changed = False
    if isinstance(note, dict):
        changed = _apply_note_metadata_to_content(content, note)
    if video_from_dom and _looks_like_video_url(video_from_dom) and content.video_url != video_from_dom:
        content.video_url = video_from_dom
        changed = True
    return changed


def _apply_note_metadata_to_content(content: Content, note: dict[str, Any]) -> bool:
    changed = False
    video_url = _extract_video_url_from_note(note)
    publish_time = _format_note_publish_time(note)
    cover = _extract_note_cover(note)

    if video_url and content.video_url != video_url:
        content.video_url = video_url
        changed = True
    if publish_time and content.publish_time != publish_time:
        content.publish_time = publish_time
        changed = True
    if cover and (not content.thumbnail or content.thumbnail != cover):
        content.thumbnail = cover
        changed = True
    return changed


async def _load_xhs_cookies_to_context(context) -> None:
    cookie_path = settings.cookie_dir / "xiaohongshu.json"
    if not cookie_path.exists():
        return
    try:
        cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(cookies, list):
        return
    now_ts = datetime.now().timestamp()
    valid = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        expires = c.get("expires", -1)
        if isinstance(expires, (int, float)) and expires > 0 and expires <= now_ts:
            continue
        valid.append(c)
    if valid:
        await context.add_cookies(valid)


def _build_xhs_cookie_header() -> str:
    cookie_path = settings.cookie_dir / "xiaohongshu.json"
    if not cookie_path.exists():
        return ""
    try:
        cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(cookies, list):
        return ""

    now_ts = datetime.now().timestamp()
    parts = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        domain = str(c.get("domain") or "")
        if "xiaohongshu.com" not in domain:
            continue
        expires = c.get("expires", -1)
        if isinstance(expires, (int, float)) and expires > 0 and expires <= now_ts:
            continue
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        if not name:
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _extract_initial_state_from_html(html: str) -> Optional[dict[str, Any]]:
    if not html:
        return None
    m = re.search(r"window\.__INITIAL_STATE__=(\{.*?\})</script>", html, flags=re.S)
    if not m:
        return None
    raw = m.group(1)
    # JS 字面量清洗：把 undefined 兜底成 null
    raw = re.sub(r":undefined([,}])", r":null\1", raw)
    try:
        return json.loads(raw)
    except Exception:
        return None


def _extract_note_from_state(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    note_state = state.get("note") if isinstance(state.get("note"), dict) else {}
    detail_map = (
        note_state.get("noteDetailMap")
        if isinstance(note_state.get("noteDetailMap"), dict)
        else {}
    )
    for value in detail_map.values():
        if not isinstance(value, dict):
            continue
        note = value.get("note")
        if isinstance(note, dict) and note:
            return note
    return None


def _extract_video_url_from_note(note: dict[str, Any]) -> str:
    roots = [
        note.get("video"),
        note.get("videos"),
        note.get("media"),
        note,
    ]
    for root in roots:
        url = _find_video_url(root)
        if url:
            return url
    return ""


def _find_video_url(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node if _looks_like_video_url(node) else ""
    if isinstance(node, list):
        for item in node:
            found = _find_video_url(item)
            if found:
                return found
        return ""
    if not isinstance(node, dict):
        return ""

    for key in (
        "master_url",
        "url",
        "backup_url",
        "origin_url",
        "video_url",
        "h264",
        "h265",
        "default",
    ):
        if key in node:
            found = _find_video_url(node.get(key))
            if found:
                return found
    for value in node.values():
        found = _find_video_url(value)
        if found:
            return found
    return ""


def _looks_like_video_url(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not (text.startswith("http://") or text.startswith("https://")):
        return False
    markers = (".mp4", ".m3u8", "/video/", "/stream/", "h264", "h265", "fmp4")
    if not any(m in text for m in markers):
        return False
    return "xiaohongshu.com/explore/" not in text


def _extract_note_cover(note: dict[str, Any]) -> str:
    image_list = note.get("image_list")
    if isinstance(image_list, list) and image_list:
        first = image_list[0]
        if isinstance(first, dict):
            for key in ("url_default", "url_pre", "url"):
                value = first.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    cover = note.get("cover")
    if isinstance(cover, dict):
        for key in ("url_pre", "url", "default"):
            value = cover.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _format_note_publish_time(note: dict[str, Any]) -> str:
    candidates = [
        note.get("time"),
        note.get("publish_time"),
        note.get("publishTime"),
        note.get("create_time"),
    ]
    for value in candidates:
        dt = _to_datetime(value)
        if dt:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    return ""


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000
            return datetime.fromtimestamp(ts)
        text = str(value).strip()
        if not text:
            return None
        if re.fullmatch(r"\d{10,13}", text):
            ts = float(text)
            if ts > 10_000_000_000:
                ts /= 1000
            return datetime.fromtimestamp(ts)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except Exception:
                continue
    except Exception:
        return None
    return None


def _guess_platform(url: str) -> str:
    domain_map = {
        "xiaohongshu.com": "xiaohongshu",
        "xhslink.com": "xiaohongshu",
        "douyin.com": "douyin",
        "bilibili.com": "bilibili",
        "b23.tv": "bilibili",
        "youtube.com": "youtube",
        "youtu.be": "youtube",
        "x.com": "x",
        "twitter.com": "x",
        "tiktok.com": "tiktok",
        "channels.weixin.qq.com": "shipinhao",
    }
    for domain, platform in domain_map.items():
        if domain in url:
            return platform
    return "unknown"


def _get_or_create_schedule_config(db: Session) -> ScheduleConfig:
    cfg = db.query(ScheduleConfig).first()
    if cfg:
        return cfg

    cfg = ScheduleConfig(
        enabled=settings.schedule_enabled,
        hour=settings.schedule_cron_hour,
        minute=settings.schedule_cron_minute,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def _get_or_create_app_config(db: Session) -> AppConfig:
    cfg = db.query(AppConfig).first()
    if cfg:
        changed = False
        if cfg.scrape_headless is None:
            cfg.scrape_headless = settings.scrape_headless
            changed = True
        if not str(cfg.feishu_chat_id or "").strip() and settings.feishu_chat_id.strip():
            cfg.feishu_chat_id = settings.feishu_chat_id.strip()
            changed = True
        if changed:
            db.commit()
            db.refresh(cfg)
        return cfg

    cfg = AppConfig(
        api_key=settings.api_key,
        feishu_webhook_url=settings.feishu_webhook_url,
        feishu_chat_id=settings.feishu_chat_id,
        scrape_headless=settings.scrape_headless,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def _load_runtime_api_key(db: Session) -> str:
    cfg = _get_or_create_app_config(db)
    return cfg.api_key or settings.api_key


def _ensure_runtime_config_defaults() -> None:
    db = SessionLocal()
    try:
        _get_or_create_app_config(db)
    finally:
        db.close()


def _ensure_schedule_defaults() -> None:
    db = SessionLocal()
    try:
        cfg = _get_or_create_schedule_config(db)
        if db.query(ScheduleSlot).count() == 0:
            db.add(ScheduleSlot(hour=cfg.hour, minute=cfg.minute, enabled=True))
            db.commit()
    finally:
        db.close()


def _ensure_xhs_cookie_seeded() -> None:
    """支持通过环境变量注入小红书 cookie，方便云端部署。"""
    raw = ""
    if settings.xhs_cookies_base64.strip():
        try:
            raw = base64.b64decode(settings.xhs_cookies_base64.strip()).decode("utf-8")
        except Exception:
            logging.exception("TA_XHS_COOKIES_BASE64 decode failed")
            return
    elif settings.xhs_cookies_json.strip():
        raw = settings.xhs_cookies_json.strip()

    if not raw:
        return

    cookie_path = settings.cookie_dir / "xiaohongshu.json"
    if cookie_path.exists() and not settings.xhs_cookies_overwrite:
        logging.info(
            "Cookie file already exists, skip env injection: %s (set TA_XHS_COOKIES_OVERWRITE=true to override)",
            cookie_path,
        )
        return

    try:
        cookies = json.loads(raw)
        if not isinstance(cookies, list):
            raise ValueError("cookie payload must be a JSON array")
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logging.info("Seeded xiaohongshu cookie file from env: %s", cookie_path)
    except Exception:
        logging.exception("Failed to seed xiaohongshu cookie from env")


def _load_schedule_state() -> tuple[ScheduleConfig, list[tuple[int, int]]]:
    db = SessionLocal()
    try:
        cfg = _get_or_create_schedule_config(db)
        slots = _get_schedule_slots(db)
        return cfg, slots
    finally:
        db.close()


def _get_schedule_slots(db: Session) -> list[tuple[int, int]]:
    rows = db.query(ScheduleSlot).filter(ScheduleSlot.enabled == True).all()
    if not rows:
        cfg = _get_or_create_schedule_config(db)
        return [(cfg.hour, cfg.minute)]
    return sorted([(r.hour, r.minute) for r in rows])


def _normalize_slots(slots: list[ScheduleSlotItem]) -> list[tuple[int, int]]:
    normalized: set[tuple[int, int]] = set()
    for slot in slots:
        if slot.hour < 0 or slot.hour > 23:
            continue
        if slot.minute < 0 or slot.minute > 59:
            continue
        normalized.add((slot.hour, slot.minute))
    return sorted(normalized)


def _parse_stored_platforms(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except Exception:
        return ["xiaohongshu"]
    if not isinstance(value, list):
        return ["xiaohongshu"]
    platforms = []
    for item in value:
        name = str(item).strip().lower()
        if not name or name in platforms:
            continue
        platforms.append(name)
    return platforms or ["xiaohongshu"]


def _normalize_requested_platforms(platforms: list[str]) -> list[str]:
    normalized = []
    unsupported = []
    for item in platforms:
        name = str(item).strip().lower()
        if not name:
            continue
        if name in normalized:
            continue
        if name not in _supported_platforms:
            unsupported.append(name)
            continue
        normalized.append(name)

    if unsupported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported platforms: {', '.join(unsupported)}",
        )
    if not normalized:
        raise HTTPException(status_code=400, detail="至少选择一个有效平台")
    return normalized
