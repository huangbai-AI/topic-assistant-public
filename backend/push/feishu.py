"""
飞书推送模块
- 优先使用应用机器人（im/v1/messages，chat_id）
- 若未配置应用机器人，再回退到 Webhook 自定义机器人
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from config import settings
from database import AppConfig, RiskEvent, SessionLocal
from models import ContentItem
from security_tokens import generate_risk_artifact_token

logger = logging.getLogger(__name__)

PLATFORM_NAMES = {
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "bilibili": "B站",
    "youtube": "YouTube",
    "x": "X",
    "tiktok": "TikTok",
    "shipinhao": "视频号",
}

_TOKEN_CACHE: dict[str, Any] = {"value": "", "expires_at": 0.0}


async def push_to_feishu(
    keyword: str,
    items_by_platform: dict[str, list[ContentItem]],
) -> bool:
    total_count = sum(len(v) for v in items_by_platform.values())
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if total_count == 0:
        logger.info("No items to push for keyword: %s, sending empty notification", keyword)
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"📋 选题推送 | {keyword} | {run_time}",
                },
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**关键词：{keyword}**\n"
                        f"**抓取时间：{run_time}**\n\n"
                        "本次抓取未发现符合条件的内容（可能原因：点赞数不足、已去重、或抓取失败）"
                    ),
                }
            ],
        }
        try:
            ok = await _send_card(card)
            if ok:
                logger.info("Feishu empty notification sent for keyword: %s", keyword)
                return True
            logger.error("Feishu empty notification failed for keyword: %s", keyword)
            return False
        except Exception:
            logger.exception("Feishu empty notification error for keyword: %s", keyword)
            return False

    elements = _build_card_elements(keyword, items_by_platform, run_time)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"📋 选题推送 | {keyword} | {run_time}",
            },
            "template": "blue",
        },
        "elements": elements,
    }

    try:
        ok = await _send_card(card)
        if ok:
            logger.info("Feishu push success: keyword=%s, total=%d", keyword, total_count)
            return True
        # 卡片失败时降级成文本，避免“抓取成功但完全无消息”
        fallback_text = f"选题推送失败（卡片发送失败）\\n关键词：{keyword}\\n时间：{run_time}\\n请检查飞书应用消息权限或卡片能力。"
        text_ok = await _send_text(fallback_text)
        logger.error("Feishu push failed for keyword=%s (fallback_text_ok=%s)", keyword, text_ok)
        return bool(text_ok)
    except Exception:
        logger.exception("Feishu push error for keyword: %s", keyword)
        return False


async def push_login_risk_alert(
    platform: str,
    keyword: str,
    flags: list[str],
    stage: str,
    screenshot_path: str = "",
    html_path: str = "",
    alert_count: int = 1,
) -> bool:
    """登录掉线/风控告警：优先单独发送二维码图片，再发送恢复指引。"""
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    platform_name = PLATFORM_NAMES.get(platform, platform)
    flag_text = "、".join(flags) if flags else "未知"
    link_info = _build_risk_artifact_links(
        platform=platform,
        keyword=keyword,
        stage=stage,
        screenshot_path=screenshot_path,
        html_path=html_path,
    )

    recovery_steps = [
        "1) 使用小红书 App 扫描上方二维码图片完成登录",
        "2) 登录后等待 5-10 秒，在飞书发送“已登录”（若未接入消息回调，点下方“一键恢复”）",
        "3) 系统会自动解除暂停并发起一次重试；若仍失败再切到有头模式",
    ]
    dashboard_link_md = ""
    if settings.public_base_url.strip():
        dashboard_link_md = f"[打开 Dashboard]({settings.public_base_url.strip().rstrip('/')}/dashboard)"

    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**平台**：{platform_name}\n"
                f"**关键词**：{keyword}\n"
                f"**阶段**：{stage}\n"
                f"**检测时间**：{run_time}\n"
                f"**连续告警**：第 {max(1, int(alert_count))} 次\n"
                f"**命中信号**：{flag_text}\n\n"
                "检测到可能掉线，请按恢复步骤处理。"
            ),
        },
        {"tag": "markdown", "content": "**恢复步骤**\n" + "\n".join(recovery_steps)},
    ]

    message_ok = False
    image_delivered = False
    image_key = await _try_upload_image_and_get_key(screenshot_path)
    if image_key:
        image_ok = await _send_image(image_key=image_key)
        message_ok = message_ok or image_ok
        image_delivered = bool(image_ok)
        if not image_ok:
            elements.append({"tag": "markdown", "content": "二维码图片发送失败，已降级为链接方式。"})
    else:
        if screenshot_path:
            elements.append(
                {
                    "tag": "markdown",
                    "content": f"二维码截图已保存：`{screenshot_path}`\n{link_info.get('screenshot_markdown', '')}",
                }
            )
        if settings.feishu_app_id.strip() and settings.feishu_app_secret.strip():
            elements.append(
                {
                    "tag": "markdown",
                    "content": "已尝试上传截图但失败，请检查飞书应用权限（im:image 上传）或 chat_id 配置。",
                }
            )
        else:
            elements.append(
                {
                    "tag": "markdown",
                    "content": "未配置 `TA_FEISHU_APP_ID/TA_FEISHU_APP_SECRET`，当前无法直接发图片；请使用下方“一键恢复”继续流程。",
                }
            )

    if html_path:
        elements.append(
            {
                "tag": "markdown",
                "content": f"调试页快照：`{html_path}`\n{link_info.get('html_markdown', '')}",
            }
        )

    quick_links_lines = []
    if link_info.get("screenshot_markdown") and not image_delivered:
        quick_links_lines.append(f"- 最近二维码截图：{link_info['screenshot_markdown']}")
    if link_info.get("html_markdown"):
        quick_links_lines.append(f"- 最近调试快照：{link_info['html_markdown']}")
    if dashboard_link_md:
        quick_links_lines.append(f"- 控制台入口：{dashboard_link_md}")
    if link_info.get("resume_markdown"):
        quick_links_lines.append(f"- 我已登录，一键恢复：{link_info['resume_markdown']}")
    if quick_links_lines:
        elements.append({"tag": "markdown", "content": "**快速链接**\n" + "\n".join(quick_links_lines)})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"⚠️ 登录风控告警 | {platform_name} | {run_time}",
            },
            "template": "red",
        },
        "elements": elements,
    }
    card_ok = await _send_card(card)
    return bool(message_ok or card_ok)


async def send_ops_text(text: str) -> bool:
    """发送运维指令回复文本（应用机器人优先）。"""
    return await _send_text(text)


def _build_risk_artifact_links(
    platform: str,
    keyword: str,
    stage: str,
    screenshot_path: str,
    html_path: str,
) -> dict[str, str]:
    base_url = settings.public_base_url.strip().rstrip("/")
    if not base_url:
        return {"screenshot_markdown": "", "html_markdown": "", "resume_markdown": ""}

    event_id = _find_latest_risk_event_id(
        platform=platform,
        keyword=keyword,
        stage=stage,
        screenshot_path=screenshot_path,
        html_path=html_path,
    )
    if not event_id:
        return {"screenshot_markdown": "", "html_markdown": "", "resume_markdown": ""}

    token_secret = _resolve_artifact_token_secret()
    ttl = max(60, int(settings.risk_artifact_token_ttl_seconds))
    screenshot_token, _ = generate_risk_artifact_token(event_id=event_id, kind="screenshot", secret=token_secret, ttl_seconds=ttl)
    html_token, _ = generate_risk_artifact_token(event_id=event_id, kind="html", secret=token_secret, ttl_seconds=ttl)
    resume_token, _ = generate_risk_artifact_token(event_id=event_id, kind="resume", secret=token_secret, ttl_seconds=ttl)
    screenshot_url = f"{base_url}/api/risk/events/{event_id}/artifact?kind=screenshot&token={screenshot_token}"
    html_url = f"{base_url}/api/risk/events/{event_id}/artifact?kind=html&token={html_token}"
    resume_url = f"{base_url}/api/login/ack?event_id={event_id}&token={resume_token}"
    return {
        "screenshot_markdown": f"[点击查看截图]({screenshot_url})",
        "html_markdown": f"[点击查看HTML快照]({html_url})",
        "resume_markdown": f"[我已登录，恢复抓取]({resume_url})",
    }


def _find_latest_risk_event_id(
    platform: str,
    keyword: str,
    stage: str,
    screenshot_path: str,
    html_path: str,
) -> int:
    db = SessionLocal()
    try:
        q = db.query(RiskEvent).filter(RiskEvent.platform == platform, RiskEvent.keyword == keyword)
        if screenshot_path:
            row = (
                q.filter(RiskEvent.screenshot_path == screenshot_path)
                .order_by(RiskEvent.detected_at.desc())
                .first()
            )
            if row:
                return int(row.id)
        if html_path:
            row = (
                q.filter(RiskEvent.html_path == html_path)
                .order_by(RiskEvent.detected_at.desc())
                .first()
            )
            if row:
                return int(row.id)
        row = q.filter(RiskEvent.stage == stage).order_by(RiskEvent.detected_at.desc()).first()
        if row:
            return int(row.id)
        row = q.order_by(RiskEvent.detected_at.desc()).first()
        return int(row.id) if row else 0
    finally:
        db.close()


def _resolve_artifact_token_secret() -> str:
    preferred = settings.risk_artifact_token_secret.strip()
    if preferred:
        return preferred
    db = SessionLocal()
    try:
        cfg = db.query(AppConfig).first()
        if cfg and cfg.api_key:
            return cfg.api_key.strip()
        return settings.api_key.strip()
    finally:
        db.close()


def _build_card_elements(
    keyword: str,
    items_by_platform: dict[str, list[ContentItem]],
    run_time: str,
) -> list[dict]:
    elements: list[dict] = []

    total = sum(len(v) for v in items_by_platform.values())
    platforms_str = "、".join(
        PLATFORM_NAMES.get(p, p) for p in items_by_platform if items_by_platform[p]
    )
    elements.append(
        {
            "tag": "markdown",
            "content": (
                f"共发现 **{total}** 条热门内容，覆盖平台：{platforms_str}\n"
                f"抓取时间：`{run_time}`"
            ),
        }
    )

    for platform, items in items_by_platform.items():
        if not items:
            continue

        platform_name = PLATFORM_NAMES.get(platform, platform)
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": f"**{platform_name}** （{len(items)} 条）"})

        lines = []
        for i, item in enumerate(items[:10], 1):
            metric_str = _format_count(item.likes)
            publish_time = _format_publish_time(item.publish_time)
            line = f"{i}. [{item.title}]({item.url})"
            if item.author:
                line += f" — {item.author}"
            if platform == "bilibili":
                line += f" | 🕒 {publish_time} | 👍 {metric_str} 点赞"
            else:
                line += f" | 🕒 {publish_time} | 👍 {metric_str}"
            lines.append(line)

        elements.append({"tag": "markdown", "content": "\n".join(lines)})

    return elements


def _format_count(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _format_publish_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "未知"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return text


def _get_runtime_cfg() -> tuple[str, str]:
    """返回 (chat_id, webhook_url)。"""
    db = SessionLocal()
    try:
        cfg = db.query(AppConfig).first()
        chat_id = str(getattr(cfg, "feishu_chat_id", "") or "").strip() if cfg else ""
        webhook_url = str(getattr(cfg, "feishu_webhook_url", "") or "").strip() if cfg else ""
    finally:
        db.close()

    if not chat_id:
        chat_id = settings.feishu_chat_id.strip()
    if not webhook_url:
        webhook_url = settings.feishu_webhook_url.strip()
    return chat_id, webhook_url


def _has_app_credentials() -> bool:
    return bool(settings.feishu_app_id.strip() and settings.feishu_app_secret.strip())


def _resolve_push_target() -> dict[str, str]:
    """
    发送目标优先级：
    1) 应用机器人（有 app_id/app_secret 且有 chat_id）
    2) Webhook（仅当未配置应用机器人凭证时）
    """
    chat_id, webhook_url = _get_runtime_cfg()
    if _has_app_credentials():
        if chat_id:
            return {"mode": "app", "chat_id": chat_id, "webhook_url": ""}
        logger.warning("Feishu app credentials exist but chat_id missing, skip webhook fallback to avoid wrong bot")
        return {"mode": "", "chat_id": "", "webhook_url": ""}

    if webhook_url:
        return {"mode": "webhook", "chat_id": "", "webhook_url": webhook_url}
    return {"mode": "", "chat_id": "", "webhook_url": ""}


async def _send_card(card: dict[str, Any]) -> bool:
    target = _resolve_push_target()
    mode = target.get("mode")
    if mode == "app":
        # 应用机器人 interactive 的 content 直接是卡片 JSON
        return await _post_app_message(target.get("chat_id", ""), "interactive", card)
    if mode == "webhook":
        payload = {"msg_type": "interactive", "card": card}
        return await _post_webhook_payload(target.get("webhook_url", ""), payload)
    logger.warning("Feishu push target missing, skip card message")
    return False


async def _send_text(text: str) -> bool:
    target = _resolve_push_target()
    mode = target.get("mode")
    if mode == "app":
        return await _post_app_message(target.get("chat_id", ""), "text", {"text": text})
    if mode == "webhook":
        payload = {"msg_type": "text", "content": {"text": text}}
        return await _post_webhook_payload(target.get("webhook_url", ""), payload)
    logger.warning("Feishu push target missing, skip text message")
    return False


async def _send_image(image_key: str) -> bool:
    if not image_key:
        return False
    target = _resolve_push_target()
    mode = target.get("mode")
    if mode == "app":
        return await _post_app_message(target.get("chat_id", ""), "image", {"image_key": image_key})
    if mode == "webhook":
        payload = {"msg_type": "image", "content": {"image_key": image_key}}
        return await _post_webhook_payload(target.get("webhook_url", ""), payload)
    logger.warning("Feishu push target missing, skip image message")
    return False


async def _post_webhook_payload(webhook_url: str, payload: dict[str, Any]) -> bool:
    if not webhook_url:
        return False
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(webhook_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return bool(data.get("code") == 0 or data.get("StatusCode") == 0)


async def _post_app_message(chat_id: str, msg_type: str, content_obj: dict[str, Any]) -> bool:
    chat = str(chat_id or "").strip()
    if not chat:
        logger.warning("Feishu app send skipped: chat_id missing")
        return False
    tenant_token = await _get_tenant_access_token()
    if not tenant_token:
        logger.warning("Feishu app send skipped: tenant_access_token missing")
        return False

    url = "https://open.feishu.cn/open-apis/im/v1/messages"
    payload = {
        "receive_id": chat,
        "msg_type": msg_type,
        "content": json.dumps(content_obj, ensure_ascii=False),
    }
    headers = {"Authorization": f"Bearer {tenant_token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, params={"receive_id_type": "chat_id"}, headers=headers, json=payload)
        body_text = resp.text
        if resp.status_code >= 300:
            logger.warning(
                "Feishu app send http failed: status=%s body=%s",
                resp.status_code,
                body_text[:1000],
            )
            return False
        try:
            data = resp.json()
        except Exception:
            logger.warning("Feishu app send parse json failed: body=%s", body_text[:1000])
            return False
        ok = int(data.get("code", -1)) == 0
        if not ok:
            logger.warning("Feishu app send business failed: %s", data)
        return ok


async def _get_tenant_access_token() -> str:
    now = time.time()
    cached = str(_TOKEN_CACHE.get("value") or "")
    expires_at = float(_TOKEN_CACHE.get("expires_at") or 0)
    if cached and now < expires_at - 30:
        return cached

    app_id = settings.feishu_app_id.strip()
    app_secret = settings.feishu_app_secret.strip()
    if not app_id or not app_secret:
        return ""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
            token = (
                data.get("tenant_access_token")
                or data.get("data", {}).get("tenant_access_token")
                or ""
            )
            expire = int(
                data.get("expire")
                or data.get("data", {}).get("expire")
                or 7200
            )
            if not token:
                logger.warning("Fetch tenant_access_token failed: %s", data)
                return ""
            _TOKEN_CACHE["value"] = token
            _TOKEN_CACHE["expires_at"] = now + max(60, expire)
            return str(token)
    except Exception:
        logger.exception("Fetch tenant_access_token error")
        return ""


async def _try_upload_image_and_get_key(image_path: str) -> str:
    image_file = Path(image_path.strip()) if image_path else None
    if not image_file or not image_file.exists() or not image_file.is_file():
        return ""

    tenant_token = await _get_tenant_access_token()
    if not tenant_token:
        return ""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            upload_resp = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {tenant_token}"},
                data={"image_type": "message"},
                files={
                    "image": (
                        image_file.name,
                        image_file.read_bytes(),
                        "image/png",
                    )
                },
            )
            upload_resp.raise_for_status()
            upload_data = upload_resp.json()
            if upload_data.get("code") != 0:
                logger.warning("Feishu image upload failed: %s", upload_data)
                return ""
            return str(upload_data.get("data", {}).get("image_key") or "").strip()
    except Exception:
        logger.exception("Feishu image upload error: path=%s", image_path)
        return ""
