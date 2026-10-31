from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ContentItem(BaseModel):
    platform: str
    title: str
    author: str = ""
    url: str
    thumbnail: str = ""
    likes: int = 0
    comments: int = 0
    shares: int = 0
    publish_time: str = ""
    video_url: str = ""
    keyword: str = ""
    source: str = "auto"


class ContentResponse(ContentItem):
    id: int
    is_read: bool = False
    is_starred: bool = False
    collected_at: datetime

    model_config = {"from_attributes": True}


class FeedItemResponse(BaseModel):
    id: int
    platform: str
    keyword: str
    title: str
    author: str = ""
    url: str
    thumbnail: str = ""
    video_url: str = ""
    likes: int = 0
    publish_time: str = ""
    is_read: bool = False
    is_starred: bool = False
    collected_at: datetime

    model_config = {"from_attributes": True}


class CollectRequest(BaseModel):
    url: str
    title: str = ""
    author: str = ""
    platform: str = ""
    likes: int = 0
    comments: int = 0
    shares: int = 0
    keyword: str = ""
    source: str = "manual"


class ScrapeTriggerRequest(BaseModel):
    keyword_ids: list[int] = Field(default_factory=list)
    scrape_headless: Optional[bool] = None


class ScrapeTriggerResponse(BaseModel):
    detail: str
    task_id: str
    status: str
    queued_at: datetime
    position: int
    scope: str
    keyword_count: int
    scrape_headless: Optional[bool] = None


class KeywordCreate(BaseModel):
    keyword: str
    platforms: list[str] = Field(default_factory=lambda: ["xiaohongshu"])
    min_likes: int = 0
    enabled: bool = True


class KeywordResponse(KeywordCreate):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ScrapeResult(BaseModel):
    keyword: str
    platform: str
    count: int
    items: list[ContentItem]


class ScheduleConfigUpdate(BaseModel):
    enabled: bool
    hour: int
    minute: int


class ScheduleConfigResponse(ScheduleConfigUpdate):
    updated_at: datetime


class ScheduleSlotItem(BaseModel):
    hour: int
    minute: int
    enabled: bool = True


class ScheduleMultiUpdate(BaseModel):
    enabled: bool
    slots: list[ScheduleSlotItem]


class ScheduleMultiResponse(BaseModel):
    enabled: bool
    slots: list[ScheduleSlotItem]
    updated_at: datetime


class DashboardBootstrapResponse(BaseModel):
    api_key: str
    feishu_webhook_url: str = ""
    feishu_chat_id: str = ""
    schedule: ScheduleMultiResponse
    scrape_headless: bool = True


class SystemConfigUpdate(BaseModel):
    api_key: str
    feishu_webhook_url: str = ""
    feishu_chat_id: str = ""
    scrape_headless: Optional[bool] = None


class SystemConfigResponse(BaseModel):
    api_key: str
    feishu_webhook_url: str = ""
    feishu_chat_id: str = ""
    updated_at: datetime
    scrape_headless: bool = True


class PushLogResponse(BaseModel):
    id: int
    pushed_at: datetime
    keyword: str
    content_count: int
    status: str
    detail: str = ""

    model_config = {"from_attributes": True}


class HistorySummary(BaseModel):
    total_runs: int
    success_runs: int
    failed_runs: int
    total_contents: int


class HistoryListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    summary: HistorySummary
    items: list[PushLogResponse]


class RiskEventResponse(BaseModel):
    id: int
    platform: str
    keyword: str
    detected_at: datetime
    flags: str
    stage: str
    screenshot_path: str = ""
    html_path: str = ""
    screenshot_url: str = ""
    html_url: str = ""
    recovery_hint: str = ""
    artifact_token_supported: bool = True

    model_config = {"from_attributes": True}


class RiskStatsResponse(BaseModel):
    total_events: int
    recent_events_24h: int
    last_event_at: Optional[datetime]
    platform_breakdown: dict[str, int]


class ScrapeTaskResponse(BaseModel):
    task_id: str
    trigger_source: str
    status: str
    requested_keyword_ids: list[int] = Field(default_factory=list)
    resolved_keyword_ids: list[int] = Field(default_factory=list)
    scrape_headless: Optional[bool] = None
    queued_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_code: str = ""
    error_message: str = ""
    result_summary: str = ""
    request_id: str = ""
    queue_position: int = 0
    retry_count: int = 0
    failure_category: str = ""
    failed_history_log_id: Optional[int] = None


class RuntimeStatusResponse(BaseModel):
    backend_ok: bool
    scheduler_ok: bool
    task_runner_ok: bool
    local_health_code: int
    public_health_code: int
    login_blocked: bool = False
    login_blocked_at: str = ""
    login_block_reason: str = ""
    login_block_keyword: str = ""
    tunnel_last_reachable_at: str = ""
    last_checked_at: str


class RiskArtifactTokenResponse(BaseModel):
    event_id: int
    kind: str
    token: str
    expires_at: str
