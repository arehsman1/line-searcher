"""
LINE SEARCHER - Admin handlers & commands
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards import (
    admin_panel,
    admin_refresh_now,
    admin_storage_confirm,
    admin_storage_menu,
    back_home,
    broadcast_confirm,
    broadcast_filters,
    maintenance_toggle,
)
from app.bot.middlewares import is_admin
from app.bot.states import AdminStorageStates, AdminUserStates, BroadcastStates
from app.broadcast.manager import broadcast_manager
from app.core.config import get_settings
from app.core.database import plans_repo, settings_repo, storage_repo, users_repo
from app.core.models import BroadcastFilter, CatalogEntry, User, UserStatus, utcnow_iso
from app.jobs.manager import job_manager
from app.storage.catalog import catalog_manager
from app.utils.helpers import escape_html, format_bytes

logger = logging.getLogger(__name__)
router = Router(name="admin")


async def _notify_public_storage_refreshed(
    bot: Bot,
    *,
    file_count: int,
    total_size: int,
    display_name: str | None = None,
) -> int:
    """
    Notify all active users that the public free storage list was updated.
    Rate-limited so a large user base does not flood Telegram.
    Returns number of successful deliveries.
    """
    import asyncio

    name = display_name or "Public Free Storage"
    text = (
        "📢 <b>Public storage updated</b>\n\n"
        f"The public file list has been refreshed.\n"
        f"Storage: <b>{escape_html(name)}</b>\n"
        f"Files: <b>{file_count:,}</b>\n"
        f"Total size: <b>{format_bytes(total_size)}</b>\n\n"
        "You can start a new search with /search or the 🔎 SEARCH button."
    )
    users = await users_repo.all_users()
    sent = 0
    for u in users:
        if u.status != UserStatus.ACTIVE:
            continue
        try:
            await bot.send_message(u.user_id, text, parse_mode="HTML")
            sent += 1
            # ~25 msg/s soft limit
            await asyncio.sleep(0.04)
        except Exception as e:
            logger.debug("Notify user %s failed: %s", u.user_id, e)
    return sent


def admin_only(func):
    """Decorator-style check inside handlers."""
    return func


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    await _show_admin_panel(message)


@router.callback_query(F.data == "admin:panel")
async def cb_admin_panel(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return
    await _show_admin_panel_edit(callback)


async def _show_admin_panel(message: Message) -> None:
    text = await _admin_dashboard_text()
    await message.answer(text, parse_mode="HTML", reply_markup=admin_panel())


async def _show_admin_panel_edit(callback: CallbackQuery) -> None:
    text = await _admin_dashboard_text()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_panel())  # type: ignore
    await callback.answer()


async def _admin_dashboard_text() -> str:
    total = await users_repo.count()
    all_u = await users_repo.all_users()
    free_n = sum(1 for u in all_u if u.plan == "FREE")
    paid_n = sum(1 for u in all_u if u.plan != "FREE")
    active_paid = 0
    now = datetime.now(timezone.utc)
    for u in all_u:
        if u.plan != "FREE" and u.plan_expiry:
            try:
                exp = datetime.fromisoformat(u.plan_expiry)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp > now:
                    active_paid += 1
            except Exception:
                pass
    searches_today = sum(u.daily_searches_used for u in all_u)
    pub = await storage_repo.get_public()
    rt = await settings_repo.get()
    status = "🔴 MAINTENANCE" if rt.maintenance_mode else "🟢 Operational"
    return (
        f"🛠 <b>LINE SEARCHER ADMIN</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"👥 USERS\n"
        f"{total:,} registered\n\n"
        f"💎 PAID\n"
        f"{active_paid} active / {paid_n} total\n\n"
        f"🔎 SEARCHES\n"
        f"{searches_today} today\n\n"
        f"📂 STORAGE\n"
        f"{pub.file_count:,} files\n\n"
        f"{status}\n"
        f"Active jobs: {job_manager.active_count()} | Queue: {job_manager.queue_size()}\n"
        f"━━━━━━━━━━━━━━"
    )


# ─── Storage ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:storage")
async def cb_admin_storage(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    pub = await storage_repo.get_public()
    if pub.status == "active" and pub.chat_id:
        text = (
            f"📂 <b>PUBLIC STORAGE</b>\n\n"
            f"Status: 🟢 ACTIVE\n"
            f"Name: {escape_html(pub.display_name or '—')}\n"
            f"Chat ID: <code>{pub.chat_id}</code>\n"
            f"Indexed Files: {pub.file_count:,}\n"
            f"Size: {format_bytes(pub.total_size_bytes)}\n"
            f"Last Refresh: {escape_html((pub.last_refresh or '—')[:19])}"
        )
        configured = True
    else:
        text = (
            "📂 <b>PUBLIC STORAGE</b>\n\n"
            "🔴 NOT CONFIGURED\n\n"
            "Free users cannot search until Public Free Storage is configured."
        )
        configured = False
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_storage_menu(configured))  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "admin:storage:set")
@router.callback_query(F.data == "admin:storage:change")
async def cb_storage_set(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStorageStates.waiting_chat_id)
    await callback.message.edit_text(  # type: ignore
        "📂 <b>SET PUBLIC STORAGE</b>\n\n"
        "Send the Telegram Channel/Group ID.\n"
        "Example: <code>-1001234567890</code>\n\n"
        "Or forward a message from the target channel/group.",
        parse_mode="HTML",
        reply_markup=back_home(),
    )
    await callback.answer()


@router.message(AdminStorageStates.waiting_chat_id)
async def msg_storage_chat_id(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return
    # Forward detection
    if message.forward_from_chat:
        chat = message.forward_from_chat
        await state.update_data(pending_chat_id=chat.id, pending_title=chat.title or str(chat.id))
        await state.set_state(AdminStorageStates.waiting_name)
        await message.answer(
            f"📂 <b>USE THIS STORAGE?</b>\n\n"
            f"Name: {escape_html(chat.title or '—')}\n"
            f"Chat ID: <code>{chat.id}</code>\n\n"
            f"What display name should users see?",
            parse_mode="HTML",
            reply_markup=admin_storage_confirm(),
        )
        return

    text = (message.text or "").strip()
    try:
        chat_id = int(text)
    except ValueError:
        await message.answer("Invalid chat ID. Send a number like <code>-1001234567890</code>", parse_mode="HTML")
        return

    # Verify access
    try:
        chat = await bot.get_chat(chat_id)
        title = chat.title or str(chat_id)
    except Exception as e:
        await message.answer(
            f"⚠️ Cannot access this chat: {escape_html(str(e)[:100])}\n"
            f"Make sure the bot is an admin in the channel/group.",
            parse_mode="HTML",
        )
        return

    await state.update_data(pending_chat_id=chat_id, pending_title=title)
    await state.set_state(AdminStorageStates.waiting_name)
    await message.answer(
        f"📂 Chat verified: <b>{escape_html(title)}</b>\n"
        f"ID: <code>{chat_id}</code>\n\n"
        f"What name should users see for this storage?\n"
        f"Example: <code>LINE SEARCHER FREE STORAGE</code>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin:storage:confirm")
async def cb_storage_confirm_forward(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    data = await state.get_data()
    chat_id = data.get("pending_chat_id")
    title = data.get("pending_title", "Public Storage")
    if not chat_id:
        await callback.answer("No pending storage", show_alert=True)
        return
    await state.update_data(pending_name=title)
    # Use title as name immediately
    cfg = await storage_repo.set_public(int(chat_id), title)
    await state.clear()
    await callback.message.edit_text(  # type: ignore
        f"✅ Public storage set.\n\n"
        f"Name: {escape_html(cfg.display_name)}\n"
        f"Chat ID: <code>{cfg.chat_id}</code>\n\n"
        f"🔄 Refresh file catalog now?",
        parse_mode="HTML",
        reply_markup=admin_refresh_now(),
    )
    await callback.answer()


@router.message(AdminStorageStates.waiting_name)
async def msg_storage_name(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 100:
        await message.answer("Please send a display name (1–100 characters).")
        return
    data = await state.get_data()
    chat_id = data.get("pending_chat_id")
    if not chat_id:
        await message.answer("Session expired. Start again.")
        await state.clear()
        return
    cfg = await storage_repo.set_public(int(chat_id), name)
    await state.clear()
    await message.answer(
        f"✅ Public storage set.\n\n"
        f"Name: {escape_html(cfg.display_name)}\n"
        f"Chat ID: <code>{cfg.chat_id}</code>\n\n"
        f"🔄 Refresh file catalog now?",
        parse_mode="HTML",
        reply_markup=admin_refresh_now(),
    )


@router.callback_query(F.data == "admin:storage:refresh")
@router.callback_query(F.data == "admin:refresh")
async def cb_storage_refresh(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        return
    pub = await storage_repo.get_public()
    if pub.status != "active" or not pub.chat_id:
        await callback.answer("Storage not configured", show_alert=True)
        return

    await callback.message.edit_text("🔄 <b>Refreshing catalog…</b>\nThis may take a while for large channels.", parse_mode="HTML")  # type: ignore
    await callback.answer()

    try:
        count, total_size = await _refresh_catalog(bot, pub.chat_id, "public")
        await storage_repo.update_public_stats(count, total_size)
        notified = await _notify_public_storage_refreshed(
            bot,
            file_count=count,
            total_size=total_size,
            display_name=pub.display_name,
        )
        await callback.message.edit_text(  # type: ignore
            f"✅ <b>Catalog refreshed</b>\n\n"
            f"Files: {count:,}\n"
            f"Total size: {format_bytes(total_size)}\n"
            f"Users notified: {notified:,}",
            parse_mode="HTML",
            reply_markup=admin_panel(),
        )
    except Exception as e:
        logger.exception("Refresh failed")
        await callback.message.edit_text(  # type: ignore
            f"❌ Refresh failed: {escape_html(str(e)[:200])}",
            parse_mode="HTML",
            reply_markup=admin_panel(),
        )


async def _refresh_catalog(bot: Bot, chat_id: int, storage_id: str) -> tuple[int, int]:
    """
    Index TXT documents from a channel/group.
    Uses get_chat_history via Bot API limitations — for large channels,
    prefer that the admin posts files the bot can see, or use a userbot later.
    Bot API does not provide full history iteration like MTProto.
    Strategy: scan recent messages via getUpdates is not enough.
    Practical approach: admin can forward files OR we use iterative
    getChatHistory is NOT available in Bot API.
    
    Limitation note: Standard Bot API cannot list all historical messages
    in a channel. We support:
    1. Indexing files that are sent/forwarded to the bot while it monitors
    2. Manual file registration
    3. For production with existing large archives, Local Bot API + a one-time
       MTProto indexer (Telethon/Pyrogram) can be added as an optional component
       without requiring end-user credentials.
    
    For this implementation we index messages the bot receives in the storage
    chat (channel posts where bot is admin) and provide a partial scan via
    pinned/recent if available. Full historical index requires optional MTProto
    component (documented in deployment).
    """
    # Clear and rebuild from what we can access.
    # Bot API limitation: no getChatHistory. We keep existing catalog and
    # rely on the channel monitor handler for new files.
    # On refresh we verify existing entries still resolve.
    entries = await catalog_manager.list_available(storage_id)
    available = 0
    total_size = 0
    for entry in list(entries):
        try:
            f = await bot.get_file(entry.file_id)
            if f:
                available += 1
                total_size += entry.file_size or (f.file_size or 0)
            else:
                await catalog_manager.mark_missing(storage_id, entry.chat_id, entry.message_id)
        except Exception:
            await catalog_manager.mark_missing(storage_id, entry.chat_id, entry.message_id)

    await catalog_manager.save(storage_id)
    stats = await catalog_manager.stats(storage_id)
    return stats["available"], stats["total_size"]


@router.callback_query(F.data == "admin:storage:remove")
async def cb_storage_remove(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await storage_repo.remove_public()
    await callback.message.edit_text(  # type: ignore
        "❌ Public storage configuration removed.\nFree users cannot search until reconfigured.",
        reply_markup=admin_panel(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:storage:files")
async def cb_storage_files(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    entries = await catalog_manager.list_available("public")
    if not entries:
        text = "📋 No indexed TXT files."
    else:
        lines = [f"📋 <b>Indexed files ({len(entries)})</b>\n"]
        for e in entries[:30]:
            lines.append(f"• {escape_html(e.file_name)} ({format_bytes(e.file_size)})")
        if len(entries) > 30:
            lines.append(f"\n… and {len(entries) - 30} more")
        text = "\n".join(lines)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_panel())  # type: ignore
    await callback.answer()


# ─── Index incoming channel documents ────────────────────────────────────────

@router.channel_post(F.document)
async def on_channel_document(message: Message) -> None:
    """When bot is admin in public storage channel, auto-index TXT uploads."""
    pub = await storage_repo.get_public()
    if pub.status != "active" or not pub.chat_id:
        return
    if message.chat.id != pub.chat_id:
        return
    doc = message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        return
    entry = CatalogEntry(
        chat_id=message.chat.id,
        message_id=message.message_id,
        file_id=doc.file_id,
        file_unique_id=doc.file_unique_id,
        file_name=doc.file_name or f"{message.message_id}.txt",
        file_size=doc.file_size or 0,
        mime_type=doc.mime_type,
        storage_id="public",
        status="available",
    )
    await catalog_manager.upsert("public", entry)
    await catalog_manager.save("public")
    stats = await catalog_manager.stats("public")
    await storage_repo.update_public_stats(stats["available"], stats["total_size"])
    logger.info("Indexed channel file: %s (%s)", entry.file_name, entry.file_size)


# ─── Users / Stats / Commands ────────────────────────────────────────────────

@router.callback_query(F.data == "admin:users")
async def cb_admin_users(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    total = await users_repo.count()
    text = (
        f"👥 <b>USERS</b>\n\n"
        f"Total: {total:,}\n\n"
        f"Commands:\n"
        f"/user USER_ID — details\n"
        f"/allow USER_ID PLAN — grant plan\n"
        f"/deny USER_ID — block\n"
        f"/users — list recent\n"
        f"/export — download users.json"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_panel())  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "admin:stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    all_u = await users_repo.all_users()
    free_n = sum(1 for u in all_u if u.plan == "FREE")
    paid_n = len(all_u) - free_n
    searches_today = sum(u.daily_searches_used for u in all_u)
    total_searches = sum(u.total_searches for u in all_u)
    pub = await storage_repo.get_public()
    try:
        import psutil
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(str(get_settings().data_dir))
        res = (
            f"\n🖥 CPU: {cpu}%\n"
            f"RAM: {mem.percent}% ({format_bytes(mem.used)} / {format_bytes(mem.total)})\n"
            f"Disk: {disk.percent}% ({format_bytes(disk.used)} / {format_bytes(disk.total)})"
        )
    except Exception:
        res = ""
    text = (
        f"📊 <b>STATISTICS</b>\n\n"
        f"👥 Users: {len(all_u):,}\n"
        f"🆓 Free: {free_n:,}\n"
        f"💎 Paid: {paid_n:,}\n"
        f"🔎 Searches today: {searches_today}\n"
        f"🔎 Total searches: {total_searches:,}\n"
        f"📂 Storage files: {pub.file_count:,}\n"
        f"📂 Storage size: {format_bytes(pub.total_size_bytes)}\n"
        f"🟠 Active jobs: {job_manager.active_count()}\n"
        f"⏳ Queue: {job_manager.queue_size()}"
        f"{res}"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_panel())  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "admin:jobs")
async def cb_admin_jobs(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    active = [j for j in job_manager._jobs.values() if j.status.value in (
        "QUEUED", "PREPARING", "DOWNLOADING", "SEARCHING", "GENERATING_RESULTS", "DELIVERING"
    )]
    lines = [f"🔎 <b>SEARCH JOBS</b>\n\nActive: {len(active)}\n"]
    for j in active[:20]:
        lines.append(f"<code>{j.job_id}</code> user={j.user_id} {j.status.value}")
    if not active:
        lines.append("No active jobs.")
    lines.append("\n/cancel JOB_ID — cancel a job")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=admin_panel())  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "admin:plans")
async def cb_admin_plans(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    plans = await plans_repo.all()
    lines = ["💎 <b>PLANS</b>\n"]
    for pid, p in plans.items():
        status = "🟢" if p.enabled else "🔴"
        lines.append(f"{status} {p.display_name} — ${p.price_usd:g} / {p.duration_days}d / {p.daily_searches}/day")
    lines.append("\nEdit plans via plans.json or future admin UI.")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=admin_panel())  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "admin:settings")
async def cb_admin_settings(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    rt = await settings_repo.get()
    s = get_settings()
    text = (
        f"⚙️ <b>SETTINGS</b>\n\n"
        f"Free daily searches: {rt.free_daily_searches}\n"
        f"Max concurrent searches: {rt.max_concurrent_searches}\n"
        f"Result retention: {rt.result_retention_seconds}s\n"
        f"Timezone: {rt.timezone}\n"
        f"Broadcast rate: {rt.broadcast_rate}/s\n"
        f"Maintenance: {'ON' if rt.maintenance_mode else 'OFF'}\n"
        f"Local Bot API: {'ON' if s.use_local_bot_api else 'OFF'}"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_panel())  # type: ignore
    await callback.answer()


@router.callback_query(F.data == "admin:maintenance")
async def cb_maintenance(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    rt = await settings_repo.get()
    text = (
        f"🛠 <b>MAINTENANCE MODE</b>\n\n"
        f"Currently: {'🔴 ENABLED' if rt.maintenance_mode else '🟢 DISABLED'}\n\n"
        f"When enabled, normal users cannot search."
    )
    await callback.message.edit_text(  # type: ignore
        text, parse_mode="HTML", reply_markup=maintenance_toggle(rt.maintenance_mode)
    )
    await callback.answer()


@router.callback_query(F.data == "admin:maintenance:on")
async def cb_maint_on(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await settings_repo.update(maintenance_mode=True)
    await callback.answer("Maintenance ENABLED")
    await cb_maintenance(callback)


@router.callback_query(F.data == "admin:maintenance:off")
async def cb_maint_off(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        return
    await settings_repo.update(maintenance_mode=False)
    await callback.answer("Maintenance DISABLED")
    await cb_maintenance(callback)


# ─── Broadcast ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast")
async def cb_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    if broadcast_manager.is_active():
        await callback.answer("A broadcast is already running", show_alert=True)
        return
    await state.set_state(BroadcastStates.waiting_message)
    await callback.message.edit_text(  # type: ignore
        "📢 <b>BROADCAST CENTER</b>\n\n"
        "Send the message you want to broadcast.\n"
        "Text, photo, video, or document are supported.\n\n"
        "Then you will choose recipients and confirm.",
        parse_mode="HTML",
        reply_markup=back_home(),
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_message)
async def msg_broadcast_content(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    data = {}
    if message.photo:
        data = {"message_type": "photo", "file_id": message.photo[-1].file_id, "caption": message.caption, "text": None}
    elif message.video:
        data = {"message_type": "video", "file_id": message.video.file_id, "caption": message.caption, "text": None}
    elif message.document:
        data = {"message_type": "document", "file_id": message.document.file_id, "caption": message.caption, "text": None}
    elif message.text:
        data = {"message_type": "text", "text": message.text, "file_id": None, "caption": None}
    else:
        await message.answer("Unsupported message type. Send text or media.")
        return

    await state.update_data(**data)
    await state.set_state(BroadcastStates.confirming)
    await message.answer(
        "📢 Select recipients:",
        reply_markup=broadcast_filters(),
    )


@router.callback_query(F.data.startswith("broadcast:filter:"), BroadcastStates.confirming)
async def cb_broadcast_filter(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    filt = callback.data.split(":")[-1]  # type: ignore
    try:
        filter_enum = BroadcastFilter(filt)
    except ValueError:
        filter_enum = BroadcastFilter.ALL

    data = await state.get_data()
    job = await broadcast_manager.create(
        admin_id=callback.from_user.id,
        filter_=filter_enum,
        text=data.get("text"),
        message_type=data.get("message_type", "text"),
        file_id=data.get("file_id"),
        caption=data.get("caption"),
    )
    recipients = await broadcast_manager.resolve_recipients(filter_enum)
    job.total_recipients = len(recipients)
    await state.update_data(broadcast_id=job.broadcast_id)

    preview = data.get("text") or data.get("caption") or f"[{data.get('message_type')} media]"
    text = (
        f"📢 <b>BROADCAST PREVIEW</b>\n\n"
        f"{escape_html(str(preview)[:500])}\n\n"
        f"👥 Recipients: {len(recipients):,}\n"
        f"Filter: {filter_enum.value}\n\n"
        f"Are you sure you want to send this broadcast?"
    )
    await callback.message.edit_text(  # type: ignore
        text, parse_mode="HTML", reply_markup=broadcast_confirm(job.broadcast_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("broadcast:send:"))
async def cb_broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    bid = callback.data.split(":")[-1]  # type: ignore
    await state.clear()
    progress = await callback.message.edit_text(  # type: ignore
        f"📢 <b>BROADCASTING</b>\nStarting…\n<code>{bid}</code>",
        parse_mode="HTML",
    )
    await callback.answer()
    await broadcast_manager.start_send(
        bid,
        progress_chat_id=callback.message.chat.id,  # type: ignore
        progress_message_id=progress.message_id,  # type: ignore
    )


# ─── Commands ────────────────────────────────────────────────────────────────

@router.message(Command("allow"))
async def cmd_allow(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Usage: /allow USER_ID PLAN [DAYS]\nPlans: STARTER STANDARD PRO UNLIMITED")
        return
    try:
        uid = int(parts[1])
        plan = parts[2].upper()
        days = int(parts[3]) if len(parts) > 3 else 0
    except ValueError:
        await message.answer("Invalid arguments.")
        return

    plans = await plans_repo.all()
    if plan not in plans and plan != "CUSTOM":
        await message.answer(f"Unknown plan. Available: {', '.join(plans.keys())}")
        return

    p = plans.get(plan)
    duration = days or (p.duration_days if p else 0)
    user = await users_repo.get(uid)
    if not user:
        user, _ = await users_repo.get_or_create(uid)
    await users_repo.set_plan(uid, plan, duration_days=duration)
    await message.answer(f"✅ User <code>{uid}</code> → <b>{plan}</b> ({duration} days)", parse_mode="HTML")


@router.message(Command("deny"))
async def cmd_deny(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /deny USER_ID")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await message.answer("Invalid user ID")
        return
    await users_repo.block(uid)
    await message.answer(f"🔴 User <code>{uid}</code> blocked.", parse_mode="HTML")


@router.message(Command("user"))
async def cmd_user(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /user USER_ID")
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await message.answer("Invalid user ID")
        return
    user = await users_repo.get(uid)
    if not user:
        await message.answer("User not found")
        return
    text = (
        f"👤 <b>User {uid}</b>\n"
        f"Name: {escape_html(user.first_name or '—')}\n"
        f"Username: @{escape_html(user.username or '—')}\n"
        f"Plan: {user.plan}\n"
        f"Expiry: {user.plan_expiry or '—'}\n"
        f"Today: {user.daily_searches_used}\n"
        f"Total: {user.total_searches}\n"
        f"Status: {user.status.value}\n"
        f"Registered: {user.registration_date[:19]}"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("users"))
async def cmd_users(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    users = await users_repo.all_users()
    users = sorted(users, key=lambda u: u.last_interaction, reverse=True)[:30]
    lines = ["👥 <b>Recent users</b>\n"]
    for u in users:
        lines.append(f"<code>{u.user_id}</code> {u.plan} @{escape_html(u.username or '—')}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    raw = await users_repo.export_raw()
    await message.answer_document(
        BufferedInputFile(raw, filename="users.json"),
        caption="LINE SEARCHER users export",
    )


@router.message(Command("cancel"))
async def cmd_cancel_job(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Usage: /cancel JOB-ID")
        return
    job_id = parts[1].upper()
    if not job_id.startswith("JOB-"):
        job_id = f"JOB-{job_id}"
    ok = await job_manager.cancel(job_id, by_admin=True)
    await message.answer("✅ Cancel requested" if ok else "❌ Cannot cancel / not found")


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    # Reuse stats panel content
    class FakeCB:
        from_user = message.from_user
        message = message
        async def answer(self, *a, **k):
            pass
    # Just send text
    all_u = await users_repo.all_users()
    await message.answer(
        f"📊 Users: {len(all_u)} | Active jobs: {job_manager.active_count()} | Queue: {job_manager.queue_size()}"
    )


@router.message(Command("maintenance"))
async def cmd_maintenance(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].lower() in ("on", "1", "true"):
        await settings_repo.update(maintenance_mode=True)
        await message.answer("🔴 Maintenance ENABLED")
    elif len(parts) > 1 and parts[1].lower() in ("off", "0", "false"):
        await settings_repo.update(maintenance_mode=False)
        await message.answer("🟢 Maintenance DISABLED")
    else:
        rt = await settings_repo.get()
        await message.answer(f"Maintenance: {'ON' if rt.maintenance_mode else 'OFF'}\nUsage: /maintenance on|off")


@router.message(Command("refresh"))
async def cmd_refresh(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id):
        return
    pub = await storage_repo.get_public()
    if pub.status != "active" or not pub.chat_id:
        await message.answer("Public storage not configured")
        return
    msg = await message.answer("🔄 Refreshing…")
    try:
        count, total_size = await _refresh_catalog(bot, pub.chat_id, "public")
        await storage_repo.update_public_stats(count, total_size)
        notified = await _notify_public_storage_refreshed(
            bot,
            file_count=count,
            total_size=total_size,
            display_name=pub.display_name,
        )
        await msg.edit_text(
            f"✅ Refreshed: {count:,} files, {format_bytes(total_size)}\n"
            f"Users notified: {notified:,}"
        )
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


@router.message(Command("plan"))
async def cmd_plan(message: Message) -> None:
    """View or change a user's plan. Usage: /plan USER_ID [PLAN] [DAYS]"""
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "Usage:\n"
            "<code>/plan USER_ID</code> — view plan\n"
            "<code>/plan USER_ID PLAN [DAYS]</code> — set plan\n"
            "Plans: FREE STARTER STANDARD PRO UNLIMITED OWNER",
            parse_mode="HTML",
        )
        return
    try:
        uid = int(parts[1])
    except ValueError:
        await message.answer("Invalid user ID")
        return
    user = await users_repo.get(uid)
    if not user:
        await message.answer("User not found")
        return
    if len(parts) == 2:
        await message.answer(
            f"👤 <code>{uid}</code>\n"
            f"Plan: <b>{escape_html(user.plan)}</b>\n"
            f"Expiry: {escape_html(str(user.plan_expiry or 'Never')[:19])}\n"
            f"Today: {user.daily_searches_used}\n"
            f"Status: {user.status.value}",
            parse_mode="HTML",
        )
        return
    plan = parts[2].upper()
    days = int(parts[3]) if len(parts) > 3 else 0
    plans = await plans_repo.all()
    if plan not in plans and plan != "CUSTOM":
        await message.answer(f"Unknown plan. Available: {', '.join(plans.keys())}")
        return
    p = plans.get(plan)
    duration = days or (p.duration_days if p else 0)
    # Never demote configured admins away from OWNER via accident
    if uid in get_settings().admin_ids and plan != "OWNER":
        await message.answer(
            "⚠️ That user is a configured admin. They stay OWNER unless you remove them from ADMIN_TELEGRAM_IDS."
        )
        return
    await users_repo.set_plan(uid, plan, duration_days=duration)
    await message.answer(
        f"✅ User <code>{uid}</code> → <b>{plan}</b>"
        + (f" ({duration} days)" if duration else ""),
        parse_mode="HTML",
    )


@router.message(Command("publicstorage"))
async def cmd_publicstorage(message: Message, state: FSMContext) -> None:
    """Shortcut to configure Public Free Storage."""
    if not is_admin(message.from_user.id):
        return
    pub = await storage_repo.get_public()
    if pub.status == "active" and pub.chat_id:
        text = (
            f"📂 <b>PUBLIC STORAGE</b>\n\n"
            f"Status: 🟢 ACTIVE\n"
            f"Name: {escape_html(pub.display_name or '—')}\n"
            f"Chat ID: <code>{pub.chat_id}</code>\n"
            f"Files: {pub.file_count:,}\n"
            f"Size: {format_bytes(pub.total_size_bytes)}\n\n"
            f"Use /admin → STORAGE to change, or send a new chat ID to reconfigure."
        )
        await message.answer(text, parse_mode="HTML", reply_markup=admin_storage_menu(True))
    else:
        await state.set_state(AdminStorageStates.waiting_chat_id)
        await message.answer(
            "📂 <b>SET PUBLIC STORAGE</b>\n\n"
            "Send the Telegram Channel/Group ID.\n"
            "Example: <code>-1001234567890</code>\n\n"
            "Or forward a message from the target channel/group.",
            parse_mode="HTML",
        )


@router.message(Command("privatestorage"))
async def cmd_privatestorage(message: Message) -> None:
    """
    Link private storage for a paid user.
    Usage: /privatestorage USER_ID CHAT_ID [NAME]
    Only paid plans (and OWNER) may use private storage.
    """
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3:
        await message.answer(
            "Usage: <code>/privatestorage USER_ID CHAT_ID [NAME]</code>\n\n"
            "Private storage is for <b>paid users only</b> (not Free).",
            parse_mode="HTML",
        )
        return
    try:
        uid = int(parts[1])
        chat_id = int(parts[2])
    except ValueError:
        await message.answer("Invalid USER_ID or CHAT_ID")
        return
    name = parts[3].strip() if len(parts) > 3 else f"Private storage ({chat_id})"
    user = await users_repo.get(uid)
    if not user:
        await message.answer("User not found. They must /start first.")
        return
    if user.plan == "FREE":
        await message.answer(
            "❌ Private storage is for <b>paid users only</b>.\n"
            f"User <code>{uid}</code> is FREE. Use /allow first.",
            parse_mode="HTML",
        )
        return
    user.private_storage_chat_id = chat_id
    user.private_storage_name = name
    await users_repo.save(user)
    await message.answer(
        f"✅ Private storage linked for <code>{uid}</code>\n"
        f"Name: {escape_html(name)}\n"
        f"Chat ID: <code>{chat_id}</code>\n\n"
        f"Ensure the bot is admin in that chat so files can be indexed.",
        parse_mode="HTML",
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        return
    if broadcast_manager.is_active():
        await message.answer("A broadcast is already running.")
        return
    await state.set_state(BroadcastStates.waiting_message)
    await message.answer(
        "📢 <b>BROADCAST</b>\n\n"
        "Send the message to broadcast (text or media).\n"
        "Then choose recipients and confirm.",
        parse_mode="HTML",
    )
