import asyncio
import random
from abc import ABC, abstractmethod

from models import ContentItem, ScrapeResult
from config import settings


class BaseScraper(ABC):
    """所有平台 Scraper 的基类，定义统一接口"""

    platform: str = ""

    @abstractmethod
    async def search(self, keyword: str, max_results: int = 20) -> list[ContentItem]:
        """搜索关键词，返回内容列表"""
        ...

    async def search_and_wrap(
        self, keyword: str, max_results: int = 20
    ) -> ScrapeResult:
        items = await self.search(keyword, max_results)
        return ScrapeResult(
            keyword=keyword,
            platform=self.platform,
            count=len(items),
            items=items,
        )

    @staticmethod
    async def random_delay():
        delay = random.uniform(settings.scrape_delay_min, settings.scrape_delay_max)
        await asyncio.sleep(delay)
