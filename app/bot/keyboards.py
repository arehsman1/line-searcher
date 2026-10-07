"""
LINE SEARCHER - Inline Keyboards
Clean, consistent, premium-looking button layouts.
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.core.models import PlanConfig


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🔎 SEARCH", callback_data="search:start"),
        InlineKeyboardButton(text="📂 STORAGE", callback_data="storage:view"),
    )
    b.row(
        InlineKeyboardButton(text="💎 UPGRADE", callback_data="plans:view"),
        InlineKeyboardButton(text="📊 MY JOBS", callback_data="jobs:my"),
    )
    b.row(
        InlineKeyboardButton(text="👤 ACCOUNT", callback_data="account:view"),
        InlineKeyboardButton(text="❓ HELP", callback_data="help:view"),
    )
    return b.as_markup()


def back_home() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🏠 HOME", callback_data="home"))
    return b.as_markup()


def search_confirm(case_sensitive: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    cs = "ON ✅" if case_sensitive else "OFF"
    b.row(InlineKeyboardButton(text=f"🔤 Case-sensitive: {cs}", callback_data="search:toggle_case"))
    b.row(
        InlineKeyboardButton(text="🚀 START SEARCH", callback_data="search:confirm"),
        InlineKeyboardButton(text="❌ CANCEL", callback_data="search:cancel"),
    )
    return b.as_markup()


def search_cancel_btn(job_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="❌ CANCEL SEARCH", callback_data=f"job:cancel:{job_id}"))
    return b.as_markup()


def plans_keyboard(plans: dict[str, PlanConfig]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    order = ["FREE", "STARTER", "STANDARD", "PRO", "UNLIMITED"]
    for pid in order:
        p = plans.get(pid)
        if not p or not p.enabled:
            continue
        label = p.display_name
        if p.is_popular:
            label += " ⭐"
        b.row(InlineKeyboardButton(text=label, callback_data=f"plans:select:{pid}"))
    b.row(InlineKeyboardButton(text="💬 CONTACT @Arehsmanbot", url="https://t.me/Arehsmanbot"))
    b.row(InlineKeyboardButton(text="⬅️ BACK", callback_data="home"))
    return b.as_markup()


def upgrade_request(plan_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="💬 CONTACT ADMIN", url="https://t.me/Arehsmanbot"))
    b.row(InlineKeyboardButton(text="⬅️ BACK", callback_data="plans:view"))
    return b.as_markup()


def admin_panel() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="👥 USERS", callback_data="admin:users"),
        InlineKeyboardButton(text="📂 STORAGE", callback_data="admin:storage"),
    )
    b.row(
        InlineKeyboardButton(text="💎 PLANS", callback_data="admin:plans"),
        InlineKeyboardButton(text="🔎 JOBS", callback_data="admin:jobs"),
    )
    b.row(
        InlineKeyboardButton(text="📊 STATS", callback_data="admin:stats"),
        InlineKeyboardButton(text="📢 BROADCAST", callback_data="admin:broadcast"),
    )
    b.row(
        InlineKeyboardButton(text="⚙️ SETTINGS", callback_data="admin:settings"),
        InlineKeyboardButton(text="🛠 MAINTENANCE", callback_data="admin:maintenance"),
    )
    b.row(InlineKeyboardButton(text="🔄 REFRESH STORAGE", callback_data="admin:refresh"))
    b.row(InlineKeyboardButton(text="🏠 USER HOME", callback_data="home"))
    return b.as_markup()


def admin_storage_menu(configured: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if configured:
        b.row(
            InlineKeyboardButton(text="✏️ CHANGE", callback_data="admin:storage:change"),
            InlineKeyboardButton(text="🔄 REFRESH", callback_data="admin:storage:refresh"),
        )
        b.row(
            InlineKeyboardButton(text="📋 FILES", callback_data="admin:storage:files"),
            InlineKeyboardButton(text="❌ REMOVE", callback_data="admin:storage:remove"),
        )
    else:
        b.row(InlineKeyboardButton(text="➕ SET PUBLIC STORAGE", callback_data="admin:storage:set"))
    b.row(InlineKeyboardButton(text="⬅️ BACK", callback_data="admin:panel"))
    return b.as_markup()


def admin_storage_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ USE THIS STORAGE", callback_data="admin:storage:confirm"),
        InlineKeyboardButton(text="❌ CANCEL", callback_data="admin:storage"),
    )
    return b.as_markup()


def admin_refresh_now() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ REFRESH NOW", callback_data="admin:storage:refresh"),
        InlineKeyboardButton(text="⏳ LATER", callback_data="admin:panel"),
    )
    return b.as_markup()


def broadcast_filters() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="👥 ALL USERS", callback_data="broadcast:filter:all"))
    b.row(
        InlineKeyboardButton(text="🆓 FREE USERS", callback_data="broadcast:filter:free"),
        InlineKeyboardButton(text="💎 PAID USERS", callback_data="broadcast:filter:paid"),
    )
    b.row(
        InlineKeyboardButton(text="🟢 ACTIVE USERS", callback_data="broadcast:filter:active"),
        InlineKeyboardButton(text="⏰ EXPIRING SOON", callback_data="broadcast:filter:expiring_soon"),
    )
    b.row(InlineKeyboardButton(text="❌ CANCEL", callback_data="admin:panel"))
    return b.as_markup()


def broadcast_confirm(bid: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="✅ SEND BROADCAST", callback_data=f"broadcast:send:{bid}"),
        InlineKeyboardButton(text="❌ CANCEL", callback_data="admin:panel"),
    )
    return b.as_markup()


def broadcast_cancel_confirm(bid: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(
        InlineKeyboardButton(text="🛑 CANCEL BROADCAST", callback_data=f"broadcast:cancel:{bid}"),
        InlineKeyboardButton(text="↩️ CONTINUE", callback_data="admin:panel"),
    )
    return b.as_markup()


def maintenance_toggle(enabled: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if enabled:
        b.row(InlineKeyboardButton(text="🟢 DISABLE MAINTENANCE", callback_data="admin:maintenance:off"))
    else:
        b.row(InlineKeyboardButton(text="🔴 ENABLE MAINTENANCE", callback_data="admin:maintenance:on"))
    b.row(InlineKeyboardButton(text="⬅️ BACK", callback_data="admin:panel"))
    return b.as_markup()
