import asyncio
import html
import json
import logging
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlparse

import httpx
from playwright.async_api import Page, async_playwright

from config import settings
from models import ContentItem
from push.feishu import push_login_risk_alert
from scrapers.base import BaseScraper
from scrapers.xiaohongshu import _runtime_scrape_headless

logger = logging.getLogger(__name__)

BILIBILI_ORDER = "click"
BILIBILI_FILTER_DAYS = 7
BILIBILI_SEARCH_URL = (
    "https://search.bilibili.com/video?keyword={keyword}&order={order}"
    "&pubtime_begin_s={pubtime_begin_s}&pubtime_end_s={pubtime_end_s}"
)


class BilibiliScraper(BaseScraper):
    platform = "bilibili"

    def __init__(self):
        self.last_search_debug: dict[str, Any] = {}
        self._last_login_alert_fp = ""
        self._last_login_alert_ts = 0.0
        self._login_alert_counts: dict[str, int] = {}

    async def search(self, keyword: str, max_results: int = 20) -> list[ContentItem]:
        runtime_headless = _runtime_scrape_headless()
        payloads: list[dict[str, Any]] = []
        pending_tasks: list[asyncio.Task] = []

        self.last_search_debug = {
            "mode": "api_first",
            "keyword": keyword,
            "payloads": 0,
            "source": "",
            "fallback": False,
            "result_count": 0,
            "login_required": False,
            "login_alert_sent": False,
            "risk_stage": "",
            "risk_flags": [],
            "risk_screenshot": "",
            "filter_sort_target": "most_liked",
            "filter_time_target_days": BILIBILI_FILTER_DAYS,
            "search_url": "",
            "filter_url_applied": False,
            "filter_request_hits": 0,
            "filter_request_examples": [],
            "like_enrich_attempted": 0,
            "like_enriched": 0,
            "like_missing_after_enrich": 0,
            "result_sort": "likes_desc",
            "week_filter_total_before": 0,
            "week_filter_dropped_old": 0,
            "week_filter_dropped_unknown": 0,
            "week_filter_kept_unknown": 0,
        }

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=runtime_headless)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            def on_response(resp):
                if resp.request.resource_type not in ("xhr", "fetch"):
                    return
                url = resp.url
                if "api.bilibili.com" not in url:
                    return
                if not any(
                    marker in url
                    for marker in (
                        "/x/web-interface/search",
                        "/x/web-interface/wbi/search",
                        "/x/web-interface/search/type",
                    )
                ):
                    return
                if self._is_bilibili_filter_request(resp.request.url):
                    self.last_search_debug["filter_request_hits"] = int(
                        self.last_search_debug.get("filter_request_hits", 0)
                    ) + 1
                    examples = self.last_search_debug.get("filter_request_examples")
                    if isinstance(examples, list) and len(examples) < 5:
                        examples.append(resp.request.url[:240])
                task = asyncio.create_task(self._capture_json_response(resp, payloads))
                pending_tasks.append(task)

            page.on("response", on_response)

            try:
                pubtime_begin_s, pubtime_end_s = self._compute_recent_week_range_ts()
                url = BILIBILI_SEARCH_URL.format(
                    keyword=quote(keyword),
                    order=BILIBILI_ORDER,
                    pubtime_begin_s=pubtime_begin_s,
                    pubtime_end_s=pubtime_end_s,
                )
                logger.info("Bilibili scraper navigating: %s", url)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2000)
                current_url = str(page.url or "")
                self.last_search_debug["search_url"] = current_url
                self.last_search_debug["filter_url_applied"] = self._is_bilibili_filter_url(
                    current_url
                )

                risk_flags = await self._detect_risk_or_login(page)
                if self._looks_like_login_required(risk_flags):
                    await self._handle_login_required(
                        page=page,
                        keyword=keyword,
                        stage="pre_capture",
                        flags=risk_flags,
                    )
                    return []

                for _ in range(max(6, max_results // 3)):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(800)

                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)

                items = self._extract_items_from_payloads(payloads, keyword)
                self.last_search_debug["payloads"] = len(payloads)

                if not items:
                    state_items = await self._extract_items_from_page_state(
                        page, keyword, max_results
                    )
                    if state_items:
                        self.last_search_debug["fallback"] = True
                        self.last_search_debug["source"] = "state"
                        items = state_items
                    else:
                        self.last_search_debug["fallback"] = True
                        self.last_search_debug["source"] = "dom"
                        items = await self._extract_items_from_dom(page, keyword, max_results)
                else:
                    self.last_search_debug["source"] = "api"

                if not items:
                    flags = await self._detect_risk_or_login(page)
                    if self._looks_like_login_required(flags):
                        await self._handle_login_required(
                            page=page,
                            keyword=keyword,
                            stage="api_empty",
                            flags=flags,
                        )
                        return []

                allow_unknown = bool(
                    self.last_search_debug.get("filter_url_applied")
                    or int(self.last_search_debug.get("filter_request_hits", 0)) > 0
                )
                items = await self._enrich_items_with_stats(items)
                items = self._filter_items_within_week(items, allow_unknown=allow_unknown)
                items = self._sort_items_by_likes_desc(items)
                items = items[:max_results]
                self.last_search_debug["result_count"] = len(items)
                logger.info(
                    "Bilibili scrape done: keyword=%s payloads=%d results=%d",
                    keyword,
                    len(payloads),
                    len(items),
                )
                return items
            except Exception:
                logger.exception("Bilibili scrape failed: keyword=%s", keyword)
                self.last_search_debug["source"] = "exception"
                return []
            finally:
                await browser.close()

    async def _capture_json_response(self, response, payloads: list[dict[str, Any]]) -> None:
        try:
            data = await response.json()
        except Exception:
            return
        if not isinstance(data, dict):
            return
        payloads.append(data)

    async def _detect_risk_or_login(self, page: Page) -> list[str]:
        markers: list[str] = []
        try:
            url = str(page.url or "").lower()
            if "passport.bilibili.com" in url or "login" in url:
                markers.append("url_contains_login")
            if "captcha" in url or "verify" in url or "risk" in url:
                markers.append("url_contains_verify")

            text = await page.inner_text("body")
            body_text = str(text or "").strip()
            compact = re.sub(r"\s+", "", body_text)
            if re.search(r"(请先登录|请登录后|登录后(继续|查看|访问|操作|才能))", compact):
                markers.append("text:login_required")
            for marker in ("验证码", "安全验证", "访问异常", "操作频繁", "稍后再试", "人机验证"):
                if marker in body_text:
                    markers.append(f"text:{marker}")

            dedup: list[str] = []
            for marker in markers:
                if marker not in dedup:
                    dedup.append(marker)
            return dedup
        except Exception:
            return markers

    @staticmethod
    def _looks_like_login_required(flags: list[str]) -> bool:
        if not flags:
            return False
        login_tokens = (
            "url_contains_login",
            "url_contains_verify",
            "text:login_required",
            "text:验证码",
            "text:安全验证",
            "text:访问异常",
            "text:操作频繁",
            "text:稍后再试",
            "text:人机验证",
        )
        return any(any(token in flag for token in login_tokens) for flag in flags)

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
    ) -> None:
        screenshot_path, html_path = await self._dump_debug_artifacts(
            page=page,
            keyword=keyword,
            stage=f"{stage}_login",
            flags=flags,
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
                "Skip duplicate login alert within cooldown: platform=%s keyword=%s stage=%s",
                self.platform,
                keyword,
                stage,
            )

        self.last_search_debug["login_required"] = True
        self.last_search_debug["login_alert_sent"] = bool(alert_sent)
        self.last_search_debug["risk_stage"] = stage
        self.last_search_debug["risk_flags"] = list(flags)
        self.last_search_debug["risk_screenshot"] = screenshot_path
        self.last_search_debug["source"] = "login_required"

    async def _dump_debug_artifacts(
        self,
        page: Page,
        keyword: str,
        stage: str,
        flags: list[str],
    ) -> tuple[str, str]:
        png_path = ""
        html_path = ""
        try:
            debug_dir = settings.data_dir / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_kw = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", keyword)[:32]
            png_path = str(debug_dir / f"bili_{ts}_{safe_kw}_{stage}.png")
            html_path = str(debug_dir / f"bili_{ts}_{safe_kw}_{stage}.html")

            await page.screenshot(path=png_path, full_page=True)
            Path(html_path).write_text(await page.content(), encoding="utf-8")
            logger.warning(
                "Bilibili debug snapshot saved: screenshot=%s html=%s flags=%s",
                png_path,
                html_path,
                ",".join(flags),
            )
            self._record_risk_event(keyword, flags, stage, png_path, html_path)
        except Exception as e:
            logger.warning("Bilibili save debug snapshot failed: %s", str(e))
        return png_path, html_path

    def _record_risk_event(
        self,
        keyword: str,
        flags: list[str],
        stage: str,
        screenshot_path: str,
        html_path: str,
    ) -> None:
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
                logger.warning("Bilibili record risk event failed: %s", str(e))
                db.rollback()
            finally:
                db.close()
        except Exception:
            pass

    def _extract_items_from_payloads(
        self,
        payloads: list[dict[str, Any]],
        keyword: str,
    ) -> list[ContentItem]:
        items: list[ContentItem] = []
        seen_urls: set[str] = set()

        for payload in payloads:
            entries = self._extract_video_entries(payload)
            for entry in entries:
                item = self._entry_to_item(entry, keyword)
                if not item:
                    continue
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                items.append(item)
        return items

    def _extract_video_entries(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        data = payload.get("data")
        if isinstance(data, dict):
            result = data.get("result")
            if isinstance(result, list):
                for item in result:
                    if isinstance(item, dict):
                        entries.append(item)

        if entries:
            return entries

        stack: list[Any] = [payload]
        scanned = 0
        while stack and scanned < 5000:
            scanned += 1
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue

            if (
                node.get("arcurl")
                or node.get("bvid")
                or (
                    isinstance(node.get("uri"), str)
                    and "bilibili.com/video/" in node.get("uri", "")
                )
            ) and (node.get("title") or node.get("description")):
                entries.append(node)

            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append(value)

        return entries

    def _entry_to_item(self, entry: dict[str, Any], keyword: str) -> Optional[ContentItem]:
        raw_title = (
            entry.get("title")
            or entry.get("name")
            or entry.get("description")
            or ""
        )
        title = self._clean_title(raw_title)

        url = str(entry.get("arcurl") or entry.get("uri") or "").strip()
        if not url:
            bvid = str(entry.get("bvid") or "").strip()
            if bvid:
                url = f"https://www.bilibili.com/video/{bvid}"
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http"):
            return None

        author = (
            entry.get("author")
            or entry.get("up_name")
            or (
                (entry.get("owner") or {}).get("name")
                if isinstance(entry.get("owner"), dict)
                else ""
            )
            or ""
        )

        stat = entry.get("stat") if isinstance(entry.get("stat"), dict) else {}
        likes = self._safe_int(
            entry.get("like")
            or stat.get("like")
            or entry.get("likes")
            or entry.get("favorites")
            or 0
        )
        if likes <= 0:
            likes = self._extract_like_count_from_text(
                " ".join(
                    str(v or "")
                    for v in (
                        entry.get("description"),
                        entry.get("desc"),
                        entry.get("subtitle"),
                    )
                )
            )
        comments = self._safe_int(
            entry.get("review")
            or entry.get("comment")
            or entry.get("video_review")
            or stat.get("reply")
            or 0
        )
        shares = self._safe_int(entry.get("share") or stat.get("share") or 0)

        pic = str(entry.get("pic") or entry.get("cover") or "").strip()
        if pic.startswith("//"):
            pic = "https:" + pic

        publish_time = ""
        publish_dt = self._to_datetime(
            entry.get("pubdate")
            or entry.get("senddate")
            or entry.get("created")
            or entry.get("ctime")
            or entry.get("pubdate_str")
        )
        if publish_dt:
            publish_time = publish_dt.strftime("%Y-%m-%d %H:%M:%S")

        return ContentItem(
            platform=self.platform,
            title=title or "(无标题)",
            author=str(author).strip(),
            url=url,
            thumbnail=pic,
            likes=likes,
            comments=comments,
            shares=shares,
            publish_time=publish_time,
            video_url=url,
            keyword=keyword,
            source="auto",
        )

    async def _extract_items_from_dom(
        self,
        page: Page,
        keyword: str,
        max_results: int,
    ) -> list[ContentItem]:
        items: list[ContentItem] = []
        seen_urls: set[str] = set()

        links = await page.query_selector_all('a[href*="bilibili.com/video/"]')
        for link in links:
            try:
                href = await link.get_attribute("href")
                if not href:
                    continue
                href = href.strip()
                if href.startswith("//"):
                    href = "https:" + href
                if not href.startswith("http"):
                    continue
                if href in seen_urls:
                    continue

                container = await link.evaluate_handle(
                    "el => el.closest('article, li, div[class*=video], div[class*=item], div[class*=card]') || el.parentElement"
                )
                text = ""
                if container:
                    try:
                        text = (await container.inner_text()).strip()
                    except Exception:
                        text = ""

                title = await self._extract_dom_title(link, container)
                if not title:
                    title = "(无标题)"
                author = await self._extract_dom_author(link, container, text)

                likes = self._extract_like_count_from_text(text)
                publish_dt = self._extract_publish_time_from_text(text)
                publish_time = (
                    publish_dt.strftime("%Y-%m-%d %H:%M:%S") if publish_dt else ""
                )

                seen_urls.add(href)
                items.append(
                    ContentItem(
                        platform=self.platform,
                        title=title,
                        author=author,
                        url=href,
                        likes=likes,
                        publish_time=publish_time,
                        keyword=keyword,
                        source="auto",
                    )
                )
                if len(items) >= max_results * 2:
                    break
            except Exception:
                continue
        return items

    async def _extract_items_from_page_state(
        self,
        page: Page,
        keyword: str,
        max_results: int,
    ) -> list[ContentItem]:
        try:
            candidates = await page.evaluate(
                """
                () => {
                  const out = [];
                  if (window.__INITIAL_STATE__ && typeof window.__INITIAL_STATE__ === 'object') {
                    out.push(window.__INITIAL_STATE__);
                  }
                  if (window.__pinia && window.__pinia.state && window.__pinia.state.value) {
                    out.push(window.__pinia.state.value);
                  }
                  if (window.__STORE__ && typeof window.__STORE__ === 'object') {
                    out.push(window.__STORE__);
                  }
                  return out;
                }
                """
            )
        except Exception:
            return []

        if not isinstance(candidates, list):
            return []
        payloads: list[dict[str, Any]] = [x for x in candidates if isinstance(x, dict)]
        if not payloads:
            return []

        items = self._extract_items_from_payloads(payloads, keyword)
        if not items:
            return []
        return items[: max_results * 2]

    async def _extract_dom_title(self, link, container) -> str:
        direct = self._clean_title(await link.get_attribute("title") or "")
        if self._looks_like_valid_title(direct):
            return direct
        try:
            title = await link.evaluate(
                """
                (el) => {
                  const root = el.closest('article, li, div[class*="video"], div[class*="item"], div[class*="card"]') || el.parentElement || document;
                  const selectors = [
                    'h3',
                    'a[title]',
                    '[class*="title"]',
                    '[class*="tit"]',
                    '[class*="headline"]'
                  ];
                  const pool = [];
                  const pushText = (v) => {
                    const text = String(v || '').replace(/\\s+/g, ' ').trim();
                    if (text) pool.push(text);
                  };
                  pushText(el.getAttribute('title'));
                  pushText(el.textContent);
                  for (const sel of selectors) {
                    root.querySelectorAll(sel).forEach((n) => {
                      pushText(n.getAttribute && n.getAttribute('title'));
                      pushText(n.textContent);
                    });
                  }
                  return pool;
                }
                """
            )
            if isinstance(title, list):
                for raw in title:
                    cleaned = self._clean_title(raw)
                    if self._looks_like_valid_title(cleaned):
                        return cleaned
        except Exception:
            pass

        if container:
            try:
                fallback = self._clean_title((await container.inner_text() or "").split("\n")[0])
                if self._looks_like_valid_title(fallback):
                    return fallback
            except Exception:
                pass
        return ""

    def _looks_like_valid_title(self, text: str) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        if len(t) < 2:
            return False
        if t.startswith("稍后再看"):
            return False
        if re.fullmatch(r"[\d\s\.:万wWkK]+", t):
            return False
        return True

    async def _extract_dom_author(self, link, container, block_text: str) -> str:
        def _clean_author(raw: str) -> str:
            text = self._clean_title(raw)
            text = re.sub(r"^(UP主|UP|作者)\s*[:：]?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*[·•]\s*\d{1,2}-\d{1,2}$", "", text)
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                return ""
            if len(text) > 30:
                return ""
            if re.fullmatch(r"[\d\W_]+", text):
                return ""
            return text

        if container:
            try:
                names = await link.evaluate(
                    """
                    (el) => {
                      const root = el.closest('article, li, div[class*="video"], div[class*="item"], div[class*="card"]') || el.parentElement || document;
                      const selectors = [
                        'a[href*="space.bilibili.com/"]',
                        '[class*="author"]',
                        '[class*="upname"]',
                        '[class*="up-name"]',
                        '[class*="up"]'
                      ];
                      const out = [];
                      const pushText = (v) => {
                        const text = String(v || '').replace(/\\s+/g, ' ').trim();
                        if (text) out.push(text);
                      };
                      for (const sel of selectors) {
                        root.querySelectorAll(sel).forEach((n) => {
                          pushText(n.getAttribute && n.getAttribute('title'));
                          pushText(n.textContent);
                        });
                      }
                      return out;
                    }
                    """
                )
                if isinstance(names, list):
                    for raw in names:
                        name = _clean_author(str(raw or ""))
                        if name and name != "稍后再看":
                            return name
            except Exception:
                pass

        text = str(block_text or "")
        m = re.search(r"(?:UP主|UP|作者)\s*[:：]?\s*([A-Za-z0-9_\-\u4e00-\u9fff]{1,24})", text)
        if m:
            return _clean_author(m.group(1))
        return ""

    def _clean_title(self, raw: Any) -> str:
        text = html.unescape(str(raw or ""))
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"^稍后再看", "", text).strip()
        return text

    def _compute_recent_week_range_ts(self) -> tuple[int, int]:
        now = datetime.now()
        end_dt = datetime.combine(now.date(), time(23, 59, 59))
        start_date = now.date() - timedelta(days=BILIBILI_FILTER_DAYS - 1)
        start_dt = datetime.combine(start_date, time(0, 0, 0))
        return int(start_dt.timestamp()), int(end_dt.timestamp())

    def _is_bilibili_filter_url(self, url: str) -> bool:
        try:
            query = parse_qs(urlparse(url).query)
            order = (query.get("order") or [""])[0]
            begin = (query.get("pubtime_begin_s") or [""])[0]
            end = (query.get("pubtime_end_s") or [""])[0]
            return order == BILIBILI_ORDER and begin.isdigit() and end.isdigit()
        except Exception:
            return False

    def _is_bilibili_filter_request(self, url: str) -> bool:
        return self._is_bilibili_filter_url(url)

    def _filter_items_within_week(
        self, items: list[ContentItem], allow_unknown: bool
    ) -> list[ContentItem]:
        now = datetime.now()
        week_ago = now - timedelta(days=BILIBILI_FILTER_DAYS)
        kept: list[ContentItem] = []
        dropped_old = 0
        dropped_unknown = 0
        kept_unknown = 0
        total_before = len(items)

        for item in items:
            publish_dt = self._to_datetime(item.publish_time)
            if publish_dt is not None:
                if publish_dt >= week_ago:
                    kept.append(item)
                else:
                    dropped_old += 1
                continue
            if allow_unknown:
                kept.append(item)
                kept_unknown += 1
            else:
                dropped_unknown += 1

        self.last_search_debug["week_filter_total_before"] = total_before
        self.last_search_debug["week_filter_dropped_old"] = dropped_old
        self.last_search_debug["week_filter_dropped_unknown"] = dropped_unknown
        self.last_search_debug["week_filter_kept_unknown"] = kept_unknown
        return kept

    async def _enrich_items_with_stats(self, items: list[ContentItem]) -> list[ContentItem]:
        if not items:
            return items

        attempted = 0
        enriched = 0
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        }
        sem = asyncio.Semaphore(6)

        async def _fill(item: ContentItem, client: httpx.AsyncClient) -> None:
            nonlocal attempted, enriched
            bvid = self._extract_bvid(item.url)
            if not bvid:
                return
            attempted += 1
            try:
                async with sem:
                    resp = await client.get(
                        "https://api.bilibili.com/x/web-interface/view",
                        params={"bvid": bvid},
                    )
                if resp.status_code != 200:
                    return
                payload = resp.json()
                if not isinstance(payload, dict):
                    return
                data = payload.get("data")
                if not isinstance(data, dict):
                    return
                stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
                like_count = self._safe_int(
                    stat.get("like") or data.get("like") or 0
                )
                if like_count > 0 and like_count != int(item.likes):
                    item.likes = like_count
                    enriched += 1
                if not item.publish_time:
                    publish_dt = self._to_datetime(data.get("pubdate") or data.get("ctime"))
                    if publish_dt:
                        item.publish_time = publish_dt.strftime("%Y-%m-%d %H:%M:%S")
                comments = self._safe_int(stat.get("reply") or 0)
                if comments > int(item.comments):
                    item.comments = comments
                shares = self._safe_int(stat.get("share") or 0)
                if shares > int(item.shares):
                    item.shares = shares
            except Exception:
                return

        async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
            await asyncio.gather(
                *(_fill(item, client) for item in items),
                return_exceptions=True,
            )
        self.last_search_debug["like_enrich_attempted"] = int(attempted)
        self.last_search_debug["like_enriched"] = int(enriched)
        self.last_search_debug["like_missing_after_enrich"] = int(
            sum(1 for item in items if int(item.likes) <= 0)
        )
        return items

    @staticmethod
    def _extract_bvid(url: str) -> str:
        try:
            path = urlparse(str(url or "")).path
        except Exception:
            return ""
        m = re.search(r"/video/([A-Za-z0-9]+)", path)
        if not m:
            return ""
        return str(m.group(1) or "").strip()

    def _sort_items_by_likes_desc(self, items: list[ContentItem]) -> list[ContentItem]:
        def _key(item: ContentItem) -> tuple[int, float]:
            likes = int(item.likes or 0)
            publish_dt = self._to_datetime(item.publish_time)
            ts = publish_dt.timestamp() if publish_dt else 0.0
            return likes, ts

        return sorted(items, key=_key, reverse=True)

    def _extract_like_count_from_text(self, text: str) -> int:
        if not text:
            return 0
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        if not normalized:
            return 0
        patterns = [
            r"(?:点赞数|点赞|获赞|赞)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?\s*[万wWkK]?)",
            r"([0-9]+(?:\.[0-9]+)?\s*[万wWkK]?)\s*(?:点赞数|点赞|获赞|赞)",
        ]
        for pattern in patterns:
            m = re.search(pattern, normalized, flags=re.IGNORECASE)
            if not m:
                continue
            return self._safe_int(m.group(1))
        return 0

    def _extract_publish_time_from_text(self, text: str) -> Optional[datetime]:
        raw = str(text or "").strip()
        if not raw:
            return None
        now = datetime.now()

        m = re.search(r"(\d+)\s*小时前", raw)
        if m:
            return now - timedelta(hours=int(m.group(1)))

        m = re.search(r"(\d+)\s*天前", raw)
        if m:
            return now - timedelta(days=int(m.group(1)))

        if "昨天" in raw:
            return now - timedelta(days=1)
        if "刚刚" in raw:
            return now

        m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return self._to_datetime(m.group(1))

        m = re.search(r"\b(\d{2}-\d{2})\b", raw)
        if m:
            try:
                month, day = m.group(1).split("-")
                candidate = datetime(now.year, int(month), int(day))
                if candidate > now + timedelta(days=1):
                    candidate = datetime(now.year - 1, int(month), int(day))
                return candidate
            except Exception:
                return None
        return None

    def _to_datetime(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                ts = float(value)
                if ts > 10_000_000_000:
                    ts = ts / 1000.0
                return datetime.fromtimestamp(ts)
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return None
                if re.fullmatch(r"\d{10,13}", text):
                    ts = float(text)
                    if ts > 10_000_000_000:
                        ts = ts / 1000.0
                    return datetime.fromtimestamp(ts)
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if parsed.tzinfo is not None:
                        parsed = parsed.astimezone().replace(tzinfo=None)
                    return parsed
                except ValueError:
                    pass
                for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                    try:
                        return datetime.strptime(text, fmt)
                    except ValueError:
                        continue
        except Exception:
            return None
        return None

    def _safe_int(self, value: Any) -> int:
        try:
            if isinstance(value, str):
                text = value.strip().lower().replace(",", "")
                if not text:
                    return 0
                if "万" in text or "w" in text:
                    cleaned = text.replace("万", "").replace("w", "")
                    return int(float(cleaned) * 10000)
                if "k" in text:
                    cleaned = text.replace("k", "")
                    return int(float(cleaned) * 1000)
                return int(float(text))
            return int(float(value))
        except Exception:
            return 0
