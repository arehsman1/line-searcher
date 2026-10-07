"""
LINE SEARCHER - User & Settings Persistence Layer
JSON-backed with atomic writes. Designed for later SQLite/Postgres migration.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import orjson

from app.core.config import get_settings
from app.core.models import (
    DEFAULT_PLANS,
    PlanConfig,
    RuntimeSettings,
    StorageConfig,
    StorageType,
    User,
    UserStatus,
    utcnow,
    utcnow_iso,
)

logger = logging.getLogger(__name__)

_user_lock = asyncio.Lock()
_settings_lock = asyncio.Lock()
_plans_lock = asyncio.Lock()
_storage_lock = asyncio.Lock()


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE)
    tmp.write_bytes(payload)
    tmp.replace(path)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return orjson.loads(path.read_bytes())
    except Exception as e:
        logger.error("Failed to load %s: %s", path, e)
        return default


# ─── Users ───────────────────────────────────────────────────────────────────

class UserRepository:
    """Modular user store. Swap implementation later without rewriting handlers."""

    def __init__(self) -> None:
        self._cache: Dict[int, User] = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with _user_lock:
            if self._loaded:
                return
            settings = get_settings()
            raw = _load_json(settings.users_file, {})
            for uid_str, udata in raw.items():
                try:
                    u = User.model_validate(udata)
                    self._cache[u.user_id] = u
                except Exception as e:
                    logger.warning("Skip invalid user %s: %s", uid_str, e)
            self._loaded = True

    async def _save(self) -> None:
        settings = get_settings()
        data = {str(uid): u.model_dump(mode="json") for uid, u in self._cache.items()}
        _atomic_write(settings.users_file, data)

    async def get(self, user_id: int) -> Optional[User]:
        await self._ensure_loaded()
        return self._cache.get(user_id)

    def _is_admin_id(self, user_id: int) -> bool:
        """Recall admin IDs from ADMIN_TELEGRAM_IDS env config."""
        return user_id in get_settings().admin_ids

    async def ensure_owner_if_admin(self, user: User) -> User:
        """
        Admins from ADMIN_TELEGRAM_IDS are always OWNER — never Free.
        Re-applies on every interaction so config changes are picked up.
        """
        if self._is_admin_id(user.user_id):
            if user.plan != "OWNER" or user.status != UserStatus.ACTIVE:
                user.plan = "OWNER"
                user.plan_start = user.plan_start or utcnow_iso()
                user.plan_expiry = None  # never expires
                user.status = UserStatus.ACTIVE
                await self.save(user)
                logger.info("Admin %s elevated to OWNER", user.user_id)
        return user

    async def get_or_create(
        self,
        user_id: int,
        first_name: Optional[str] = None,
        username: Optional[str] = None,
    ) -> tuple[User, bool]:
        """Returns (user, created). Auto Free registration; admins become OWNER."""
        await self._ensure_loaded()
        async with _user_lock:
            is_admin = self._is_admin_id(user_id)
            if user_id in self._cache:
                u = self._cache[user_id]
                u.last_interaction = utcnow_iso()
                if first_name:
                    u.first_name = first_name
                if username:
                    u.username = username
                # Keep admins as OWNER even if they were previously FREE
                if is_admin and u.plan != "OWNER":
                    u.plan = "OWNER"
                    u.plan_start = u.plan_start or utcnow_iso()
                    u.plan_expiry = None
                    u.status = UserStatus.ACTIVE
                    logger.info("Existing admin %s elevated to OWNER", user_id)
                await self._save()
                return u, False

            plan = "OWNER" if is_admin else "FREE"
            u = User(
                user_id=user_id,
                first_name=first_name,
                username=username,
                plan=plan,
                plan_start=utcnow_iso() if is_admin else None,
                plan_expiry=None,
                status=UserStatus.ACTIVE,
                daily_searches_used=0,
            )
            self._cache[user_id] = u
            await self._save()
            if is_admin:
                logger.info("New OWNER (admin) registered: %s", user_id)
            else:
                logger.info("New Free user registered: %s", user_id)
            return u, True

    async def save(self, user: User) -> None:
        await self._ensure_loaded()
        async with _user_lock:
            user.last_interaction = utcnow_iso()
            self._cache[user.user_id] = user
            await self._save()

    async def all_users(self) -> List[User]:
        await self._ensure_loaded()
        return list(self._cache.values())

    async def count(self) -> int:
        await self._ensure_loaded()
        return len(self._cache)

    async def find(
        self,
        query: str = "",
        plan: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[User]:
        await self._ensure_loaded()
        results = []
        q = query.lower().strip()
        for u in self._cache.values():
            if plan and u.plan != plan:
                continue
            if status and u.status.value != status:
                continue
            if q:
                hay = f"{u.user_id} {u.username or ''} {u.first_name or ''}".lower()
                if q not in hay:
                    continue
            results.append(u)
        return results

    async def reset_daily_if_needed(self, user: User, tz_name: str = "UTC") -> User:
        """Reset daily counter if new day in configured timezone."""
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        today = datetime.now(tz).strftime("%Y-%m-%d")
        if user.daily_reset_date != today:
            user.daily_searches_used = 0
            user.daily_reset_date = today
            await self.save(user)
        return user

    async def check_and_expire_plan(self, user: User) -> User:
        """If paid plan expired → fallback to FREE. Keep private storage mapping.
        OWNER and admins never expire / never demote to FREE.
        """
        # Admins always stay OWNER
        if self._is_admin_id(user.user_id):
            if user.plan != "OWNER":
                user.plan = "OWNER"
                user.plan_expiry = None
                user.status = UserStatus.ACTIVE
                await self.save(user)
            return user
        if user.plan in ("FREE", "OWNER") or not user.plan_expiry:
            return user
        try:
            expiry = datetime.fromisoformat(user.plan_expiry)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if utcnow() >= expiry:
                logger.info("Plan expired for user %s: %s → FREE", user.user_id, user.plan)
                user.plan = "FREE"
                user.plan_start = None
                user.plan_expiry = None
                await self.save(user)
        except Exception as e:
            logger.error("Plan expiry check failed for %s: %s", user.user_id, e)
        return user

    async def set_plan(
        self,
        user_id: int,
        plan: str,
        duration_days: int = 0,
        custom_expiry: Optional[str] = None,
    ) -> Optional[User]:
        user = await self.get(user_id)
        if not user:
            return None
        user.plan = plan
        user.plan_start = utcnow_iso()
        if custom_expiry:
            user.plan_expiry = custom_expiry
        elif duration_days > 0:
            exp = utcnow() + timedelta(days=duration_days)
            user.plan_expiry = exp.isoformat()
        else:
            user.plan_expiry = None
        user.status = UserStatus.ACTIVE
        await self.save(user)
        return user

    async def block(self, user_id: int) -> Optional[User]:
        user = await self.get(user_id)
        if not user:
            return None
        user.status = UserStatus.BLOCKED
        await self.save(user)
        return user

    async def unblock(self, user_id: int) -> Optional[User]:
        user = await self.get(user_id)
        if not user:
            return None
        user.status = UserStatus.ACTIVE
        await self.save(user)
        return user

    async def export_raw(self) -> bytes:
        await self._ensure_loaded()
        settings = get_settings()
        if settings.users_file.exists():
            return settings.users_file.read_bytes()
        return b"{}"


# ─── Plans ───────────────────────────────────────────────────────────────────

class PlanRepository:
    def __init__(self) -> None:
        self._plans: Dict[str, PlanConfig] = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with _plans_lock:
            if self._loaded:
                return
            settings = get_settings()
            raw = _load_json(settings.plans_file, None)
            if raw is None:
                self._plans = {k: v.model_copy() for k, v in DEFAULT_PLANS.items()}
                await self._save()
            else:
                for k, v in raw.items():
                    try:
                        self._plans[k] = PlanConfig.model_validate(v)
                    except Exception:
                        pass
                # Ensure defaults exist
                for k, v in DEFAULT_PLANS.items():
                    if k not in self._plans:
                        self._plans[k] = v.model_copy()
            self._loaded = True

    async def _save(self) -> None:
        settings = get_settings()
        data = {k: p.model_dump(mode="json") for k, p in self._plans.items()}
        _atomic_write(settings.plans_file, data)

    async def get(self, plan_id: str) -> Optional[PlanConfig]:
        await self._ensure_loaded()
        return self._plans.get(plan_id)

    async def all(self) -> Dict[str, PlanConfig]:
        await self._ensure_loaded()
        return dict(self._plans)

    async def update(self, plan: PlanConfig) -> None:
        await self._ensure_loaded()
        async with _plans_lock:
            self._plans[plan.id] = plan
            await self._save()

    async def daily_limit_for(self, plan_id: str) -> int:
        p = await self.get(plan_id)
        if not p:
            return 3
        return p.daily_searches


# ─── Runtime Settings ────────────────────────────────────────────────────────

class SettingsRepository:
    def __init__(self) -> None:
        self._settings: Optional[RuntimeSettings] = None

    async def get(self) -> RuntimeSettings:
        if self._settings is not None:
            return self._settings
        async with _settings_lock:
            path = get_settings().settings_file
            raw = _load_json(path, None)
            if raw is None:
                self._settings = RuntimeSettings()
                await self._save()
            else:
                self._settings = RuntimeSettings.model_validate(raw)
            return self._settings

    async def _save(self) -> None:
        path = get_settings().settings_file
        if self._settings:
            self._settings.last_updated = utcnow_iso()
            _atomic_write(path, self._settings.model_dump(mode="json"))

    async def update(self, **kwargs) -> RuntimeSettings:
        s = await self.get()
        async with _settings_lock:
            for k, v in kwargs.items():
                if hasattr(s, k):
                    setattr(s, k, v)
            await self._save()
            return s


# ─── Storage Config ──────────────────────────────────────────────────────────

class StorageRepository:
    def __init__(self) -> None:
        self._config: Dict[str, StorageConfig] = {}
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with _storage_lock:
            if self._loaded:
                return
            path = get_settings().storage_config_file
            raw = _load_json(path, {})
            for k, v in raw.items():
                try:
                    self._config[k] = StorageConfig.model_validate(v)
                except Exception:
                    pass
            if "public" not in self._config:
                self._config["public"] = StorageConfig(
                    storage_id="public",
                    storage_type=StorageType.PUBLIC,
                    display_name="",
                    status="not_configured",
                )
            self._loaded = True

    async def _save(self) -> None:
        path = get_settings().storage_config_file
        data = {k: c.model_dump(mode="json") for k, c in self._config.items()}
        _atomic_write(path, data)

    async def get_public(self) -> StorageConfig:
        await self._ensure_loaded()
        return self._config["public"]

    async def set_public(self, chat_id: int, display_name: str) -> StorageConfig:
        await self._ensure_loaded()
        async with _storage_lock:
            cfg = StorageConfig(
                storage_id="public",
                storage_type=StorageType.PUBLIC,
                chat_id=chat_id,
                display_name=display_name,
                configured_at=utcnow_iso(),
                status="active",
            )
            self._config["public"] = cfg
            await self._save()
            return cfg

    async def remove_public(self) -> None:
        await self._ensure_loaded()
        async with _storage_lock:
            self._config["public"] = StorageConfig(
                storage_id="public",
                storage_type=StorageType.PUBLIC,
                status="not_configured",
            )
            await self._save()

    async def update_public_stats(self, file_count: int, total_size: int) -> None:
        await self._ensure_loaded()
        async with _storage_lock:
            cfg = self._config["public"]
            cfg.file_count = file_count
            cfg.total_size_bytes = total_size
            cfg.last_refresh = utcnow_iso()
            await self._save()


# Singletons
users_repo = UserRepository()
plans_repo = PlanRepository()
settings_repo = SettingsRepository()
storage_repo = StorageRepository()
