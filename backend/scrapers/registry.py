from typing import Callable

from scrapers.base import BaseScraper
from scrapers.bilibili import BilibiliScraper
from scrapers.xiaohongshu import XiaohongshuScraper

# 新平台接入时：在这里注册即可
SCRAPER_FACTORIES: dict[str, Callable[[], BaseScraper]] = {
    "xiaohongshu": XiaohongshuScraper,
    "bilibili": BilibiliScraper,
}


def create_scrapers() -> dict[str, BaseScraper]:
    return {platform: factory() for platform, factory in SCRAPER_FACTORIES.items()}


def supported_platforms() -> list[str]:
    return sorted(SCRAPER_FACTORIES.keys())
