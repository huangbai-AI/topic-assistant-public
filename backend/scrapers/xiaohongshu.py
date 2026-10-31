"""
小红书 Scraper（稳定版）

目标：
1. 优先尝试“筛选 -> 最多点赞 + 一周内”UI 操作
2. 即便 UI 失败，也进行代码侧“一周内”兜底过滤
3. 自动加载并刷新 Cookie，避免频繁手动登录
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote_plus, urlparse

from playwright.async_api import Page, async_playwright

from config import settings
from database import AppConfig, SessionLocal
from models import ContentItem
from push.feishu import push_login_risk_alert
from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

COOKIE_FILE = settings.cookie_dir / "xiaohongshu.json"
XHS_SEARCH_URL = (
    "https://www.xiaohongshu.com/search_result?keyword={keyword}"
    "&type=51&sort=popularity_descending"
)


_RUNTIME_HEADLESS_OVERRIDE: Optional[bool] = None


def set_runtime_headless_override(value: Optional[bool]) -> None:
    global _RUNTIME_HEADLESS_OVERRIDE
    _RUNTIME_HEADLESS_OVERRIDE = value


def _runtime_scrape_headless() -> bool:
    if _RUNTIME_HEADLESS_OVERRIDE is not None:
        return bool(_RUNTIME_HEADLESS_OVERRIDE)
    db = SessionLocal()
    try:
        cfg = db.query(AppConfig).first()
        if cfg is not None and getattr(cfg, "scrape_headless", None) is not None:
            return bool(cfg.scrape_headless)
        return bool(settings.scrape_headless)
    except Exception:
        logger.exception("Load runtime scrape_headless failed, fallback to env")
        return bool(settings.scrape_headless)
    finally:
        db.close()


class XiaohongshuScraperUIBackup(BaseScraper):
    platform = "xiaohongshu"

    async def search(self, keyword: str, max_results: int = 20) -> list[ContentItem]:
        items: list[ContentItem] = []
        runtime_headless = _runtime_scrape_headless()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=runtime_headless)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )

            await self._load_cookies(context)
            page = await context.new_page()

            try:
                url = XHS_SEARCH_URL.format(keyword=quote(keyword))
                logger.info("Navigating to: %s", url)
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(3000)
                # 防止被重定向到首页后误抓“推荐/主页”内容
                if "search_result" not in (page.url or ""):
                    logger.warning(
                        "Unexpected page after search navigation, skip UI backup extraction: url=%s",
                        page.url,
                    )
                    return []

                await self._apply_filters(page)
                items = await self._extract_notes(page, keyword, max_results)

            except Exception:
                logger.exception("Failed to scrape xiaohongshu for keyword: %s", keyword)
            finally:
                await self._save_cookies(context)
                await browser.close()

        logger.info(
            "Xiaohongshu scrape done: keyword=%s, found=%d items", keyword, len(items)
        )
        return items

    async def _load_cookies(self, context) -> None:
        if not COOKIE_FILE.exists():
            logger.warning(
                "No cookie file found at %s. Run `python -m scrapers.xiaohongshu` first.",
                COOKIE_FILE,
            )
            return

        try:
            cookies = json.loads(COOKIE_FILE.read_text())
            now_ts = datetime.now().timestamp()
            valid = [
                c for c in cookies if c.get("expires", -1) == -1 or c.get("expires", 0) > now_ts
            ]
            if not valid:
                logger.warning("All cookies expired, please login again")
                return

            await context.add_cookies(valid)
            # 预热页面失败不应影响 cookie 注入，否则会出现“明明有 cookie 但没加载”的问题
            try:
                bootstrap = await context.new_page()
                await bootstrap.goto(
                    "https://www.xiaohongshu.com",
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                await bootstrap.close()
            except Exception as warm_err:
                logger.warning("Cookie bootstrap warmup failed, continue anyway: %s", str(warm_err))
            logger.info(
                "Loaded %d valid cookies from %s (filtered %d expired)",
                len(valid),
                COOKIE_FILE,
                len(cookies) - len(valid),
            )
        except Exception as e:
            logger.warning("Failed to load cookies: %s", str(e))

    async def _save_cookies(self, context) -> None:
        try:
            updated = await context.cookies()
            if updated:
                existing = []
                if COOKIE_FILE.exists():
                    try:
                        loaded = json.loads(COOKIE_FILE.read_text())
                        if isinstance(loaded, list):
                            existing = loaded
                    except Exception:
                        existing = []
                if (not self._has_auth_cookies(updated)) and self._has_auth_cookies(existing):
                    logger.warning(
                        "Skip cookie overwrite: updated cookies missing auth markers while existing has them"
                    )
                    return
                COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
                COOKIE_FILE.write_text(json.dumps(updated, ensure_ascii=False, indent=2))
                logger.info("Updated cookies saved to %s (%d cookies)", COOKIE_FILE, len(updated))
        except Exception as e:
            logger.warning("Failed to save updated cookies: %s", str(e))

    @staticmethod
    def _has_auth_cookies(cookies: list[dict[str, Any]]) -> bool:
        if not cookies:
            return False
        names = {str(c.get("name") or "").strip() for c in cookies}
        auth_keys = {"web_session", "a1", "webId"}
        return any(k in names for k in auth_keys)

    async def _apply_filters(self, page: Page) -> bool:
        """
        严格按你的操作顺序：
        1) 点击“筛选”按钮
        2) 在下拉菜单选“最多点赞”
        3) 在下拉菜单选“一周内”
        """
        logger.info("Looking for '筛选' button...")
        await page.wait_for_timeout(1500)

        clicked_filter = await self._click_text(page, "筛选", exclude="已筛选")
        if clicked_filter:
            logger.info("Clicked '筛选' button")
            await page.wait_for_timeout(1500)
        else:
            logger.warning("Could not click '筛选' button")
            return False

        logger.info("Looking for '最多点赞' option in dropdown...")
        sort_ok = await self._click_text(page, "最多点赞")

        await page.wait_for_timeout(600)
        logger.info("Looking for '一周内' option in dropdown...")
        time_ok = await self._click_text(page, "一周内")

        if sort_ok and time_ok:
            logger.info("Filters applied successfully: sort=最多点赞, time=一周内")
            await page.wait_for_timeout(2000)
            return True
        else:
            logger.warning("Filter clicked, but options not fully applied: sort=%s, time=%s", sort_ok, time_ok)
            return False

    async def _click_text(self, page: Page, text: str, exclude: str = "") -> bool:
        # JS 优先：更接近“你看到什么就点什么”
        js = """
            (target, excluded) => {
                const nodes = Array.from(document.querySelectorAll('button, div, span, a'));
                for (const n of nodes) {
                    const t = (n.innerText || n.textContent || '').trim();
                    if (!t) continue;
                    if (excluded && t.includes(excluded)) continue;
                    if (t === target || t.includes(target)) {
                        const style = window.getComputedStyle(n);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        n.click();
                        return true;
                    }
                }
                return false;
            }
        """
        try:
            if await page.evaluate(js, text, exclude):
                await page.wait_for_timeout(300)
                return True
        except Exception:
            pass

        # XPath 回退
        xpaths = [
            f'//*[normalize-space(text())="{text}"]',
            f'//*[contains(normalize-space(text()), "{text}")]',
        ]
        for xp in xpaths:
            try:
                elements = await page.query_selector_all(f"xpath={xp}")
                for el in elements:
                    try:
                        if await el.is_visible():
                            txt = (await el.inner_text()).strip()
                            if exclude and exclude in txt:
                                continue
                            await el.scroll_into_view_if_needed()
                            await page.wait_for_timeout(200)
                            await el.click()
                            await page.wait_for_timeout(300)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    async def _extract_notes(
        self, page: Page, keyword: str, max_results: int
    ) -> list[ContentItem]:
        items: list[ContentItem] = []

        scroll_count = max(15, max_results // 2)
        logger.info("Starting to scroll and load content (scroll_count=%d)", scroll_count)

        for _ in range(scroll_count):
            cards = await page.query_selector_all('a[href*="/explore/"]')
            if len(cards) >= max_results * 2:
                logger.info("Loaded enough cards (%d), stopping scroll", len(cards))
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(900)

        links = await page.query_selector_all('a[href*="/explore/"]')
        logger.info("Found %d links with /explore/ pattern", len(links))

        seen_urls = set()
        for link in links:
            try:
                item = await self._parse_card(link, keyword)
                if not item:
                    continue
                if item.url in seen_urls:
                    continue
                # UI 筛选失败时，代码兜底最近一周
                if not self._is_recent_week(item.publish_time or item.author, strict_unknown=True):
                    continue
                seen_urls.add(item.url)
                items.append(item)
            except Exception:
                continue

        items.sort(key=lambda x: x.likes, reverse=True)
        logger.info(
            "Extracted %d items (after dedup + recent-week fallback), top likes: %s",
            len(items),
            items[0].likes if items else 0,
        )
        return items[:max_results]

    async def _parse_card(self, link_el, keyword: str) -> Optional[ContentItem]:
        href = await link_el.get_attribute("href")
        if not href:
            return None
        if href.startswith("/"):
            href = "https://www.xiaohongshu.com" + href

        container = await link_el.evaluate_handle(
            "el => el.closest('section, article, div[class*=item], div[class*=card], div[class*=note]') || el.parentElement"
        )
        if not container:
            return None

        title = ""
        author = ""
        likes = 0
        thumbnail = ""

        try:
            title_el = await container.query_selector(
                'span.title, .note-title, [class*="title"], .desc'
            )
            if title_el:
                title = (await title_el.inner_text()).strip()
        except Exception:
            pass

        try:
            author_el = await container.query_selector(
                '.author, .nickname, [class*="author"], [class*="name"]'
            )
            if author_el:
                author = (await author_el.inner_text()).strip()
        except Exception:
            pass

        try:
            img_el = await container.query_selector("img")
            if img_el:
                thumbnail = await img_el.get_attribute("src") or ""
        except Exception:
            pass

        try:
            text = (await container.inner_text()).strip()
            likes = self._extract_likes(text)
        except Exception:
            text = ""
            likes = 0

        publish_hint = self._extract_time_hint(f"{author}\n{text}")

        return ContentItem(
            platform=self.platform,
            title=title or "(无标题)",
            author=author,
            url=href,
            thumbnail=thumbnail,
            likes=likes,
            publish_time=publish_hint,
            keyword=keyword,
            source="auto",
        )

    @staticmethod
    def _extract_likes(text: str) -> int:
        # 优先万/w/k格式
        patterns = [
            r"(\d+(?:\.\d+)?)\s*[万wW]",
            r"(\d+(?:\.\d+)?)\s*[kK]",
            r"(\d{2,7})",
        ]
        for p in patterns:
            m = re.search(p, text)
            if not m:
                continue
            num = float(m.group(1))
            if "万" in m.group(0) or "w" in m.group(0).lower():
                return int(num * 10000)
            if "k" in m.group(0).lower():
                return int(num * 1000)
            return int(num)
        return 0

    @staticmethod
    def _extract_time_hint(text: str) -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        # 优先抓相对时间
        rel = re.search(r"(刚刚|今天|昨天|前天|\d+\s*分钟前|\d+\s*小时前|\d+\s*天前)", raw)
        if rel:
            return rel.group(1)
        # 再抓绝对日期
        full = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if full:
            return full.group(1)
        short = re.search(r"(\d{1,2}-\d{1,2})", raw)
        if short:
            return short.group(1)
        return ""

    @staticmethod
    def _is_recent_week(author_text: str, strict_unknown: bool = False) -> bool:
        """
        author_text 常见形态：
        - xxx\\n2天前
        - xxx\\n2026-02-18
        - xxx\\n刚刚/小时前/分钟前
        """
        if not author_text:
            return not strict_unknown

        text = author_text.strip()
        now = datetime.now()

        if "今天" in text:
            return True
        if "昨天" in text:
            return True
        if "前天" in text:
            return True
        if any(k in text for k in ["刚刚", "分钟前", "小时前"]):
            return True

        m = re.search(r"(\d+)\s*天前", text)
        if m:
            return int(m.group(1)) <= 7

        d = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if d:
            try:
                dt = datetime.strptime(d.group(1), "%Y-%m-%d")
                return (now - dt) <= timedelta(days=7)
            except ValueError:
                return not strict_unknown

        d2 = re.search(r"(\d{1,2})-(\d{1,2})", text)
        if d2:
            try:
                month = int(d2.group(1))
                day = int(d2.group(2))
                year = now.year
                dt = datetime(year, month, day)
                # 跨年场景：例如 12-31 在 1 月展示，应视为上一年
                if dt > now + timedelta(days=1):
                    dt = datetime(year - 1, month, day)
                return (now - dt) <= timedelta(days=7)
            except ValueError:
                return not strict_unknown

        # 无法识别时间时，严格模式直接丢弃，避免把主页旧内容误判为“近一周”
        return not strict_unknown


class XiaohongshuScraper(BaseScraper):
    """
    新方案：接口优先抓取（默认）
    - 优先解析 XHR/FETCH 响应中的结构化数据
    - 自动应用“一周内”过滤
    - 点赞按降序排序
    - 若接口提取失败，回退到 UI 备份方案
    """

    platform = "xiaohongshu"

    def __init__(self):
        self._ui_backup = XiaohongshuScraperUIBackup()
        self._target_sort_tags = {"popularity_descending", "most_like", "most_liked"}
        self._target_time_tags = {"一周内", "week", "within_week", "7d"}
        self._last_parse_meta: dict[str, int] = {"payloads": 0, "parsed_candidates": 0}
        self._forced_request_count = 0
        self._last_login_alert_fp = ""
        self._last_login_alert_ts = 0.0
        self._login_alert_counts: dict[str, int] = {}
        self._wait_login_recover_after_alert = False
        self.last_search_debug: dict[str, Any] = {}

    def enable_login_recover_wait(self, enabled: bool) -> None:
        self._wait_login_recover_after_alert = bool(enabled)

    async def search(self, keyword: str, max_results: int = 20) -> list[ContentItem]:
        runtime_headless = _runtime_scrape_headless()
        payloads: list[dict[str, Any]] = []
        strict_payloads: list[dict[str, Any]] = []
        pending_tasks: list[asyncio.Task] = []
        forced_before = self._forced_request_count
        self.last_search_debug = {
            "mode": "api_first",
            "keyword": keyword,
            "payloads": 0,
            "strict_payloads": 0,
            "forced_request_count": 0,
            "ui_filter_full_applied": False,
            "source": "",
            "fallback": False,
            "login_required": False,
            "login_alert_sent": False,
            "risk_stage": "",
            "risk_flags": [],
            "risk_screenshot": "",
        }

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=runtime_headless)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )

            await self._ui_backup._load_cookies(context)
            page = await context.new_page()
            await page.route(
                "**/api/sns/web/v1/search/notes*",
                self._force_search_notes_request_filters,
            )

            def on_response(resp):
                if resp.request.resource_type not in ("xhr", "fetch"):
                    return
                if "xiaohongshu.com" not in resp.url:
                    return
                task = asyncio.create_task(
                    self._capture_json_response(resp, payloads, strict_payloads)
                )
                pending_tasks.append(task)

            page.on("response", on_response)

            try:
                url = XHS_SEARCH_URL.format(keyword=quote(keyword))
                logger.info("API-first navigating to: %s", url)
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(2500)
                flags = await self._detect_login_or_risk(page)
                if flags:
                    logger.warning(
                        "Possible login/risk page before capture: keyword=%s flags=%s",
                        keyword,
                        ",".join(flags),
                    )
                    if self._looks_like_login_required(flags):
                        recovered = await self._handle_login_required(page, keyword, "pre_capture", flags)
                        if not recovered:
                            await browser.close()
                            return []
                        # 扫码恢复后刷新页面，继续本轮抓取流程
                        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                        await page.wait_for_timeout(1500)
                        flags_after_recover = await self._detect_login_or_risk(page)
                        if self._looks_like_login_required(flags_after_recover):
                            await browser.close()
                            return []
                    else:
                        await self._dump_debug_artifacts(page, keyword, "pre_capture", flags)

                # 主动触发“最多点赞 + 一周内”筛选，促使后续 search/notes 请求带上明确 filters
                try:
                    ui_filter_ok = await self._ui_backup._apply_filters(page)
                    self.last_search_debug["ui_filter_full_applied"] = bool(ui_filter_ok)
                except Exception:
                    logger.exception("Failed to apply UI filters in API-first mode")

                # 滚动以触发更多接口请求
                for _ in range(max(10, max_results // 2)):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(800)

                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)

                source_payloads = strict_payloads
                if not source_payloads:
                    logger.warning(
                        "No strict filtered payload found, fallback to broad search/notes payloads"
                    )
                    source_payloads = payloads
                    self.last_search_debug["source"] = "broad"
                else:
                    self.last_search_debug["source"] = "strict"

                items = self._extract_items_from_payloads(
                    source_payloads,
                    keyword,
                    require_publish_time=not bool(strict_payloads),
                )
                items.sort(key=lambda x: x.likes, reverse=True)
                items = items[:max_results]
                self.last_search_debug["payloads"] = len(payloads)
                self.last_search_debug["strict_payloads"] = len(strict_payloads)
                self.last_search_debug["forced_request_count"] = (
                    self._forced_request_count - forced_before
                )

                logger.info(
                    "API-first parsed %d items from strict=%d / broad=%d payloads",
                    len(items),
                    len(strict_payloads),
                    len(payloads),
                )

                if items:
                    await self._ui_backup._save_cookies(context)
                    await browser.close()
                    return items

                logger.warning("API-first yielded 0 items, fallback to UI backup")
                self.last_search_debug["fallback"] = True
                self.last_search_debug["source"] = "ui_backup"
                flags = await self._detect_login_or_risk(page)
                if flags:
                    logger.warning(
                        "Possible login/risk page when API-first empty: keyword=%s flags=%s",
                        keyword,
                        ",".join(flags),
                    )
                    if self._looks_like_login_required(flags):
                        recovered = await self._handle_login_required(page, keyword, "api_empty", flags)
                        if not recovered:
                            await browser.close()
                            return []
                    else:
                        await self._dump_debug_artifacts(page, keyword, "api_empty", flags)
                await self._ui_backup._save_cookies(context)
                await browser.close()
                items = await self._ui_backup.search(keyword, max_results)
                self.last_search_debug["result_count"] = len(items)
                return items

            except Exception:
                logger.exception("API-first scrape failed, fallback to UI backup")
                self.last_search_debug["fallback"] = True
                self.last_search_debug["source"] = "ui_backup_on_exception"
                try:
                    await self._ui_backup._save_cookies(context)
                except Exception:
                    pass
                await browser.close()
                items = await self._ui_backup.search(keyword, max_results)
                self.last_search_debug["result_count"] = len(items)
                return items

    async def _capture_json_response(
        self,
        response,
        payloads: list[dict[str, Any]],
        strict_payloads: list[dict[str, Any]],
    ) -> None:
        if "/api/sns/web/v1/search/notes" not in response.url:
            return

        try:
            data = await response.json()
        except Exception:
            return
        if not isinstance(data, dict):
            return

        sort_tag, time_tag = self._extract_sort_time_from_request(response.request)
        logger.info(
            "search/notes filters detected: sort_type=%s, filter_note_time=%s",
            sort_tag or "-",
            time_tag or "-",
        )
        if not sort_tag and not time_tag:
            raw_preview = (response.request.post_data or "").strip()
            if raw_preview:
                logger.info("search/notes raw request preview: %s", raw_preview[:240])

        payloads.append(data)
        if self._is_target_filter_combo(sort_tag, time_tag):
            strict_payloads.append(data)

    def _extract_sort_time_from_request(self, request) -> tuple[str, str]:
        sort_tag = ""
        time_tag = ""

        body = self._extract_request_body(request)
        filters = []
        if isinstance(body, dict):
            f = body.get("filters")
            if isinstance(f, list):
                filters = f
            elif isinstance(body.get("search"), dict) and isinstance(body["search"].get("filters"), list):
                filters = body["search"]["filters"]
            elif isinstance(body.get("search_req"), dict) and isinstance(body["search_req"].get("filters"), list):
                filters = body["search_req"]["filters"]

        for f in filters:
            if not isinstance(f, dict):
                continue
            filter_type = f.get("type")
            tags = f.get("tags")
            tag = ""
            if isinstance(tags, list) and tags:
                first_tag = tags[0]
                tag = self._normalize_filter_tag(first_tag)
            if not tag:
                continue
            if filter_type == "sort_type":
                sort_tag = tag
            elif filter_type == "filter_note_time":
                time_tag = tag

        # 某些请求会把筛选参数放进 query string
        if not sort_tag or not time_tag:
            query = parse_qs(urlparse(request.url).query)
            if not sort_tag:
                sort_tag = self._normalize_filter_tag(
                    (query.get("sort") or query.get("sort_type") or [""])[0]
                )
            if not time_tag:
                time_tag = self._normalize_filter_tag(
                    (query.get("filter_note_time") or query.get("note_time") or [""])[0]
                )

        return sort_tag, time_tag

    def _is_target_filter_combo(self, sort_tag: str, time_tag: str) -> bool:
        sort_norm = (sort_tag or "").strip().lower()
        time_norm = (time_tag or "").strip().lower()
        sort_ok = (
            sort_norm in self._target_sort_tags
            or "like" in sort_norm
            or "popularity" in sort_norm
        )
        time_ok = (
            time_norm in self._target_time_tags
            or "week" in time_norm
            or "7d" in time_norm
            or "7day" in time_norm
            or "一周" in time_tag
            or time_norm == "7"
        )
        return sort_ok and time_ok

    def _extract_items_from_payloads(
        self,
        payloads: list[dict[str, Any]],
        keyword: str,
        require_publish_time: bool = True,
    ) -> list[ContentItem]:
        now = datetime.now()
        one_week_ago = now - timedelta(days=7)
        results: list[ContentItem] = []
        seen_urls: set[str] = set()
        parsed_candidates = 0

        for payload in payloads:
            for entry in self._extract_note_entries_from_payload(payload):
                item = self._entry_to_item(entry, keyword)
                if not item:
                    continue
                parsed_candidates += 1
                if item.url in seen_urls:
                    continue

                # 当请求已命中“一周内”过滤时，可放宽为不强依赖发布时间字段
                publish_dt = self._extract_publish_time(entry)
                if publish_dt:
                    item.publish_time = publish_dt.strftime("%Y-%m-%d %H:%M:%S")
                if require_publish_time and (not publish_dt or publish_dt < one_week_ago):
                    continue

                seen_urls.add(item.url)
                results.append(item)

        if not results:
            self._last_parse_meta = {
                "payloads": len(payloads),
                "parsed_candidates": parsed_candidates,
            }
            logger.warning(
                "API-first extracted 0 items; scanned payloads=%d parsed_candidates=%d",
                len(payloads),
                parsed_candidates,
            )
            self._debug_note_entry_shapes(payloads)
            self._debug_payload_shapes(payloads)
        else:
            self._last_parse_meta = {
                "payloads": len(payloads),
                "parsed_candidates": parsed_candidates,
            }
        return results

    async def _force_search_notes_request_filters(self, route) -> None:
        request = route.request
        raw_post_data = request.post_data or ""
        body = self._safe_json_parse(raw_post_data)
        if not isinstance(body, dict):
            await route.continue_()
            return

        original_sort = body.get("sort")

        filters = body.get("filters")
        keep_filters: list[dict[str, Any]] = []
        if isinstance(filters, list):
            for f in filters:
                if not isinstance(f, dict):
                    continue
                ftype = str(f.get("type") or "").strip()
                if ftype in {"sort_type", "filter_note_time"}:
                    continue
                keep_filters.append(f)

        # 对齐 HAR：UI 选择“最多点赞 + 一周内”后，请求字段如下
        keep_filters.append({"type": "sort_type", "tags": ["popularity_descending"]})
        keep_filters.append({"type": "filter_note_time", "tags": ["一周内"]})
        body["filters"] = keep_filters

        self._forced_request_count += 1
        if self._forced_request_count <= 5:
            logger.info(
                "Forced search/notes filters: sort=%s (unchanged), filters=%s",
                original_sort,
                body["filters"],
            )

        try:
            await route.continue_(post_data=json.dumps(body, ensure_ascii=False))
        except Exception:
            await route.continue_()

    def _extract_request_body(self, request) -> dict[str, Any]:
        raw_post_data = request.post_data or ""
        if not raw_post_data:
            return {}

        parsed = self._safe_json_parse(raw_post_data)
        if isinstance(parsed, dict):
            return parsed

        # 某些场景会把 JSON 包在 form 字段里（如 payload=... / data=...）
        qs = parse_qs(raw_post_data, keep_blank_values=False)
        for key in ("payload", "data", "body", "request"):
            values = qs.get(key)
            if not values:
                continue
            candidate = unquote_plus(values[0])
            parsed = self._safe_json_parse(candidate)
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _safe_json_parse(text: str) -> Any:
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _normalize_filter_tag(tag: Any) -> str:
        if tag is None:
            return ""
        if isinstance(tag, str):
            return tag.strip().lower()
        if isinstance(tag, (int, float)):
            return str(tag).strip().lower()
        if isinstance(tag, dict):
            for key in ("value", "id", "name", "tag", "type"):
                value = tag.get(key)
                if value is None:
                    continue
                return str(value).strip().lower()
        return str(tag).strip().lower()

    def _extract_note_entries_from_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """
        优先走 search/notes 的标准结构，避免从其它噪声节点误提取“伪笔记”。
        """
        entries: list[dict[str, Any]] = []
        data = payload.get("data")
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        entries.append(it)

        # 兜底：某些返回会直接放 note 列表
        if not entries:
            for key in ("items", "notes", "note_list", "noteList"):
                value = payload.get(key)
                if isinstance(value, list):
                    for it in value:
                        if isinstance(it, dict):
                            entries.append(it)
                    if entries:
                        break
        return entries

    def _entry_to_item(self, entry: dict[str, Any], keyword: str) -> Optional[ContentItem]:
        note_card = entry.get("note_card")
        if not isinstance(note_card, dict):
            note_card = entry

        note_id = (
            note_card.get("note_id")
            or note_card.get("noteId")
            or entry.get("note_id")
            or entry.get("noteId")
            or entry.get("id")
        )
        title = (
            note_card.get("display_title")
            or note_card.get("title")
            or entry.get("display_title")
            or entry.get("title")
        )
        if not note_id or not title:
            return None

        xsec_token = (
            entry.get("xsec_token")
            or note_card.get("xsec_token")
            or entry.get("xsecToken")
            or note_card.get("xsecToken")
            or ""
        )

        user = note_card.get("user") if isinstance(note_card.get("user"), dict) else {}
        author = (
            user.get("nickname")
            or user.get("name")
            or note_card.get("author")
            or entry.get("author")
            or ""
        )

        interact = (
            note_card.get("interact_info")
            if isinstance(note_card.get("interact_info"), dict)
            else {}
        )
        likes_raw = (
            interact.get("liked_count")
            or note_card.get("liked_count")
            or note_card.get("like_count")
            or entry.get("liked_count")
            or entry.get("like_count")
            or 0
        )
        likes = self._safe_int(likes_raw)
        video_url = self._extract_video_url_from_entry(entry)

        cover = (
            note_card.get("cover")
            or note_card.get("image")
            or note_card.get("pic")
            or note_card.get("cover_url")
            or ""
        )
        if isinstance(cover, dict):
            cover = cover.get("url_pre") or cover.get("url") or cover.get("default") or ""

        explore_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if isinstance(xsec_token, str) and xsec_token.strip():
            # 关键：详情页通常需要 xsec_token 才能稳定打开
            token = quote(xsec_token.strip(), safe="")
            explore_url = (
                f"{explore_url}?xsec_token={token}&xsec_source=pc_search"
            )

        return ContentItem(
            platform=self.platform,
            title=str(title).strip() or "(无标题)",
            author=str(author).strip(),
            url=explore_url,
            thumbnail=str(cover).strip(),
            video_url=video_url,
            likes=likes,
            keyword=keyword,
            source="auto",
        )

    async def _detect_login_or_risk(self, page: Page) -> list[str]:
        """
        登录风控信号分层：
        - strong:* 代表高置信登录/风控信号，可直接判定
        - weak:* 代表弱文本线索，需要组合命中才能判定
        """
        markers: list[str] = []
        try:
            url = (page.url or "").lower()
            if "login" in url:
                markers.append("strong:url_contains_login")
            if "captcha" in url or "verify" in url:
                markers.append("strong:url_contains_verify")

            body_text = await page.inner_text("body")
            body_text = (body_text or "").strip()

            strong_words = [
                "登录后查看搜索结果",
                "手机号登录",
                "获取验证码",
                "请先登录",
                "重新登录",
                "访问受限",
                "请求异常",
                "安全验证",
                "异常行为",
            ]
            weak_words = ["登录", "扫码", "验证码", "验证"]

            for word in strong_words:
                if word in body_text:
                    markers.append(f"strong:text:{word}")
            for word in weak_words:
                if word in body_text:
                    markers.append(f"weak:text:{word}")

            if await self._looks_like_login_gate_ui(page):
                markers.append("strong:ui_login_gate")

            if await self._is_any_selector_visible(
                page,
                [
                    "img[src*='qrcode']",
                    "img[src*='qr']",
                    "canvas[class*='qrcode']",
                    "canvas[class*='qr']",
                    "[class*='qrcode'] canvas",
                    "[class*='qrcode'] img",
                    "[class*='qr-code'] canvas",
                    "[class*='qr-code'] img",
                ],
            ):
                markers.append("strong:ui_qr_visible")

            # 去重但保持顺序
            deduped: list[str] = []
            for marker in markers:
                if marker not in deduped:
                    deduped.append(marker)
            return deduped
        except Exception:
            return markers

    async def _looks_like_login_gate_ui(self, page: Page) -> bool:
        texts = [
            "登录后查看搜索结果",
            "手机号登录",
            "获取验证码",
            "请先登录",
            "重新登录",
            "小红书如何扫码",
        ]
        for snippet in texts:
            try:
                locator = page.get_by_text(snippet, exact=False).first
                if await locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _is_any_selector_visible(self, page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                node = await page.query_selector(selector)
            except Exception:
                node = None
            if not node:
                continue
            try:
                if await node.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _looks_like_login_required(self, flags: list[str]) -> bool:
        if not flags:
            return False
        normalized = [str(f).strip().lower() for f in flags if str(f).strip()]
        if not normalized:
            return False

        def has_token(token: str) -> bool:
            t = token.lower()
            return any(t in flag for flag in normalized)

        # 强信号命中，直接判为登录风控
        strong_tokens = (
            "strong:",
            "url_contains_login",
            "url_contains_verify",
            "text:请先登录",
            "text:重新登录",
            "text:访问受限",
            "text:请求异常",
            "text:安全验证",
            "text:异常行为",
            "text:手机号登录",
            "text:获取验证码",
            "text:登录后查看搜索结果",
        )
        if any(has_token(token) for token in strong_tokens):
            return True

        # 仅“登录”字样是弱信号，避免误判；需要组合命中才判定
        weak_login = has_token("weak:text:登录") or has_token("text:登录")
        weak_scan = has_token("weak:text:扫码") or has_token("text:扫码")
        weak_sms = has_token("weak:text:验证码") or has_token("text:验证码")
        weak_verify = has_token("weak:text:验证") or has_token("text:验证")

        if weak_login and (weak_scan or weak_sms or weak_verify):
            return True
        if sum(1 for hit in (weak_scan, weak_sms, weak_verify) if hit) >= 2:
            return True

        return False

    def _should_send_login_alert(
        self, keyword: str, stage: str, flags: list[str]
    ) -> tuple[bool, int]:
        normalized_flags = sorted({str(f).strip() for f in flags if str(f).strip()})
        fingerprint = f"{keyword}|{stage}|{'|'.join(normalized_flags)}"
        now_ts = datetime.now().timestamp()
        cooldown = max(60, int(settings.risk_alert_cooldown_seconds))
        count = int(self._login_alert_counts.get(fingerprint, 0)) + 1
        self._login_alert_counts[fingerprint] = count
        if (
            fingerprint == self._last_login_alert_fp
            and (now_ts - self._last_login_alert_ts) < cooldown
        ):
            return False, count
        self._last_login_alert_fp = fingerprint
        self._last_login_alert_ts = now_ts
        return True, count

    async def _handle_login_required(
        self, page: Page, keyword: str, stage: str, flags: list[str]
    ) -> bool:
        screenshot_path, html_path = await self._dump_debug_artifacts(
            page=page,
            keyword=keyword,
            stage=f"{stage}_login",
            flags=flags,
            prefer_qr=True,
        )
        alert_sent = False
        should_send, alert_count = self._should_send_login_alert(keyword, stage, flags)
        if should_send:
            alert_sent = await push_login_risk_alert(
                platform=self.platform,
                keyword=keyword,
                flags=flags,
                stage=stage,
                screenshot_path=screenshot_path,
                html_path=html_path,
                alert_count=alert_count,
            )
        else:
            logger.info(
                "Skip duplicate login alert within cooldown: keyword=%s stage=%s",
                keyword,
                stage,
            )
        recovered = False
        if self._wait_login_recover_after_alert:
            recovered = await self._wait_and_persist_login_recovery(
                page=page,
                keyword=keyword,
                stage=stage,
            )
        self.last_search_debug["login_required"] = (not recovered)
        self.last_search_debug["login_alert_sent"] = bool(alert_sent)
        self.last_search_debug["risk_stage"] = stage
        self.last_search_debug["risk_flags"] = list(flags)
        self.last_search_debug["risk_screenshot"] = screenshot_path
        self.last_search_debug["source"] = "login_recovered" if recovered else "login_required"
        self.last_search_debug["login_recovered"] = bool(recovered)
        return recovered

    async def _wait_and_persist_login_recovery(
        self,
        page: Page,
        keyword: str,
        stage: str,
    ) -> bool:
        wait_seconds = max(10, int(settings.xhs_login_recover_wait_seconds))
        deadline = time.monotonic() + wait_seconds
        logger.info(
            "Wait for QR login recovery: keyword=%s stage=%s timeout=%ss",
            keyword,
            stage,
            wait_seconds,
        )
        last_flags: list[str] = []
        while time.monotonic() < deadline:
            await page.wait_for_timeout(2000)
            flags = await self._detect_login_or_risk(page)
            last_flags = flags
            if not self._looks_like_login_required(flags):
                try:
                    await self._ui_backup._save_cookies(page.context)
                    logger.info(
                        "QR login recovery detected and cookies persisted: keyword=%s stage=%s",
                        keyword,
                        stage,
                    )
                    return True
                except Exception:
                    logger.exception("Persist cookies after QR login recovery failed")
                    return False
        logger.warning(
            "QR login recovery timeout: keyword=%s stage=%s last_flags=%s",
            keyword,
            stage,
            ",".join(last_flags),
        )
        return False

    async def _dump_debug_artifacts(
        self,
        page: Page,
        keyword: str,
        stage: str,
        flags: list[str],
        prefer_qr: bool = False,
    ) -> tuple[str, str]:
        """保存调试文件并返回路径，用于记录到数据库"""
        png_path = ""
        html_path = ""
        try:
            debug_dir = settings.data_dir / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_kw = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", keyword)[:32]
            png_path = str(debug_dir / f"xhs_{ts}_{safe_kw}_{stage}.png")
            html_path = str(debug_dir / f"xhs_{ts}_{safe_kw}_{stage}.html")
            shot_ok = False
            if prefer_qr:
                shot_ok = await self._try_capture_login_qr(page, png_path)
            if not shot_ok:
                await page.screenshot(path=png_path, full_page=True)
            html_path_obj = Path(html_path)
            html_path_obj.write_text(await page.content(), encoding="utf-8")
            logger.warning(
                "Debug snapshot saved: screenshot=%s html=%s flags=%s meta=%s",
                png_path,
                html_path,
                ",".join(flags),
                self._last_parse_meta,
            )
            # 记录到数据库
            self._record_risk_event(keyword, flags, stage, png_path, html_path)
        except Exception as e:
            logger.warning("Failed to save debug snapshot: %s", str(e))
        return png_path, html_path

    async def _try_capture_login_qr(self, page: Page, png_path: str) -> bool:
        qr_selectors = [
            "img[src*='qrcode']",
            "img[src*='qr']",
            "img[alt*='二维码']",
            "canvas[class*='qrcode']",
            "canvas[class*='qr']",
            "[class*='qrcode'] canvas",
            "[class*='qrcode'] img",
            "[class*='qr-code'] canvas",
            "[class*='qr-code'] img",
        ]
        try:
            await page.wait_for_timeout(1200)
            for selector in qr_selectors:
                node = await page.query_selector(selector)
                if not node:
                    continue
                if not await node.is_visible():
                    continue
                await node.scroll_into_view_if_needed()
                await page.wait_for_timeout(150)
                await node.screenshot(path=png_path)
                return True
        except Exception:
            logger.debug("Capture login QR element failed", exc_info=True)
        return False

    def _record_risk_event(
        self, keyword: str, flags: list[str], stage: str, screenshot_path: str, html_path: str
    ) -> None:
        """异步记录风控事件到数据库（避免阻塞爬虫）"""
        try:
            from database import RiskEvent, SessionLocal

            db = SessionLocal()
            try:
                event = RiskEvent(
                    platform=self.platform,
                    keyword=keyword,
                    flags=json.dumps(flags, ensure_ascii=False),
                    stage=stage,
                    screenshot_path=screenshot_path,
                    html_path=html_path,
                )
                db.add(event)
                db.commit()
            except Exception as e:
                logger.warning("Failed to record risk event: %s", str(e))
                db.rollback()
            finally:
                db.close()
        except Exception:
            pass  # 如果数据库模块未加载，静默失败

    def _walk_dict(self, obj: Any):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from self._walk_dict(v)
        elif isinstance(obj, list):
            for i in obj:
                yield from self._walk_dict(i)

    def _expand_candidates(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        if isinstance(node, dict):
            candidates.append(node)
            for key in ("note_card", "noteCard", "note", "item", "post", "model"):
                value = node.get(key)
                if isinstance(value, dict):
                    candidates.append(value)
        return candidates

    def _node_to_item(self, node: dict[str, Any], keyword: str) -> Optional[ContentItem]:
        note_id = (
            node.get("note_id")
            or node.get("noteId")
            or node.get("id")
            or node.get("item_id")
            or node.get("itemId")
            or node.get("noteid")
        )
        title = node.get("title") or node.get("display_title") or node.get("name")
        if not note_id or not title:
            return None

        url = f"https://www.xiaohongshu.com/explore/{note_id}"

        user = node.get("user") if isinstance(node.get("user"), dict) else {}
        author = user.get("nickname") or user.get("name") or node.get("author") or ""

        likes_raw = (
            node.get("liked_count")
            or node.get("like_count")
            or node.get("likes")
            or node.get("likedCount")
            or 0
        )
        likes = self._safe_int(likes_raw)

        cover = (
            node.get("cover")
            or node.get("image")
            or node.get("pic")
            or node.get("cover_url")
            or ""
        )
        if isinstance(cover, dict):
            cover = cover.get("url") or cover.get("default") or ""

        return ContentItem(
            platform=self.platform,
            title=str(title).strip() or "(无标题)",
            author=str(author).strip(),
            url=url,
            thumbnail=str(cover).strip(),
            likes=likes,
            keyword=keyword,
            source="auto",
        )

    def _debug_payload_shapes(self, payloads: list[dict[str, Any]]) -> None:
        # 仅打印极少量样本，避免日志爆炸
        samples = 0
        for payload in payloads:
            for node in self._walk_dict(payload):
                if not isinstance(node, dict):
                    continue
                keys = list(node.keys())
                if not keys:
                    continue
                if any(k in node for k in ("note_card", "note", "title", "note_id", "id")):
                    logger.info("API-first shape sample keys: %s", keys[:12])
                    samples += 1
                    if samples >= 5:
                        return

    def _debug_note_entry_shapes(self, payloads: list[dict[str, Any]]) -> None:
        samples = 0
        for payload in payloads:
            for entry in self._extract_note_entries_from_payload(payload):
                if not isinstance(entry, dict):
                    continue
                note_card = entry.get("note_card") if isinstance(entry.get("note_card"), dict) else {}
                logger.info(
                    "API-first note entry keys=%s note_card_keys=%s",
                    list(entry.keys())[:16],
                    list(note_card.keys())[:16],
                )
                samples += 1
                if samples >= 5:
                    return

    def _extract_publish_time(self, node: dict[str, Any]) -> Optional[datetime]:
        note_card = node.get("note_card") if isinstance(node.get("note_card"), dict) else {}
        interact = (
            note_card.get("interact_info")
            if isinstance(note_card.get("interact_info"), dict)
            else {}
        )
        candidates = [
            node.get("publish_time"),
            node.get("publishTime"),
            node.get("create_time"),
            node.get("time"),
            node.get("timestamp"),
            node.get("last_update_time"),
            note_card.get("publish_time"),
            note_card.get("publishTime"),
            note_card.get("create_time"),
            note_card.get("time"),
            note_card.get("timestamp"),
            interact.get("publish_time"),
        ]
        for value in candidates:
            dt = self._to_datetime(value)
            if dt:
                return dt
        return None

    def _to_datetime(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                # 毫秒时间戳
                if value > 10_000_000_000:
                    return datetime.fromtimestamp(value / 1000)
                return datetime.fromtimestamp(value)
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    return None
                if any(k in value for k in ("分钟前", "小时前", "刚刚")):
                    return datetime.now()
                m = re.search(r"(\d+)\s*天前", value)
                if m:
                    return datetime.now() - timedelta(days=int(m.group(1)))
                if re.fullmatch(r"\d{10,13}", value):
                    num = int(value)
                    if num > 10_000_000_000:
                        return datetime.fromtimestamp(num / 1000)
                    return datetime.fromtimestamp(num)
                # 处理 ISO 时间字符串
                try:
                    iso = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if iso.tzinfo is not None:
                        iso = iso.astimezone().replace(tzinfo=None)
                    return iso
                except ValueError:
                    pass
                for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                    try:
                        return datetime.strptime(value, fmt)
                    except ValueError:
                        continue
        except Exception:
            return None
        return None

    def _safe_int(self, value: Any) -> int:
        try:
            if isinstance(value, str):
                text = value.strip().lower().replace(",", "")
                if "万" in text or "w" in text:
                    return int(float(text.replace("万", "").replace("w", "")) * 10000)
                if "k" in text:
                    return int(float(text.replace("k", "")) * 1000)
            return int(float(value))
        except Exception:
            return 0

    def _extract_video_url_from_entry(self, entry: dict[str, Any]) -> str:
        note_card = entry.get("note_card") if isinstance(entry.get("note_card"), dict) else {}
        search_roots: list[Any] = [
            note_card.get("video"),
            note_card.get("video_info"),
            note_card.get("note_video"),
            note_card,
            entry,
        ]
        for root in search_roots:
            url = self._find_video_url(root)
            if url:
                return url
        return ""

    def _find_video_url(self, node: Any) -> str:
        if node is None:
            return ""
        if isinstance(node, str):
            return node if self._looks_like_video_url(node) else ""
        if isinstance(node, list):
            for item in node:
                found = self._find_video_url(item)
                if found:
                    return found
            return ""
        if not isinstance(node, dict):
            return ""

        # 常见字段优先
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
            value = node.get(key)
            found = self._find_video_url(value)
            if found:
                return found

        for value in node.values():
            found = self._find_video_url(value)
            if found:
                return found
        return ""

    @staticmethod
    def _looks_like_video_url(value: str) -> bool:
        text = str(value or "").strip()
        lower = text.lower()
        if not (lower.startswith("http://") or lower.startswith("https://")):
            return False
        if "xiaohongshu.com/explore/" in lower:
            return False
        video_markers = (
            ".mp4",
            ".m3u8",
            "video/",
            "/stream/",
            "h264",
            "h265",
            "fmp4",
        )
        return any(m in lower for m in video_markers)


async def interactive_login():
    print("=" * 50)
    print("小红书登录助手")
    print("即将打开浏览器，请手动登录小红书")
    print("登录成功后，在终端按 Enter 键保存 Cookie")
    print("=" * 50)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")

        input("\n✅ 登录完成后，按 Enter 键保存 Cookie...")

        cookies = await context.cookies()
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))
        print(f"Cookie 已保存到 {COOKIE_FILE}")
        print(f"共保存 {len(cookies)} 个 Cookie")

        await browser.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(interactive_login())
