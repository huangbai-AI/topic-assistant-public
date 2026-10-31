from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import settings


class Base(DeclarativeBase):
    pass


class Content(Base):
    __tablename__ = "contents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(32), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    author = Column(String(200), default="")
    url = Column(String(1000), nullable=False)
    thumbnail = Column(String(1000), default="")
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    publish_time = Column(String(32), default="")
    video_url = Column(String(1000), default="")
    keyword = Column(String(200), default="", index=True)
    source = Column(String(32), default="auto")  # auto / manual
    is_read = Column(Boolean, default=False)
    is_starred = Column(Boolean, default=False)
    viewed_at = Column(DateTime, nullable=True)
    collected_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("platform", "url", name="uq_platform_url"),)


class Keyword(Base):
    __tablename__ = "keywords"

    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(String(200), nullable=False, unique=True)
    platforms = Column(Text, default='["xiaohongshu"]')
    min_likes = Column(Integer, default=0)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PushLog(Base):
    __tablename__ = "push_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pushed_at = Column(DateTime, default=datetime.utcnow)
    keyword = Column(String(200), default="")
    content_count = Column(Integer, default=0)
    status = Column(String(32), default="success")
    detail = Column(Text, default="")


class ScheduleConfig(Base):
    __tablename__ = "schedule_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    enabled = Column(Boolean, default=True)
    hour = Column(Integer, default=9)
    minute = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScheduleSlot(Base):
    __tablename__ = "schedule_slots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hour = Column(Integer, nullable=False)
    minute = Column(Integer, nullable=False)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("hour", "minute", name="uq_schedule_hour_minute"),)


class AppConfig(Base):
    __tablename__ = "app_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key = Column(String(255), default="")
    feishu_webhook_url = Column(String(1000), default="")
    feishu_chat_id = Column(String(255), default="")
    scrape_headless = Column(Boolean, default=True)
    login_blocked = Column(Boolean, default=False)
    login_blocked_at = Column(DateTime, nullable=True)
    login_block_reason = Column(String(255), default="")
    login_block_keyword = Column(String(200), default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(32), nullable=False, index=True)
    keyword = Column(String(200), default="")
    detected_at = Column(DateTime, default=datetime.utcnow, index=True)
    flags = Column(Text, default="")  # JSON array of detected markers
    stage = Column(String(32), default="")  # pre_capture, api_empty, etc.
    screenshot_path = Column(String(500), default="")
    html_path = Column(String(500), default="")


class ScrapeTask(Base):
    __tablename__ = "scrape_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(64), nullable=False, unique=True, index=True)
    trigger_source = Column(String(32), default="manual")  # manual/schedule/feishu
    status = Column(String(32), default="queued", index=True)  # queued/running/success/failed
    requested_keyword_ids = Column(Text, default="[]")
    resolved_keyword_ids = Column(Text, default="[]")
    scrape_headless = Column(Boolean, nullable=True)
    queued_at = Column(DateTime, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    error_code = Column(String(64), default="")
    error_message = Column(Text, default="")
    result_summary = Column(Text, default="")
    request_id = Column(String(64), default="")


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=False,
)
SessionLocal = sessionmaker(bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)
    _ensure_content_columns()
    _ensure_app_config_columns()


def _ensure_content_columns() -> None:
    # 轻量迁移：为已存在的 SQLite 表补齐新增字段
    if not settings.database_url.startswith("sqlite"):
        return

    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(contents)")).fetchall()
        existing = {r[1] for r in rows}
        needed = {
            "publish_time": "ALTER TABLE contents ADD COLUMN publish_time VARCHAR(32) DEFAULT ''",
            "video_url": "ALTER TABLE contents ADD COLUMN video_url VARCHAR(1000) DEFAULT ''",
            "is_read": "ALTER TABLE contents ADD COLUMN is_read BOOLEAN DEFAULT 0",
            "is_starred": "ALTER TABLE contents ADD COLUMN is_starred BOOLEAN DEFAULT 0",
            "viewed_at": "ALTER TABLE contents ADD COLUMN viewed_at DATETIME",
        }
        for col, ddl in needed.items():
            if col in existing:
                continue
            conn.execute(text(ddl))


def _ensure_app_config_columns() -> None:
    # 轻量迁移：为 app_config 补齐新增字段
    if not settings.database_url.startswith("sqlite"):
        return

    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(app_config)")).fetchall()
        existing = {r[1] for r in rows}
        needed = {
            "scrape_headless": "ALTER TABLE app_config ADD COLUMN scrape_headless BOOLEAN DEFAULT 1",
            "feishu_chat_id": "ALTER TABLE app_config ADD COLUMN feishu_chat_id VARCHAR(255) DEFAULT ''",
            "login_blocked": "ALTER TABLE app_config ADD COLUMN login_blocked BOOLEAN DEFAULT 0",
            "login_blocked_at": "ALTER TABLE app_config ADD COLUMN login_blocked_at DATETIME",
            "login_block_reason": "ALTER TABLE app_config ADD COLUMN login_block_reason VARCHAR(255) DEFAULT ''",
            "login_block_keyword": "ALTER TABLE app_config ADD COLUMN login_block_keyword VARCHAR(200) DEFAULT ''",
        }
        for col, ddl in needed.items():
            if col in existing:
                continue
            conn.execute(text(ddl))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
