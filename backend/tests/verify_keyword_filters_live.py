#!/usr/bin/env python3
"""
实跑验证：检查单关键词抓取结果是否满足
1) 点赞数降序
2) 发布时间在一周内

说明：
- 这是一个“在线验证”脚本，会真实访问小红书接口。
- 如果风控/登录导致抓取为空，结果会显示为 inconclusive（结论不充分）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# 允许从 backend/tests 直接运行脚本
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scrapers.xiaohongshu import XiaohongshuScraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


class InspectableXiaohongshuScraper(XiaohongshuScraper):
    """在不改主逻辑的前提下，记录筛选证据。"""

    def __init__(self):
        super().__init__()
        self.captured_filter_pairs: list[tuple[str, str]] = []
        self.publish_time_by_url: dict[str, datetime] = {}

    async def _capture_json_response(
        self,
        response,
        payloads,
        strict_payloads,
    ) -> None:
        if "/api/sns/web/v1/search/notes" not in response.url:
            await super()._capture_json_response(response, payloads, strict_payloads)
            return
        sort_tag, time_tag = self._extract_sort_time_from_request(response.request)
        self.captured_filter_pairs.append((sort_tag, time_tag))
        await super()._capture_json_response(response, payloads, strict_payloads)

    def _extract_items_from_payloads(self, payloads, keyword, require_publish_time=True):
        # 预先记录 URL -> 发布时间，供最终结果核验（不改变原有筛选逻辑）
        publish_map: dict[str, datetime] = {}
        for payload in payloads:
            for node in self._walk_dict(payload):
                for candidate in self._expand_candidates(node):
                    item = self._node_to_item(candidate, keyword)
                    if not item:
                        continue
                    publish_dt = self._extract_publish_time(candidate) or self._extract_publish_time(node)
                    if not publish_dt:
                        continue
                    existed = publish_map.get(item.url)
                    if existed is None or publish_dt > existed:
                        publish_map[item.url] = publish_dt
        self.publish_time_by_url = publish_map
        return super()._extract_items_from_payloads(
            payloads,
            keyword,
            require_publish_time=require_publish_time,
        )


def summarize(scraper: InspectableXiaohongshuScraper, items) -> dict:
    now = datetime.now()
    week_ago = now - timedelta(days=7)

    likes = [int(i.likes) for i in items]
    sorted_desc = likes == sorted(likes, reverse=True)
    all_zero_likes = bool(items) and all(v == 0 for v in likes)
    has_like_variation = len(set(likes)) > 1

    known_times = 0
    week_violations = []
    missing_times = 0
    for item in items:
        publish_dt = scraper.publish_time_by_url.get(item.url)
        if not publish_dt:
            missing_times += 1
            continue
        known_times += 1
        if publish_dt < week_ago:
            week_violations.append((item.url, publish_dt))

    filter_counter = Counter(scraper.captured_filter_pairs)
    strict_hits = sum(
        1
        for sort_tag, time_tag in scraper.captured_filter_pairs
        if sort_tag in scraper._target_sort_tags and time_tag in scraper._target_time_tags
    )

    time_passed_by_request_filter = bool(items) and strict_hits > 0

    if not items:
        time_status = "inconclusive"
    elif time_passed_by_request_filter and not week_violations:
        # 搜索请求已明确命中“一周内”过滤，即便列表接口未返回发布时间字段，也可判定通过
        time_status = "passed"
    elif known_times == 0:
        time_status = "inconclusive"
    elif week_violations:
        time_status = "failed"
    else:
        time_status = "passed"

    if not items:
        sort_status = "inconclusive"
    elif all_zero_likes or not has_like_variation:
        # 点赞全相同（尤其全 0）时，无法证明“最高点赞优先”是否有效
        sort_status = "inconclusive"
    else:
        sort_status = "passed" if sorted_desc else "failed"

    return {
        "sort_status": sort_status,
        "time_status": time_status,
        "item_count": len(items),
        "known_times": known_times,
        "missing_times": missing_times,
        "week_violations": week_violations,
        "all_zero_likes": all_zero_likes,
        "has_like_variation": has_like_variation,
        "strict_filter_hits": strict_hits,
        "captured_filter_counter": filter_counter,
        "time_passed_by_request_filter": time_passed_by_request_filter,
    }


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", required=True, help="要测试的关键词，例如 AIGC")
    parser.add_argument("--max-results", type=int, default=20, help="最多抓取条数")
    parser.add_argument(
        "--retry-until-pass",
        action="store_true",
        help="若不满足条件则自动重试，直到通过或达到最大次数",
    )
    parser.add_argument("--max-attempts", type=int, default=5, help="最大重试次数")
    parser.add_argument("--retry-interval", type=float, default=2.0, help="重试间隔秒数")
    args = parser.parse_args()

    attempts = max(1, args.max_attempts)
    final_items = []
    final_report = None
    final_scraper = None

    for attempt in range(1, attempts + 1):
        scraper = InspectableXiaohongshuScraper()
        items = await scraper.search(args.keyword, args.max_results)
        report = summarize(scraper, items)

        print("=" * 72)
        print(f"尝试次数: {attempt}/{attempts}")
        print(f"关键词: {args.keyword}")
        print(f"结果条数: {report['item_count']}")
        print(f"条件1-点赞降序: {report['sort_status']}")
        print(f"条件2-一周内: {report['time_status']}")
        print(f"一周内判定是否由请求过滤命中支撑: {report['time_passed_by_request_filter']}")
        print(f"点赞有区分度: {report['has_like_variation']} (是否全0: {report['all_zero_likes']})")
        print(f"命中 strict 过滤请求(最多点赞+一周内)次数: {report['strict_filter_hits']}")
        print(f"捕获请求过滤组合统计: {dict(report['captured_filter_counter'])}")
        print(f"可核验发布时间条数: {report['known_times']}, 缺失发布时间条数: {report['missing_times']}")

        if report["week_violations"]:
            print("一周外样本(最多前5条):")
            for url, dt in report["week_violations"][:5]:
                print(f"- {url} | publish={dt.isoformat(sep=' ', timespec='seconds')}")

        if items:
            print("-" * 72)
            print("Top 10 结果样本:")
            for i, item in enumerate(items[:10], 1):
                publish_dt = scraper.publish_time_by_url.get(item.url)
                publish_str = (
                    publish_dt.isoformat(sep=" ", timespec="seconds")
                    if publish_dt
                    else "unknown"
                )
                print(f"{i:02d}. likes={item.likes:<8} publish={publish_str} title={item.title[:42]}")
        print("=" * 72)

        passed = report["sort_status"] == "passed" and report["time_status"] == "passed"
        final_items = items
        final_report = report
        final_scraper = scraper
        if passed:
            print(f"✅ 第 {attempt} 次尝试已满足全部条件")
            break

        if not args.retry_until_pass:
            break
        if attempt < attempts:
            print(f"⚠️ 第 {attempt} 次未满足条件，{args.retry_interval} 秒后重试...")
            await asyncio.sleep(max(0.0, args.retry_interval))

    assert final_report is not None and final_scraper is not None
    final_passed = (
        final_report["sort_status"] == "passed"
        and final_report["time_status"] == "passed"
    )
    if final_passed:
        print("FINAL_STATUS=PASS")
        return
    print("FINAL_STATUS=FAIL")


if __name__ == "__main__":
    asyncio.run(main())
