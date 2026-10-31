"""
定时任务调度器

功能：
- 每日定时执行：遍历所有已启用的关键词，逐一在各平台搜索热门内容
- 搜索结果去重后写入数据库
- 汇总推送到飞书
"""

import asyncio
import json
import logging
import random
import uuid
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from database import AppConfig, Content, Keyword, PushLog, ScrapeTask, SessionLocal
from models import ContentItem
from push.feishu import push_to_feishu
from scrapers.registry import create_scrapers, supported_platforms
from scrapers.xiaohongshu import set_runtime_headless_override

logger = logging.getLogger(__name__)

SCRAPERS = create_scrapers()
_task_runner_lock = asyncio.Lock()
_task_runner_task: Optional[asyncio.Task] = None
_RUNNER_IDLE_SLEEP_SECONDS = 1.5
_RUNNER_BLOCKED_SLEEP_SECONDS = 3.0
_RUNNER_CRASH_RETRY_SLEEP_SECONDS = 2.0


def _get_or_create_app_config(db) -> AppConfig:
    cfg = db.query(AppConfig).first()
    if cfg:
        if not str(getattr(cfg, "feishu_chat_id", "") or "").strip() and settings.feishu_chat_id.strip():
            cfg.feishu_chat_id = settings.feishu_chat_id.strip()
            db.commit()
            db.refresh(cfg)
        return cfg
    cfg = AppConfig(
        api_key=settings.api_key,
        feishu_webhook_url=settings.feishu_webhook_url,
        feishu_chat_id=settings.feishu_chat_id,
        scrape_headless=bool(settings.scrape_headless),
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


def _load_dashboard_scrape_headless() -> bool:
    db = SessionLocal()
    try:
        cfg = _get_or_create_app_config(db)
        if getattr(cfg, "scrape_headless", None) is None:
            return bool(settings.scrape_headless)
        return bool(cfg.scrape_headless)
    finally:
        db.close()


def get_login_block_state() -> dict[str, Any]:
    db = SessionLocal()
    try:
        cfg = _get_or_create_app_config(db)
        return {
            "blocked": bool(getattr(cfg, "login_blocked", False)),
            "blocked_at": cfg.login_blocked_at.isoformat() + "Z" if getattr(cfg, "login_blocked_at", None) else "",
            "reason": str(getattr(cfg, "login_block_reason", "") or ""),
            "keyword": str(getattr(cfg, "login_block_keyword", "") or ""),
        }
    finally:
        db.close()


def set_login_blocked(blocked: bool, reason: str = "", keyword: str = "") -> dict[str, Any]:
    db = SessionLocal()
    try:
        cfg = _get_or_create_app_config(db)
        cfg.login_blocked = bool(blocked)
        if blocked:
            cfg.login_blocked_at = datetime.utcnow()
            cfg.login_block_reason = str(reason or "").strip()
            cfg.login_block_keyword = str(keyword or "").strip()
        else:
            cfg.login_blocked_at = None
            cfg.login_block_reason = ""
            cfg.login_block_keyword = ""
        db.commit()
    finally:
        db.close()
    return get_login_block_state()


async def run_scrape_job(
    keyword_ids: Optional[list[int]] = None,
    scrape_headless_override: Optional[bool] = None,
    trigger_source: str = "manual",
):
    """主抓取任务：遍历关键词 -> 各平台抓取 -> 入库 -> 推送飞书"""
    logger.info(
        "=== Scrape job started at %s (keyword_ids=%s, headless_override=%s) ===",
        datetime.now().isoformat(),
        keyword_ids or [],
        scrape_headless_override,
    )
    logger.info("Supported platforms: %s", ",".join(supported_platforms()))

    db = SessionLocal()
    summary = {
        "trigger_source": trigger_source,
        "requested_keyword_ids": sorted({int(i) for i in (keyword_ids or []) if int(i) > 0}),
        "resolved_keyword_ids": [],
        "scrape_headless": scrape_headless_override,
        "keywords_processed": 0,
        "total_saved": 0,
        "push_success_count": 0,
        "login_required_count": 0,
        "aborted_due_login_required": False,
        "aborted_after_keyword": "",
        "failed_keywords": [],
        "failed_log_ids": [],
        "max_attempts_used": 1,
    }
    try:
        selected_ids = summary["requested_keyword_ids"]
        if selected_ids:
            rows = db.query(Keyword).filter(Keyword.id.in_(selected_ids)).all()
            row_map = {r.id: r for r in rows}
            keywords = [row_map[kid] for kid in selected_ids if kid in row_map]
        else:
            keywords = db.query(Keyword).filter(Keyword.enabled == True).all()

        if not keywords:
            if selected_ids:
                logger.info("No matching keywords for selected ids=%s, skipping", selected_ids)
            else:
                logger.info("No enabled keywords, skipping")
            return summary

        summary["resolved_keyword_ids"] = [kw.id for kw in keywords]
        summary["keywords_processed"] = 0
        abort_remaining_keywords = False

        for idx, kw in enumerate(keywords):
            # 每个关键词执行前都重新决策模式：
            # - 若任务显式传了 override，则使用该值（兼容历史调用）
            # - 否则读取 Dashboard 当前配置，实现“中途改配置，下个关键词立即生效”
            if scrape_headless_override is None:
                effective_headless = _load_dashboard_scrape_headless()
                mode_source = "dashboard"
            else:
                effective_headless = bool(scrape_headless_override)
                mode_source = "override"
            set_runtime_headless_override(effective_headless)
            logger.info(
                "Keyword runtime mode resolved: keyword=%s headless=%s source=%s",
                kw.keyword,
                effective_headless,
                mode_source,
            )
            summary["keywords_processed"] += 1
            platforms = _parse_platforms_field(kw.platforms)
            all_items: dict[str, list[ContentItem]] = {}
            run_detail = {
                "keyword": kw.keyword,
                "keyword_id": kw.id,
                "min_likes": kw.min_likes,
                "platforms": platforms,
                "per_platform": {},
                "errors": [],
                "push_ok": False,
                "headless": bool(effective_headless),
                "headless_source": mode_source,
            }
            if not platforms:
                logger.warning(
                    "Keyword has no valid platforms, skipping scrape: keyword=%s",
                    kw.keyword,
                )
                run_detail["errors"].append(
                    {"platform": "", "error": "No valid platforms configured"}
                )

            for platform in platforms:
                scraper = SCRAPERS.get(platform)
                if not scraper:
                    logger.warning("No scraper for platform: %s, skipping", platform)
                    run_detail["errors"].append(
                        {"platform": platform, "error": "No scraper configured"}
                    )
                    run_detail["per_platform"][platform] = {
                        "found": 0,
                        "saved": 0,
                        "status": "failed",
                    }
                    continue

                logger.info("Scraping %s for keyword: %s", platform, kw.keyword)
                attempt = 1
                max_attempts = max(1, int(settings.scrape_retry_attempts))
                scrape_timeout = max(30, int(settings.scrape_keyword_timeout_seconds))
                result = None
                last_error = ""
                while attempt <= max_attempts:
                    try:
                        result = await asyncio.wait_for(
                            scraper.search_and_wrap(
                                kw.keyword, settings.scrape_max_results
                            ),
                            timeout=scrape_timeout,
                        )
                        last_error = ""
                        scraper_debug = (
                            scraper.last_search_debug
                            if hasattr(scraper, "last_search_debug")
                            and isinstance(scraper.last_search_debug, dict)
                            else {}
                        )
                        if scraper_debug.get("login_required"):
                            logger.warning(
                                "Login required detected, stop retry: platform=%s keyword=%s stage=%s",
                                platform,
                                kw.keyword,
                                scraper_debug.get("risk_stage", "-"),
                            )
                            break
                        if result.count > 0 or attempt == max_attempts:
                            break
                        logger.warning(
                            "Scrape empty result, retrying: platform=%s keyword=%s attempt=%d/%d",
                            platform,
                            kw.keyword,
                            attempt,
                            max_attempts,
                        )
                    except asyncio.TimeoutError:
                        last_error = f"Scraper timeout after {scrape_timeout}s"
                        logger.error(
                            "Scraper timeout: platform=%s keyword=%s timeout=%ss attempt=%d/%d",
                            platform,
                            kw.keyword,
                            scrape_timeout,
                            attempt,
                            max_attempts,
                        )
                        if attempt == max_attempts:
                            result = None
                            break
                    except Exception:
                        last_error = "Scraper execution failed"
                        logger.exception(
                            "Scraper failed: platform=%s, keyword=%s, attempt=%d/%d",
                            platform,
                            kw.keyword,
                            attempt,
                            max_attempts,
                        )
                        if attempt == max_attempts:
                            result = None
                            break
                    attempt += 1
                    retry_wait = max(2.0, settings.scrape_delay_max + 1.5)
                    await asyncio.sleep(retry_wait)

                if result is None:
                    all_items[platform] = []
                    run_detail["errors"].append(
                        {
                            "platform": platform,
                            "error": last_error or "Scraper execution failed",
                        }
                    )
                    run_detail["per_platform"][platform] = {
                        "found": 0,
                        "saved": 0,
                        "status": "failed",
                        "attempts": max_attempts,
                    }
                    summary["max_attempts_used"] = max(
                        int(summary.get("max_attempts_used") or 1),
                        int(max_attempts),
                    )
                else:
                    saved_items = _save_to_db(db, result.items, kw.min_likes)
                    all_items[platform] = saved_items
                    scraper_debug = (
                        scraper.last_search_debug
                        if hasattr(scraper, "last_search_debug")
                        and isinstance(scraper.last_search_debug, dict)
                        else {}
                    )
                    logger.info(
                        "  -> %d items found, %d saved (after dedup & min_likes filter)",
                        result.count,
                        len(saved_items),
                    )
                    run_detail["per_platform"][platform] = {
                        "found": result.count,
                        "saved": len(saved_items),
                        "status": "success" if result.count > 0 else "empty",
                        "attempts": attempt,
                        "debug": scraper_debug,
                    }
                    summary["max_attempts_used"] = max(
                        int(summary.get("max_attempts_used") or 1),
                        int(attempt),
                    )
                    if scraper_debug.get("login_required"):
                        run_detail["errors"].append(
                            {
                                "platform": platform,
                                "error": "Login required",
                                "stage": scraper_debug.get("risk_stage", ""),
                                "flags": scraper_debug.get("risk_flags", []),
                                "screenshot": scraper_debug.get("risk_screenshot", ""),
                            }
                        )
                        run_detail["per_platform"][platform]["status"] = "failed"
                        logger.warning(
                            "Login required detected, abort remaining platforms in keyword: keyword=%s platform=%s",
                            kw.keyword,
                            platform,
                        )
                        break

            total_saved = sum(len(v) for v in all_items.values())
            summary["total_saved"] += total_saved
            has_login_required = any(
                str(err.get("error", "")).lower() == "login required"
                for err in run_detail["errors"]
            )
            if has_login_required:
                logger.warning(
                    "Skip normal feishu push due login-required: keyword=%s",
                    kw.keyword,
                )
                push_ok = False
                summary["login_required_count"] += 1
                summary["aborted_due_login_required"] = True
                summary["aborted_after_keyword"] = kw.keyword
                abort_remaining_keywords = True
                set_login_blocked(
                    blocked=True,
                    reason="login_required",
                    keyword=kw.keyword,
                )
            else:
                push_ok = await push_to_feishu(kw.keyword, all_items)
                if push_ok:
                    summary["push_success_count"] += 1
            run_detail["push_ok"] = push_ok

            # 改进状态判断逻辑
            if run_detail["errors"]:
                # 有错误，即使有内容也是部分成功
                status = "partial" if total_saved > 0 else "failed"
            elif total_saved == 0:
                # 没有内容，检查是否有抓取失败
                has_failures = any(
                    p.get("status") == "failed" for p in run_detail["per_platform"].values()
                )
                status = "failed" if has_failures else "success"
            elif not push_ok:
                # 有内容但推送失败
                status = "failed"
            else:
                # 有内容且推送成功
                status = "success"
            log = PushLog(
                keyword=kw.keyword,
                content_count=total_saved,
                status=status,
                detail=json.dumps(run_detail, ensure_ascii=False),
            )
            db.add(log)
            db.commit()
            db.refresh(log)
            if status == "failed":
                summary["failed_keywords"].append(kw.keyword)
                summary["failed_log_ids"].append(int(log.id))

            if abort_remaining_keywords:
                logger.warning(
                    "Abort remaining keywords due login-required risk: current=%s, remaining=%d",
                    kw.keyword,
                    max(0, len(keywords) - idx - 1),
                )
                break

            # 关键词之间增加冷却，降低连续请求触发风控的概率
            if idx < len(keywords) - 1:
                cooldown = random.uniform(
                    max(2.0, settings.scrape_delay_min),
                    max(4.0, settings.scrape_delay_max + 1.5),
                )
                logger.info(
                    "Cooldown between keywords: %.1fs (after keyword=%s)",
                    cooldown,
                    kw.keyword,
                )
                await asyncio.sleep(cooldown)

    except Exception:
        logger.exception("Scrape job failed")
        raise
    finally:
        set_runtime_headless_override(None)
        db.close()

    logger.info("=== Scrape job finished ===")
    return summary


def enqueue_scrape_task(
    trigger_source: str = "manual",
    keyword_ids: Optional[list[int]] = None,
    scrape_headless: Optional[bool] = None,
    request_id: str = "",
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        requested_ids = sorted({int(i) for i in (keyword_ids or []) if int(i) > 0})
        task_id = uuid.uuid4().hex
        row = ScrapeTask(
            task_id=task_id,
            trigger_source=trigger_source,
            status="queued",
            requested_keyword_ids=json.dumps(requested_ids, ensure_ascii=False),
            resolved_keyword_ids="[]",
            scrape_headless=scrape_headless,
            request_id=request_id or "",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        position = (
            db.query(ScrapeTask)
            .filter(ScrapeTask.status == "queued", ScrapeTask.id <= row.id)
            .count()
        )
        return {
            "task_id": row.task_id,
            "status": row.status,
            "queued_at": row.queued_at,
            "position": int(position),
        }
    finally:
        db.close()


def get_scrape_task(task_id: str) -> Optional[ScrapeTask]:
    db = SessionLocal()
    try:
        return db.query(ScrapeTask).filter(ScrapeTask.task_id == str(task_id)).first()
    finally:
        db.close()


def list_scrape_tasks(status: str = "", limit: int = 20) -> list[ScrapeTask]:
    db = SessionLocal()
    try:
        q = db.query(ScrapeTask)
        if status:
            q = q.filter(ScrapeTask.status == status)
        rows = (
            q.order_by(ScrapeTask.queued_at.desc())
            .limit(max(1, min(int(limit), 200)))
            .all()
        )
        return rows
    finally:
        db.close()


def _recover_orphan_running_tasks() -> int:
    """
    进程异常退出后，数据库中可能残留 running 任务，导致状态长期卡住。
    在 runner 启动前做一次自愈：将这些孤儿任务标记为 failed。
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(ScrapeTask)
            .filter(ScrapeTask.status == "running")
            .order_by(ScrapeTask.id.asc())
            .all()
        )
        if not rows:
            return 0

        now = datetime.utcnow()
        task_ids: list[str] = []
        for row in rows:
            task_ids.append(str(row.task_id))
            row.status = "failed"
            row.finished_at = now
            if not str(row.error_code or "").strip():
                row.error_code = "RUNNER_RECOVERED"
            if not str(row.error_message or "").strip():
                row.error_message = (
                    "Task was left in running state by a previous runner process; "
                    "marked failed during runner bootstrap."
                )
        db.commit()
        logger.warning(
            "Recovered orphan running tasks: count=%d task_ids=%s",
            len(rows),
            ",".join(task_ids),
        )
        return len(rows)
    finally:
        db.close()


def ensure_task_runner_started() -> None:
    global _task_runner_task
    if _task_runner_task and not _task_runner_task.done():
        return

    recovered = _recover_orphan_running_tasks()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
        except Exception:
            logger.warning("Task runner not started: event loop unavailable")
            return

    if not loop.is_running():
        logger.warning("Task runner not started: event loop not running")
        return

    _task_runner_task = loop.create_task(_run_task_queue(), name="scrape-task-runner")
    logger.info(
        "Task runner started: recovered_orphan_running_tasks=%d",
        int(recovered),
    )


def get_task_runner_state() -> dict[str, Any]:
    db = SessionLocal()
    try:
        queued_count = db.query(ScrapeTask).filter(ScrapeTask.status == "queued").count()
        running_row = (
            db.query(ScrapeTask)
            .filter(ScrapeTask.status == "running")
            .order_by(ScrapeTask.started_at.desc())
            .first()
        )
        return {
            "runner_task_alive": bool(_task_runner_task and not _task_runner_task.done()),
            "runner_lock_locked": bool(_task_runner_lock.locked()),
            "queued_count": int(queued_count),
            "running_task_id": (running_row.task_id if running_row else ""),
        }
    finally:
        db.close()


async def _run_task_queue() -> None:
    if _task_runner_lock.locked():
        return
    async with _task_runner_lock:
        paused_by_login = False
        while True:
            try:
                lock_state = get_login_block_state()
                if lock_state.get("blocked"):
                    if not paused_by_login:
                        logger.warning(
                            "Task runner paused by login block: keyword=%s reason=%s",
                            lock_state.get("keyword", ""),
                            lock_state.get("reason", ""),
                        )
                    paused_by_login = True
                    await asyncio.sleep(_RUNNER_BLOCKED_SLEEP_SECONDS)
                    continue
                if paused_by_login:
                    logger.info("Task runner resumed after login block cleared")
                    paused_by_login = False

                db = SessionLocal()
                row = None
                try:
                    row = (
                        db.query(ScrapeTask)
                        .filter(ScrapeTask.status == "queued")
                        .order_by(ScrapeTask.id.asc())
                        .first()
                    )
                    if not row:
                        await asyncio.sleep(_RUNNER_IDLE_SLEEP_SECONDS)
                        continue
                    row.status = "running"
                    row.started_at = datetime.utcnow()
                    db.commit()
                    db.refresh(row)
                    task_id = row.task_id
                    requested_ids = _safe_parse_int_list(row.requested_keyword_ids)
                    headless = row.scrape_headless
                    trigger_source = row.trigger_source
                finally:
                    db.close()

                logger.info("Task runner executing task_id=%s source=%s", task_id, trigger_source)
                run_status = "success"
                error_code = ""
                error_message = ""
                summary: dict[str, Any] = {}
                try:
                    summary = await run_scrape_job(
                        keyword_ids=requested_ids or None,
                        scrape_headless_override=headless,
                        trigger_source=trigger_source,
                    )
                    # 任务级状态应与业务语义一致，避免“登录中断但显示 success”
                    if bool(summary.get("aborted_due_login_required")):
                        run_status = "failed"
                        error_code = "LOGIN_REQUIRED"
                        error_message = (
                            "Scrape aborted due to login-required risk: "
                            f"keyword={summary.get('aborted_after_keyword') or '-'}"
                        )
                    else:
                        failed_keywords = list(summary.get("failed_keywords") or [])
                        if failed_keywords:
                            total_saved = int(summary.get("total_saved") or 0)
                            run_status = "partial" if total_saved > 0 else "failed"
                            if run_status == "failed":
                                error_code = "KEYWORD_FAILED"
                                error_message = (
                                    "All processed keywords failed: "
                                    + ",".join(str(k) for k in failed_keywords)
                                )
                except Exception as exc:
                    run_status = "failed"
                    error_code = "SCRAPE_EXEC_FAILED"
                    error_message = str(exc)
                    logger.exception("Task runner failed: task_id=%s", task_id)

                db = SessionLocal()
                try:
                    row = db.query(ScrapeTask).filter(ScrapeTask.task_id == task_id).first()
                    if row is None:
                        continue
                    row.status = run_status
                    row.finished_at = datetime.utcnow()
                    row.error_code = error_code
                    row.error_message = error_message
                    if summary:
                        row.resolved_keyword_ids = json.dumps(
                            summary.get("resolved_keyword_ids") or [],
                            ensure_ascii=False,
                        )
                        row.result_summary = json.dumps(summary, ensure_ascii=False)
                    db.commit()
                finally:
                    db.close()
            except asyncio.CancelledError:
                logger.info("Task runner cancelled")
                raise
            except Exception:
                logger.exception("Task runner loop crashed, retrying")
                await asyncio.sleep(_RUNNER_CRASH_RETRY_SLEEP_SECONDS)


async def enqueue_scheduled_scrape_task() -> None:
    lock_state = get_login_block_state()
    if lock_state.get("blocked"):
        logger.warning(
            "Skip scheduled enqueue due login block: keyword=%s reason=%s",
            lock_state.get("keyword", ""),
            lock_state.get("reason", ""),
        )
        return
    enqueue_scrape_task(trigger_source="schedule", keyword_ids=None, scrape_headless=None)
    ensure_task_runner_started()


def _save_to_db(
    db, items: list[ContentItem], min_likes: int = 0
) -> list[ContentItem]:
    saved = []
    for item in items:
        if item.likes < min_likes:
            continue
        existing = _find_existing_content(db, item)
        if existing:
            if item.url and existing.url != item.url:
                existing.url = item.url
            if item.likes > existing.likes:
                existing.likes = item.likes
                existing.comments = item.comments
                existing.shares = item.shares
            if item.publish_time:
                existing.publish_time = item.publish_time
            if item.video_url:
                existing.video_url = item.video_url
            db.commit()
            continue

        content = Content(
            platform=item.platform,
            title=item.title,
            author=item.author,
            url=item.url,
            thumbnail=item.thumbnail,
            likes=item.likes,
            comments=item.comments,
            shares=item.shares,
            publish_time=item.publish_time,
            video_url=item.video_url,
            keyword=item.keyword,
            source=item.source,
        )
        db.add(content)
        saved.append(item)

    db.commit()
    return saved


def _find_existing_content(db, item: ContentItem):
    # 小红书链接可能因 xsec_token 不同而变化，按 note_id 做去重更稳
    if item.platform == "xiaohongshu":
        note_id = _extract_xhs_note_id(item.url)
        if note_id:
            return (
                db.query(Content)
                .filter(
                    Content.platform == item.platform,
                    Content.url.like(f"%/explore/{note_id}%"),
                )
                .first()
            )
    return (
        db.query(Content)
        .filter(Content.platform == item.platform, Content.url == item.url)
        .first()
    )


def _extract_xhs_note_id(url: str) -> str:
    try:
        path = urlparse(str(url or "")).path
    except Exception:
        return ""
    marker = "/explore/"
    idx = path.find(marker)
    if idx < 0:
        return ""
    return path[idx + len(marker):].strip("/")


def _parse_platforms_field(raw: str) -> list[str]:
    try:
        platforms = json.loads(raw)
    except Exception:
        return []
    if not isinstance(platforms, list):
        return []
    normalized: list[str] = []
    for p in platforms:
        name = str(p).strip().lower()
        if not name or name in normalized:
            continue
        normalized.append(name)
    return normalized


def _safe_parse_int_list(raw: str) -> list[int]:
    try:
        values = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(values, list):
        return []
    cleaned: list[int] = []
    for val in values:
        try:
            i = int(val)
            if i > 0:
                cleaned.append(i)
        except Exception:
            continue
    return sorted(set(cleaned))


def setup_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler()


def configure_daily_scrapes(
    scheduler: AsyncIOScheduler, enabled: bool, slots: list[tuple[int, int]]
) -> None:
    for job in scheduler.get_jobs():
        if job.id == "daily_scrape" or job.id.startswith("daily_scrape_"):
            scheduler.remove_job(job.id)

    if not enabled:
        logger.info("Daily scrape disabled")
        return

    for hour, minute in slots:
        job_id = f"daily_scrape_{hour:02d}_{minute:02d}"
        scheduler.add_job(
            enqueue_scheduled_scrape_task,
            "cron",
            hour=hour,
            minute=minute,
            id=job_id,
            replace_existing=True,
        )
        logger.info("Scheduled daily scrape at %02d:%02d", hour, minute)
