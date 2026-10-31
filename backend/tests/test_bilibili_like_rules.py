from models import ContentItem
from scrapers.bilibili import BilibiliScraper


def test_entry_to_item_uses_like_not_play() -> None:
    scraper = BilibiliScraper()
    entry = {
        "title": "示例视频",
        "bvid": "BV1xx411c7mD",
        "play": "12888",
        "like": "523",
        "pubdate": 1710000000,
    }

    item = scraper._entry_to_item(entry, keyword="AI")

    assert item is not None
    assert item.likes == 523


def test_sort_items_by_likes_desc() -> None:
    scraper = BilibiliScraper()
    items = [
        ContentItem(
            platform="bilibili",
            title="A",
            url="https://www.bilibili.com/video/BV1A",
            likes=120,
            publish_time="2026-03-20 10:00:00",
        ),
        ContentItem(
            platform="bilibili",
            title="B",
            url="https://www.bilibili.com/video/BV1B",
            likes=999,
            publish_time="2026-03-20 11:00:00",
        ),
        ContentItem(
            platform="bilibili",
            title="C",
            url="https://www.bilibili.com/video/BV1C",
            likes=999,
            publish_time="2026-03-19 10:00:00",
        ),
    ]

    sorted_items = scraper._sort_items_by_likes_desc(items)

    assert [item.title for item in sorted_items] == ["B", "C", "A"]
