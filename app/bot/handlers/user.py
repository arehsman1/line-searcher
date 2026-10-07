"""
LINE SEARCHER - User-facing handlers
"""
from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    back_home,
    main_menu,
    plans_keyboard,
    search_cancel_btn,
    search_confirm,
    upgrade_request,
)
from app.bot.middlewares import is_admin
from app.bot.states import SearchStates
from app.core.config import get_settings
from app.core.database import plans_repo, settings_repo, storage_repo, users_repo
from app.core.models import User
from app.jobs.manager import job_manager
from app.utils.helpers import escape_html, format_bytes

logger = logging.getLogger(__name__)
router = Router(name="user")


def _home_text(user: User, storage_name: str = "Not configured") -> str:
    if user.plan == "OWNER":
        plan_icon = "👑"
        plan_label = "OWNER"
        searches_line = "📊 Searches: <b>Unlimited</b> (owner)"
    elif user.plan == "UNLIMITED":
        plan_icon = "💎"
        plan_label = user.plan
        searches_line = "📊 Searches: <b>Unlimited</b>"
    else:
        plan_icon = "🆓" if user.plan == "FREE" else "💎"
        plan_label = user.plan
        searches_line = f"📊 Today: <b>{user.daily_searches_used}</b> searches used"
    return (
        f"🔎 <b>LINE SEARCHER</b>\n"
        f"Search large TXT datasets directly from Telegram.\n\n"
        f"🟢 Account: <b>ACTIVE</b>\n"
        f"{plan_icon} Plan: <b>{escape_html(plan_label)}</b>\n"
        f"{searches_line}\n"
        f"📂 Storage: {escape_html(storage_name)}"
    )


async def _get_storage_label(user: User) -> str:
    pub = await storage_repo.get_public()
    parts = []
    if pub.status == "active" and pub.display_name:
        parts.append(pub.display_name)
    elif pub.status == "active":
        parts.append("Public Free Storage")
    else:
        parts.append("Public storage not configured")
    if user.plan != "FREE" and user.private_storage_name:
        parts.append(f"💎 {user.private_storage_name}")
    return " + ".join(parts)


@router.message(CommandStart())
async def cmd_start(message: Message, db_user: User, state: FSMContext) -> None:
    await state.clear()
    rt = await settings_repo.get()
    if rt.maintenance_mode and not is_admin(message.from_user.id):
        await message.answer(
            "🛠 <b>LINE SEARCHER IS UNDER MAINTENANCE</b>\n\n"
            "Searches are temporarily unavailable.\nPlease try again later.",
            parse_mode="HTML",
        )
        return

    storage_label = await _get_storage_label(db_user)
    plans = await plans_repo.all()
    free_plan = plans.get("FREE")
    daily = free_plan.daily_searches if free_plan else 3

    if db_user.plan == "OWNER" or is_admin(message.from_user.id):
        text = (
            f"🔎 <b>LINE SEARCHER</b>\n\n"
            f"Welcome, <b>Owner</b>.\n"
            f"Your admin ID is recognized from configuration.\n\n"
            f"👑 <b>OWNER ACCESS</b>\n"
            f"• Unlimited searches\n"
            f"• Full storage access\n"
            f"• Admin panel: /admin\n"
            f"• No daily limits\n\n"
            f"{_home_text(db_user, storage_label)}"
        )
    else:
        text = (
            f"🔎 <b>LINE SEARCHER</b>\n\n"
            f"Welcome to fast TXT searching built for large datasets.\n"
            f"Your Free account is ready.\n\n"
            f"🆓 <b>FREE ACCESS</b>\n"
            f"• {daily} searches every day\n"
            f"• Public Free Storage\n"
            f"• Large TXT search support\n"
            f"• Instant Telegram results\n\n"
            f"{_home_text(db_user, storage_label)}"
        )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())


@router.callback_query(F.data == "home")
async def cb_home(callback: CallbackQuery, db_user: User, state: FSMContext) -> None:
    await state.clear()
    storage_label = await _get_storage_label(db_user)
    text = _home_text(db_user, storage_label)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu())  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "account:view")
async def cb_account(callback: CallbackQuery, db_user: User) -> None:
    plans = await plans_repo.all()
    plan = plans.get(db_user.plan)
    limit = plan.daily_searches if plan else 3
    limit_str = "∞" if limit < 0 else str(limit)
    storage_label = await _get_storage_label(db_user)
    if db_user.plan in ("FREE", "OWNER") or not db_user.plan_expiry:
        expiry_line = "Never"
    else:
        expiry_line = escape_html(str(db_user.plan_expiry)[:19])
    role = "👑 Owner (admin)" if db_user.plan == "OWNER" or is_admin(db_user.user_id) else escape_html(db_user.plan)
    text = (
        f"👤 <b>MY ACCOUNT</b>\n\n"
        f"Telegram ID: <code>{db_user.user_id}</code>\n"
        f"Username: @{escape_html(db_user.username or '—')}\n"
        f"Plan: <b>{role}</b>\n"
        f"Expires: {expiry_line}\n"
        f"Searches today: <b>{db_user.daily_searches_used} / {limit_str}</b>\n"
        f"Total searches: {db_user.total_searches}\n"
        f"Storage: {escape_html(storage_label)}\n"
        f"Status: {'🟢 Active' if db_user.is_active() else '🔴 Disabled'}"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_home())  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "storage:view")
async def cb_storage(callback: CallbackQuery, db_user: User) -> None:
    pub = await storage_repo.get_public()
    lines = ["📂 <b>MY STORAGE</b>\n"]
    if pub.status == "active":
        lines.append(f"🆓 <b>Public Free Storage</b>")
        lines.append(f"Name: {escape_html(pub.display_name or '—')}")
        lines.append(f"Files: {pub.file_count:,}")
        if pub.total_size_bytes:
            lines.append(f"Size: {format_bytes(pub.total_size_bytes)}")
    else:
        lines.append("🔴 Public Free Storage is not configured yet.")
        lines.append("Contact admin or wait until storage is set up.")

    if db_user.plan != "FREE":
        lines.append("")
        if db_user.private_storage_chat_id:
            lines.append(f"💎 <b>Private Storage</b>")
            lines.append(f"Name: {escape_html(db_user.private_storage_name or '—')}")
        else:
            lines.append("💎 Private storage: available on your plan (contact admin to link)")
    else:
        lines.append("")
        lines.append("💎 Private storage: upgrade to unlock")

    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=back_home())  # type: ignore
    await callback.answer()


def _user_help_text() -> str:
    return (
        "❓ <b>HELP — LINE SEARCHER</b>\n\n"
        "Search large TXT datasets stored on Telegram.\n"
        "Prefer the <b>buttons</b> on the main menu — you do not need to remember commands.\n\n"
        "<b>How to search</b>\n"
        "1. Tap 🔎 SEARCH (or /search)\n"
        "2. Enter one or more keywords (one per line)\n"
        "3. Confirm and watch live progress\n"
        "4. Receive result files with exact matching lines\n\n"
        "<b>User commands</b>\n"
        "<code>/start</code> — Register and open the main menu\n"
        "<code>/help</code> — Show how LINE SEARCHER works\n"
        "<code>/search</code> — Start a new search\n"
        "<code>/jobs</code> — View current and previous search jobs\n"
        "<code>/storage</code> — View the storage available to you\n"
        "<code>/account</code> — Plan, searches remaining, and expiry\n"
        "<code>/upgrade</code> — Paid plans and upgrade instructions\n"
        "<code>/cancel</code> — Cancel your active search\n"
        "<code>/settings</code> — Search and account settings\n\n"
        "<b>Free access</b>\n"
        "• Automatic on /start\n"
        "• Daily search limit (resets every day)\n"
        "• Public Free Storage only\n\n"
        "<b>Paid plans</b>\n"
        "Higher limits + <b>private storage</b> (paid only).\n"
        "Contact @Arehsmanbot to upgrade.\n\n"
        "<b>Support</b>\n"
        "💬 @Arehsmanbot"
    )


def _admin_help_text() -> str:
    return (
        "❓ <b>HELP — LINE SEARCHER (ADMIN)</b>\n\n"
        "Full command list. Main UI also uses buttons.\n\n"
        "<b>User commands</b>\n"
        "<code>/start</code> — Register and open the main menu\n"
        "<code>/help</code> — Show this help\n"
        "<code>/search</code> — Start a new search\n"
        "<code>/jobs</code> — View jobs (yours; admins see system jobs via /admin)\n"
        "<code>/storage</code> — View your storage\n"
        "<code>/account</code> — Plan, searches, expiry\n"
        "<code>/upgrade</code> — Paid plans\n"
        "<code>/cancel</code> — Cancel your active search\n"
        "<code>/settings</code> — User settings\n\n"
        "👑 <b>Admin commands</b>\n"
        "(Only configured admin Telegram IDs)\n"
        "<code>/admin</code> — Open Admin Panel\n"
        "<code>/users</code> — View / search users\n"
        "<code>/user USER_ID</code> — View one user's account\n"
        "<code>/allow USER_ID [PLAN] [DAYS]</code> — Activate / assign access\n"
        "<code>/deny USER_ID</code> — Disable a user's access\n"
        "<code>/plan USER_ID [PLAN] [DAYS]</code> — View or change plan\n"
        "<code>/stats</code> — System statistics\n"
        "<code>/cancel JOB_ID</code> — Cancel any job\n"
        "<code>/publicstorage</code> — Configure Public Free Storage\n"
        "<code>/privatestorage USER_ID [CHAT_ID] [NAME]</code> — Link private storage\n"
        "<code>/broadcast</code> — Send a broadcast\n"
        "<code>/maintenance on|off</code> — Maintenance mode\n"
        "<code>/refresh</code> — Refresh public catalog (notifies all users)\n"
        "<code>/export</code> — Export users.json\n"
        "<code>/settings</code> — System settings (admin)\n\n"
        "<b>Notes</b>\n"
        "• Private storage is for <b>paid users only</b>\n"
        "• After /refresh, <b>all users</b> are notified that the public list was updated\n"
        "• Support: @Arehsmanbot"
    )


@router.message(Command("help"))
async def cmd_help(message: Message, db_user: User) -> None:
    if is_admin(message.from_user.id) or db_user.plan == "OWNER":
        text = _admin_help_text()
    else:
        text = _user_help_text()
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())


@router.callback_query(F.data == "help:view")
async def cb_help(callback: CallbackQuery, db_user: User) -> None:
    if is_admin(callback.from_user.id) or db_user.plan == "OWNER":
        text = _admin_help_text()
    else:
        text = _user_help_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_home())  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "plans:view")
async def cb_plans(callback: CallbackQuery) -> None:
    plans = await plans_repo.all()
    lines = ["💎 <b>CHOOSE YOUR ACCESS</b>\n"]
    order = ["FREE", "STARTER", "STANDARD", "PRO", "UNLIMITED"]
    for pid in order:
        p = plans.get(pid)
        if not p or not p.enabled:
            continue
        star = " ⭐" if p.is_popular else ""
        lines.append(f"{p.display_name}{star}")
        lines.append(f"{p.description}")
        lines.append("")
    lines.append("Contact @Arehsmanbot to activate a paid plan.")
    await callback.message.edit_text(  # type: ignore
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=plans_keyboard(plans),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("plans:select:"))
async def cb_plan_select(callback: CallbackQuery) -> None:
    plan_id = callback.data.split(":")[-1]  # type: ignore
    plans = await plans_repo.all()
    p = plans.get(plan_id)
    if not p:
        await callback.answer("Plan not found", show_alert=True)
        return
    if plan_id == "FREE":
        await callback.answer("You already have Free access", show_alert=True)
        return
    text = (
        f"💎 <b>UPGRADE REQUEST</b>\n\n"
        f"You selected: <b>{escape_html(p.display_name)}</b>\n"
        f"Price: ${p.price_usd:g}\n"
        f"Duration: {p.duration_days} days\n"
        f"Search limit: {p.daily_searches if p.daily_searches >= 0 else 'Custom'}/day\n\n"
        f"To activate this plan, contact @Arehsmanbot."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=upgrade_request(plan_id))  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "jobs:my")
async def cb_my_jobs(callback: CallbackQuery, db_user: User) -> None:
    jobs = job_manager.user_jobs(db_user.user_id, limit=15)
    if not jobs:
        text = "📊 <b>MY JOBS</b>\n\nNo jobs yet. Start a search!"
    else:
        lines = ["📊 <b>MY JOBS</b>\n"]
        icons = {
            "COMPLETED": "🟢",
            "SEARCHING": "🟠",
            "QUEUED": "⏳",
            "DOWNLOADING": "📥",
            "FAILED": "🔴",
            "CANCELLED": "❌",
            "PREPARING": "🟠",
            "DELIVERING": "📤",
        }
        for j in jobs:
            icon = icons.get(j.status.value, "⚪")
            kw = ", ".join(j.keywords[:3])
            if len(j.keywords) > 3:
                kw += "…"
            lines.append(f"{icon} <code>{j.job_id}</code> — {escape_html(kw)}")
            lines.append(f"   {j.status.value}")
        text = "\n".join(lines)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_home())  # type: ignore
    await callback.answer()


# ─── Search flow ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "search:start")
async def cb_search_start(callback: CallbackQuery, db_user: User, state: FSMContext) -> None:
    # Check access
    pub = await storage_repo.get_public()
    if pub.status != "active" or not pub.chat_id:
        await callback.answer("Public storage not configured", show_alert=True)
        await callback.message.edit_text(  # type: ignore
            "🔴 <b>Storage not ready</b>\n\n"
            "Free users cannot search until Public Free Storage is configured by an admin.\n\n"
            "Contact @Arehsmanbot",
            parse_mode="HTML",
            reply_markup=back_home(),
        )
        return

    plans = await plans_repo.all()
    plan = plans.get(db_user.plan)
    limit = plan.daily_searches if plan else 3
    if limit >= 0 and db_user.daily_searches_used >= limit:
        await callback.answer("Daily limit reached", show_alert=True)
        await callback.message.edit_text(  # type: ignore
            f"⏳ <b>Daily limit reached</b>\n\n"
            f"You used {db_user.daily_searches_used} / {limit} searches today.\n"
            f"Limit resets daily.\n\n"
            f"💎 Upgrade for higher limits: contact @Arehsmanbot",
            parse_mode="HTML",
            reply_markup=back_home(),
        )
        return

    # Queue / concurrency soft check
    settings = get_settings()
    if job_manager.queue_size() >= settings.max_queue_size:
        await callback.answer("Queue is full, try later", show_alert=True)
        return

    active_user = sum(
        1 for j in job_manager.user_jobs(db_user.user_id)
        if j.status.value in ("QUEUED", "PREPARING", "DOWNLOADING", "SEARCHING", "GENERATING_RESULTS", "DELIVERING")
    )
    if active_user >= settings.max_active_jobs_per_user:
        await callback.answer("You have too many active jobs", show_alert=True)
        return

    await state.set_state(SearchStates.waiting_keywords)
    await state.update_data(case_sensitive=False)
    await callback.message.edit_text(  # type: ignore
        "🔎 <b>SEARCH</b>\n\n"
        "Send one or more keywords.\n"
        "• One keyword per line\n"
        "• Or a single keyword\n\n"
        "Example:\n"
        "<code>apple\ngoogle\nmicrosoft</code>",
        parse_mode="HTML",
        reply_markup=back_home(),
    )
    await callback.answer()


@router.message(SearchStates.waiting_keywords)
async def msg_keywords(message: Message, db_user: User, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Please send at least one keyword.")
        return
    keywords = [k.strip() for k in text.splitlines() if k.strip()]
    if not keywords:
        await message.answer("No valid keywords found.")
        return
    if len(keywords) > 20:
        await message.answer("Maximum 20 keywords per search.")
        return

    data = await state.get_data()
    case_sensitive = data.get("case_sensitive", False)
    await state.update_data(keywords=keywords)
    await state.set_state(SearchStates.confirm)

    pub = await storage_repo.get_public()
    storage_name = pub.display_name or "Public Free Storage"
    size_est = format_bytes(pub.total_size_bytes) if pub.total_size_bytes else "unknown"

    conf = (
        f"🔎 <b>SEARCH CONFIRMATION</b>\n\n"
        f"Storage: <b>{escape_html(storage_name)}</b>\n"
        f"Keywords: <b>{len(keywords)}</b>\n"
        f"  {escape_html(', '.join(keywords[:5]))}{'…' if len(keywords) > 5 else ''}\n"
        f"Case sensitive: <b>{'ON' if case_sensitive else 'OFF'}</b>\n"
        f"Estimated data: {size_est}\n\n"
        f"Ready to search?"
    )
    await message.answer(conf, parse_mode="HTML", reply_markup=search_confirm(case_sensitive))


@router.callback_query(F.data == "search:toggle_case", SearchStates.confirm)
async def cb_toggle_case(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cs = not data.get("case_sensitive", False)
    await state.update_data(case_sensitive=cs)
    keywords = data.get("keywords", [])
    pub = await storage_repo.get_public()
    storage_name = pub.display_name or "Public Free Storage"
    size_est = format_bytes(pub.total_size_bytes) if pub.total_size_bytes else "unknown"
    conf = (
        f"🔎 <b>SEARCH CONFIRMATION</b>\n\n"
        f"Storage: <b>{escape_html(storage_name)}</b>\n"
        f"Keywords: <b>{len(keywords)}</b>\n"
        f"Case sensitive: <b>{'ON' if cs else 'OFF'}</b>\n"
        f"Estimated data: {size_est}\n\n"
        f"Ready to search?"
    )
    await callback.message.edit_text(conf, parse_mode="HTML", reply_markup=search_confirm(cs))  # type: ignore
    await callback.answer(f"Case-sensitive: {'ON' if cs else 'OFF'}")


@router.callback_query(F.data == "search:cancel")
async def cb_search_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Search cancelled.", reply_markup=main_menu())  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "search:confirm", SearchStates.confirm)
async def cb_search_confirm(callback: CallbackQuery, db_user: User, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    keywords = data.get("keywords", [])
    case_sensitive = data.get("case_sensitive", False)
    await state.clear()

    if not keywords:
        await callback.answer("No keywords", show_alert=True)
        return

    # Re-check limit
    plans = await plans_repo.all()
    plan = plans.get(db_user.plan)
    limit = plan.daily_searches if plan else 3
    if limit >= 0 and db_user.daily_searches_used >= limit:
        await callback.answer("Daily limit reached", show_alert=True)
        return

    # Public storage for everyone; private only for paid / owner with a linked chat
    storage_ids = ["public"]
    if db_user.plan != "FREE" and db_user.private_storage_chat_id:
        storage_ids.append(f"user_{db_user.user_id}")

    # Progress message
    progress_msg = await callback.message.edit_text(  # type: ignore
        "⏳ <b>SEARCH QUEUED</b>\n\nYour search has been added to the queue.",
        parse_mode="HTML",
    )

    job = await job_manager.create_job(
        user_id=db_user.user_id,
        keywords=keywords,
        case_sensitive=case_sensitive,
        storage_ids=storage_ids,
        progress_chat_id=callback.message.chat.id,  # type: ignore
        progress_message_id=progress_msg.message_id,  # type: ignore
    )

    qpos = job_manager.queue_size()
    await bot.edit_message_text(
        f"⏳ <b>SEARCH QUEUED</b>\n\n"
        f"<code>{job.job_id}</code>\n"
        f"Position: ~{qpos}\n\n"
        f"You will see live progress here.",
        chat_id=callback.message.chat.id,  # type: ignore
        message_id=progress_msg.message_id,  # type: ignore
        parse_mode="HTML",
        reply_markup=search_cancel_btn(job.job_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("job:cancel:"))
async def cb_job_cancel(callback: CallbackQuery, db_user: User) -> None:
    job_id = callback.data.split(":")[-1]  # type: ignore
    job = job_manager.get(job_id)
    if not job:
        await callback.answer("Job not found", show_alert=True)
        return
    if job.user_id != db_user.user_id and not is_admin(db_user.user_id):
        await callback.answer("Not your job", show_alert=True)
        return
    ok = await job_manager.cancel(job_id)
    if ok:
        await callback.answer("Cancellation requested")
    else:
        await callback.answer("Cannot cancel this job", show_alert=True)


# ─── Command aliases (same actions as main menu buttons) ─────────────────────

@router.message(Command("search"))
async def cmd_search(message: Message, db_user: User, state: FSMContext) -> None:
    """Mirror of 🔎 SEARCH button."""
    await state.clear()
    # Reuse the same gate checks as the callback by simulating start flow text
    pub = await storage_repo.get_public()
    if pub.status != "active" or not pub.chat_id:
        await message.answer(
            "🔴 <b>Storage not ready</b>\n\n"
            "Public Free Storage is not configured yet.\nContact @Arehsmanbot",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return
    plans = await plans_repo.all()
    plan = plans.get(db_user.plan)
    limit = plan.daily_searches if plan else 3
    if limit >= 0 and db_user.daily_searches_used >= limit:
        await message.answer(
            f"⏳ <b>Daily limit reached</b>\n\n"
            f"You used {db_user.daily_searches_used} / {limit} searches today.\n"
            f"💎 Upgrade: contact @Arehsmanbot",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
        return
    await state.set_state(SearchStates.waiting_keywords)
    await state.update_data(case_sensitive=False)
    await message.answer(
        "🔎 <b>SEARCH</b>\n\n"
        "Send one or more keywords.\n"
        "• One keyword per line\n"
        "• Or a single keyword\n\n"
        "Example:\n"
        "<code>apple\ngoogle\nmicrosoft</code>",
        parse_mode="HTML",
        reply_markup=back_home(),
    )


@router.message(Command("jobs"))
async def cmd_jobs(message: Message, db_user: User) -> None:
    jobs = job_manager.user_jobs(db_user.user_id, limit=15)
    if not jobs:
        text = "📊 <b>MY JOBS</b>\n\nNo jobs yet. Start a search!"
    else:
        lines = ["📊 <b>MY JOBS</b>\n"]
        icons = {
            "COMPLETED": "🟢",
            "SEARCHING": "🟠",
            "QUEUED": "⏳",
            "DOWNLOADING": "📥",
            "FAILED": "🔴",
            "CANCELLED": "❌",
            "PREPARING": "🟠",
            "DELIVERING": "📤",
        }
        for j in jobs:
            icon = icons.get(j.status.value, "⚪")
            kw = ", ".join(j.keywords[:3])
            if len(j.keywords) > 3:
                kw += "…"
            lines.append(f"{icon} <code>{j.job_id}</code> — {escape_html(kw)}")
            lines.append(f"   {j.status.value}")
        text = "\n".join(lines)
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())


@router.message(Command("storage"))
async def cmd_storage(message: Message, db_user: User) -> None:
    pub = await storage_repo.get_public()
    lines = ["📂 <b>MY STORAGE</b>\n"]
    if pub.status == "active":
        lines.append("🆓 <b>Public Free Storage</b>")
        lines.append(f"Name: {escape_html(pub.display_name or '—')}")
        lines.append(f"Files: {pub.file_count:,}")
        if pub.total_size_bytes:
            lines.append(f"Size: {format_bytes(pub.total_size_bytes)}")
    else:
        lines.append("🔴 Public Free Storage is not configured yet.")
    if db_user.plan != "FREE":
        lines.append("")
        if db_user.private_storage_chat_id:
            lines.append("💎 <b>Private Storage</b>")
            lines.append(f"Name: {escape_html(db_user.private_storage_name or '—')}")
        else:
            lines.append("💎 Private storage: available on your plan (contact admin to link)")
    else:
        lines.append("")
        lines.append("💎 Private storage: upgrade to unlock")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_menu())


@router.message(Command("account"))
async def cmd_account(message: Message, db_user: User) -> None:
    plans = await plans_repo.all()
    plan = plans.get(db_user.plan)
    limit = plan.daily_searches if plan else 3
    limit_str = "∞" if limit < 0 else str(limit)
    storage_label = await _get_storage_label(db_user)
    if db_user.plan in ("FREE", "OWNER") or not db_user.plan_expiry:
        expiry_line = "Never"
    else:
        expiry_line = escape_html(str(db_user.plan_expiry)[:19])
    role = "👑 Owner (admin)" if db_user.plan == "OWNER" or is_admin(db_user.user_id) else escape_html(db_user.plan)
    text = (
        f"👤 <b>MY ACCOUNT</b>\n\n"
        f"Telegram ID: <code>{db_user.user_id}</code>\n"
        f"Plan: <b>{role}</b>\n"
        f"Daily searches: <b>{db_user.daily_searches_used}</b> / {limit_str}\n"
        f"Expiry: {expiry_line}\n"
        f"Storage: {escape_html(storage_label)}"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())


@router.message(Command("upgrade"))
async def cmd_upgrade(message: Message) -> None:
    plans = await plans_repo.all()
    lines = ["💎 <b>CHOOSE YOUR ACCESS</b>\n"]
    order = ["FREE", "STARTER", "STANDARD", "PRO", "UNLIMITED"]
    for pid in order:
        p = plans.get(pid)
        if not p or not p.enabled:
            continue
        star = " ⭐" if p.is_popular else ""
        lines.append(f"{p.display_name}{star}")
        lines.append(f"{p.description}")
        lines.append("")
    lines.append("Contact @Arehsmanbot to activate a paid plan.")
    lines.append("Private storage is available on paid plans only.")
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=plans_keyboard(plans),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db_user: User, state: FSMContext) -> None:
    await state.clear()
    parts = (message.text or "").split()
    # Admin cancelling by JOB_ID → handled below without blocking
    if len(parts) >= 2 and is_admin(message.from_user.id):
        job_id = parts[1].upper()
        if not job_id.startswith("JOB-"):
            job_id = f"JOB-{job_id}"
        ok = await job_manager.cancel(job_id, by_admin=True)
        await message.answer(
            f"✅ Cancel requested for <code>{job_id}</code>" if ok else "❌ Cannot cancel / not found",
            parse_mode="HTML",
        )
        return

    # User: cancel own most recent active job
    jobs = job_manager.user_jobs(db_user.user_id, limit=10)
    active = [
        j for j in jobs
        if j.status.value in (
            "QUEUED", "PREPARING", "DOWNLOADING", "SEARCHING",
            "GENERATING_RESULTS", "DELIVERING",
        )
    ]
    if not active:
        await message.answer("No active search to cancel.", reply_markup=main_menu())
        return
    job = active[0]
    ok = await job_manager.cancel(job.job_id)
    if ok:
        await message.answer(
            f"❌ Cancellation requested for <code>{job.job_id}</code>",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )
    else:
        await message.answer("Could not cancel that job.", reply_markup=main_menu())


@router.message(Command("settings"))
async def cmd_settings(message: Message, db_user: User) -> None:
    # Admins get system settings summary
    if is_admin(message.from_user.id):
        rt = await settings_repo.get()
        s = get_settings()
        text = (
            "⚙️ <b>SYSTEM SETTINGS</b>\n\n"
            f"Free daily searches: {rt.free_daily_searches}\n"
            f"Max concurrent searches: {rt.max_concurrent_searches}\n"
            f"Result retention: {rt.result_retention_seconds}s\n"
            f"Timezone: {rt.timezone}\n"
            f"Broadcast rate: {rt.broadcast_rate}/s\n"
            f"Maintenance: {'ON' if rt.maintenance_mode else 'OFF'}\n"
            f"Local Bot API: {'ON' if s.use_local_bot_api else 'OFF'}\n"
            f"Admin IDs: <code>{', '.join(str(i) for i in s.admin_ids)}</code>\n\n"
            "Use /admin for the full control panel."
        )
        await message.answer(text, parse_mode="HTML", reply_markup=main_menu())
        return

    text = (
        "⚙️ <b>SETTINGS</b>\n\n"
        "Search options (case-sensitive, etc.) are chosen when you start a search.\n\n"
        f"Plan: <b>{escape_html(db_user.plan)}</b>\n"
        f"Daily searches used: <b>{db_user.daily_searches_used}</b>\n\n"
        "Need more searches or private storage?\n"
        "Use /upgrade or contact @Arehsmanbot."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())
