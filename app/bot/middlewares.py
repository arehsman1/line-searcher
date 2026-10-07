"""Auth, maintenance, and user registration middleware."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

from app.core.config import get_settings
from app.core.database import settings_repo, users_repo
from app.core.models import UserStatus


class UserMiddleware(BaseMiddleware):
    """Auto-register Free users and attach user to handler data."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = None
        tg_user = None
        if isinstance(event, Message) and event.from_user:
            tg_user = event.from_user
        elif isinstance(event, CallbackQuery) and event.from_user:
            tg_user = event.from_user

        if tg_user and not tg_user.is_bot:
            user, _ = await users_repo.get_or_create(
                user_id=tg_user.id,
                first_name=tg_user.first_name,
                username=tg_user.username,
            )
            # Admins from ADMIN_TELEGRAM_IDS are always OWNER (never Free)
            user = await users_repo.ensure_owner_if_admin(user)
            # Expiry + daily reset
            settings = get_settings()
            user = await users_repo.check_and_expire_plan(user)
            user = await users_repo.reset_daily_if_needed(user, settings.timezone)
            data["db_user"] = user

        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    """Block non-admin users during maintenance."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        rt = await settings_repo.get()
        if not rt.maintenance_mode:
            return await handler(event, data)

        settings = get_settings()
        tg_user = None
        if isinstance(event, Message) and event.from_user:
            tg_user = event.from_user
        elif isinstance(event, CallbackQuery) and event.from_user:
            tg_user = event.from_user

        if tg_user and tg_user.id in settings.admin_ids:
            return await handler(event, data)

        # Allow /start so user sees maintenance message
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)

        text = (
            "🛠 <b>LINE SEARCHER IS UNDER MAINTENANCE</b>\n\n"
            "Searches are temporarily unavailable.\n"
            "Please try again later."
        )
        if isinstance(event, Message):
            await event.answer(text, parse_mode="HTML")
        elif isinstance(event, CallbackQuery):
            await event.answer("Under maintenance", show_alert=True)
            try:
                await event.message.edit_text(text, parse_mode="HTML")  # type: ignore
            except Exception:
                pass
        return None


class BlockMiddleware(BaseMiddleware):
    """Reject blocked users."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("db_user")
        if user and user.status == UserStatus.BLOCKED:
            text = "🔴 Your access to LINE SEARCHER has been disabled.\nContact @Arehsmanbot if you believe this is an error."
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer("Access disabled", show_alert=True)
            return None
        return await handler(event, data)


def is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_ids
