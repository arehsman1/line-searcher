"""
LINE SEARCHER - Central Configuration
All business rules and paths are configurable. Secrets come only from env.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Secrets ---
    bot_token: str = Field(..., alias="BOT_TOKEN")
    admin_telegram_ids: str = Field(..., alias="ADMIN_TELEGRAM_IDS")
    api_id: Optional[int] = Field(None, alias="API_ID")
    api_hash: Optional[str] = Field(None, alias="API_HASH")

    # --- Local Bot API ---
    use_local_bot_api: bool = Field(True, alias="USE_LOCAL_BOT_API")
    local_bot_api_url: str = Field("http://127.0.0.1:8081", alias="LOCAL_BOT_API_URL")

    # --- Paths ---
    data_dir: Path = Field(Path("/opt/line-searcher/data"), alias="DATA_DIR")
    log_dir: Path = Field(Path("/opt/line-searcher/logs"), alias="LOG_DIR")

    # --- Timezone ---
    timezone: str = Field("UTC", alias="TIMEZONE")

    # --- Free plan ---
    free_daily_searches: int = Field(3, alias="FREE_DAILY_SEARCHES")

    # --- Concurrency & limits ---
    max_concurrent_searches: int = Field(2, alias="MAX_CONCURRENT_SEARCHES")
    max_active_jobs_per_user: int = Field(3, alias="MAX_ACTIVE_JOBS_PER_USER")
    max_queue_size: int = Field(50, alias="MAX_QUEUE_SIZE")
    result_retention_seconds: int = Field(3600, alias="RESULT_RETENTION_SECONDS")
    failed_result_retention_seconds: int = Field(7200, alias="FAILED_RESULT_RETENTION_SECONDS")
    max_result_file_size_mb: int = Field(49, alias="MAX_RESULT_FILE_SIZE_MB")  # under 50MB for cloud fallback
    max_result_parts: int = Field(50, alias="MAX_RESULT_PARTS")

    # --- Search defaults ---
    default_case_sensitive: bool = Field(False, alias="DEFAULT_CASE_SENSITIVE")
    search_subfolders: bool = Field(False, alias="SEARCH_SUBFOLDERS")
    progress_update_interval_sec: float = Field(2.0, alias="PROGRESS_UPDATE_INTERVAL_SEC")

    # --- Broadcast ---
    broadcast_rate_per_second: float = Field(25.0, alias="BROADCAST_RATE_PER_SECOND")
    broadcast_expiring_soon_hours: int = Field(24, alias="BROADCAST_EXPIRING_SOON_HOURS")

    # --- Maintenance ---
    maintenance_mode: bool = Field(False, alias="MAINTENANCE_MODE")

    # --- Resource protection ---
    max_temp_disk_gb: float = Field(50.0, alias="MAX_TEMP_DISK_GB")
    disk_warning_percent: float = Field(85.0, alias="DISK_WARNING_PERCENT")

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if isinstance(v, list):
            return ",".join(str(x) for x in v)
        return str(v)

    @property
    def admin_ids(self) -> List[int]:
        if not self.admin_telegram_ids:
            return []
        return [int(x.strip()) for x in self.admin_telegram_ids.split(",") if x.strip()]

    @property
    def users_file(self) -> Path:
        return self.data_dir / "users" / "users.json"

    @property
    def catalog_dir(self) -> Path:
        return self.data_dir / "catalog"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "temp"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def plans_file(self) -> Path:
        return self.data_dir / "plans.json"

    @property
    def storage_config_file(self) -> Path:
        return self.data_dir / "storage_config.json"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.users_file.parent,
            self.catalog_dir,
            self.jobs_dir,
            self.results_dir,
            self.temp_dir,
            self.log_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


# Singleton
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    _settings.ensure_dirs()
    return _settings
