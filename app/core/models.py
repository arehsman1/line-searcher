"""
LINE SEARCHER - Data Models
Modular so later migration to SQLite/PostgreSQL is straightforward.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


# ─── Plans ───────────────────────────────────────────────────────────────────

class PlanId(str, Enum):
    FREE = "FREE"
    STARTER = "STARTER"
    STANDARD = "STANDARD"
    PRO = "PRO"
    UNLIMITED = "UNLIMITED"
    OWNER = "OWNER"
    CUSTOM = "CUSTOM"


class PlanConfig(BaseModel):
    id: str
    display_name: str
    price_usd: float = 0.0
    duration_days: int = 0  # 0 = never expires (FREE / OWNER)
    daily_searches: int = 3  # -1 = unlimited (still resource-limited)
    storage_access: List[str] = Field(default_factory=lambda: ["public"])  # public | private
    enabled: bool = True
    is_popular: bool = False
    description: str = ""


DEFAULT_PLANS: Dict[str, PlanConfig] = {
    "FREE": PlanConfig(
        id="FREE",
        display_name="🆓 FREE",
        price_usd=0.0,
        duration_days=0,
        daily_searches=3,
        storage_access=["public"],
        description="3 searches/day • Public Free Storage",
    ),
    "STARTER": PlanConfig(
        id="STARTER",
        display_name="🟢 STARTER",
        price_usd=5.0,
        duration_days=15,
        daily_searches=20,
        storage_access=["public", "private"],
        description="$5 • 15 days • 20 searches/day",
    ),
    "STANDARD": PlanConfig(
        id="STANDARD",
        display_name="🔵 STANDARD",
        price_usd=10.0,
        duration_days=35,
        daily_searches=50,
        storage_access=["public", "private"],
        is_popular=True,
        description="$10 • 35 days • 50 searches/day",
    ),
    "PRO": PlanConfig(
        id="PRO",
        display_name="🟣 PRO",
        price_usd=20.0,
        duration_days=60,
        daily_searches=150,
        storage_access=["public", "private"],
        description="$20 • 60 days • Higher search limit",
    ),
    "UNLIMITED": PlanConfig(
        id="UNLIMITED",
        display_name="💎 UNLIMITED",
        price_usd=0.0,  # custom
        duration_days=0,  # custom
        daily_searches=-1,
        storage_access=["public", "private"],
        description="Custom price • Custom duration • No daily counter",
    ),
    # Owner plan: assigned automatically to ADMIN_TELEGRAM_IDS. Not sold via upgrade UI.
    "OWNER": PlanConfig(
        id="OWNER",
        display_name="👑 OWNER",
        price_usd=0.0,
        duration_days=0,
        daily_searches=-1,
        storage_access=["public", "private"],
        enabled=False,  # hidden from public plan list
        description="Bot owner — full access, no daily limits",
    ),
}


# ─── User ────────────────────────────────────────────────────────────────────

class UserStatus(str, Enum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class User(BaseModel):
    user_id: int
    first_name: Optional[str] = None
    username: Optional[str] = None
    registration_date: str = Field(default_factory=utcnow_iso)
    last_interaction: str = Field(default_factory=utcnow_iso)
    plan: str = "FREE"
    plan_start: Optional[str] = None
    plan_expiry: Optional[str] = None
    daily_searches_used: int = 0
    daily_reset_date: str = Field(default_factory=lambda: utcnow().strftime("%Y-%m-%d"))
    private_storage_chat_id: Optional[int] = None
    private_storage_name: Optional[str] = None
    status: UserStatus = UserStatus.ACTIVE
    notes: str = ""
    total_searches: int = 0

    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE

    def is_owner(self) -> bool:
        return self.plan == "OWNER"

    def is_paid(self) -> bool:
        return self.plan not in ("FREE",)


# ─── Storage ─────────────────────────────────────────────────────────────────

class StorageType(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"


class StorageConfig(BaseModel):
    storage_id: str  # "public" or "user_<id>"
    storage_type: StorageType
    chat_id: Optional[int] = None
    display_name: str = ""
    configured_at: Optional[str] = None
    last_refresh: Optional[str] = None
    file_count: int = 0
    total_size_bytes: int = 0
    status: str = "not_configured"  # not_configured | active | error


class CatalogEntry(BaseModel):
    chat_id: int
    message_id: int
    file_id: str
    file_unique_id: Optional[str] = None
    file_name: str
    file_size: int = 0
    mime_type: Optional[str] = None
    indexed_at: str = Field(default_factory=utcnow_iso)
    status: str = "available"  # available | missing | encoding_error
    storage_id: str = "public"


# ─── Jobs ────────────────────────────────────────────────────────────────────

class JobStatus(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    DOWNLOADING = "DOWNLOADING"
    SEARCHING = "SEARCHING"
    GENERATING_RESULTS = "GENERATING_RESULTS"
    DELIVERING = "DELIVERING"
    COMPLETED = "COMPLETED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    INTERRUPTED = "INTERRUPTED"


class SearchJob(BaseModel):
    job_id: str
    user_id: int
    plan: str
    keywords: List[str]
    case_sensitive: bool = False
    storage_ids: List[str] = Field(default_factory=lambda: ["public"])
    status: JobStatus = JobStatus.CREATED
    created_at: str = Field(default_factory=utcnow_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    progress_percent: float = 0.0
    processed_bytes: int = 0
    total_bytes: int = 0
    current_file: str = ""
    matches_found: int = 0
    files_scanned: int = 0
    result_files: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    progress_message_id: Optional[int] = None
    progress_chat_id: Optional[int] = None
    result_dir: Optional[str] = None
    speed_bps: float = 0.0
    eta_seconds: Optional[float] = None


# ─── Broadcast ───────────────────────────────────────────────────────────────

class BroadcastStatus(str, Enum):
    CREATED = "CREATED"
    CONFIRMING = "CONFIRMING"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class BroadcastFilter(str, Enum):
    ALL = "all"
    FREE = "free"
    PAID = "paid"
    ACTIVE = "active"
    EXPIRING_SOON = "expiring_soon"


class BroadcastJob(BaseModel):
    broadcast_id: str
    admin_id: int
    filter: BroadcastFilter = BroadcastFilter.ALL
    status: BroadcastStatus = BroadcastStatus.CREATED
    created_at: str = Field(default_factory=utcnow_iso)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    total_recipients: int = 0
    sent: int = 0
    failed: int = 0
    blocked: int = 0
    message_type: str = "text"  # text | photo | video | document
    text: Optional[str] = None
    file_id: Optional[str] = None
    caption: Optional[str] = None
    progress_message_id: Optional[int] = None
    progress_chat_id: Optional[int] = None


# ─── Runtime Settings (persisted) ────────────────────────────────────────────

class RuntimeSettings(BaseModel):
    maintenance_mode: bool = False
    free_daily_searches: int = 3
    max_concurrent_searches: int = 2
    result_retention_seconds: int = 3600
    timezone: str = "UTC"
    search_subfolders: bool = False
    default_case_sensitive: bool = False
    broadcast_rate: float = 25.0
    expiring_soon_hours: int = 24
    last_updated: str = Field(default_factory=utcnow_iso)
