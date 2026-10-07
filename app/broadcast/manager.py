"""
LINE SEARCHER - Broadcast Worker

Separate from search workers. Rate-limited, cancellable, with preview confirmation.
Does not block the search engine.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest

from app.core.config import get_settings
from app.core.database import settings_repo, users_repo
from app.core.models import (
    BroadcastFilter,
    BroadcastJob,
    BroadcastStatus,
    UserStatus,
    utcnow_iso,
)
from app.utils.helpers import RateLimiter, broadcast_id_from_seq, progress_bar

logger = logging.getLogger(__name__)


class BroadcastManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, BroadcastJob] = {}
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._seq = 0
        self._bot: Optional[Bot] = None
        self._active_broadcast: Optional[str] = None
        self._lock = asyncio.Lock()

    def set_bot(self, bot: Bot) -> None:
        self._bot = bot

    async def _next_id(self) -> str:
        self._seq += 1
        return broadcast_id_from_seq(self._seq)

    def get(self, bid: str) -> Optional[BroadcastJob]:
        return self._jobs.get(bid)

    def is_active(self) -> bool:
        return self._active_broadcast is not None

    async def create(
        self,
        admin_id: int,
        filter_: BroadcastFilter,
        text: Optional[str] = None,
        message_type: str = "text",
        file_id: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> BroadcastJob:
        bid = await self._next_id()
        job = BroadcastJob(
            broadcast_id=bid,
            admin_id=admin_id,
            filter=filter_,
            status=BroadcastStatus.CREATED,
            text=text,
            message_type=message_type,
            file_id=file_id,
            caption=caption,
        )
        self._jobs[bid] = job
        self._cancel_events[bid] = asyncio.Event()
        return job

    async def resolve_recipients(self, filter_: BroadcastFilter) -> List[int]:
        users = await users_repo.all_users()
        rt = await settings_repo.get()
        now = datetime.now(timezone.utc)
        expiring_hours = rt.expiring_soon_hours
        result = []

        for u in users:
            if u.status != UserStatus.ACTIVE:
                continue
            if filter_ == BroadcastFilter.ALL:
                result.append(u.user_id)
            elif filter_ == BroadcastFilter.FREE:
                if u.plan == "FREE":
                    result.append(u.user_id)
            elif filter_ == BroadcastFilter.PAID:
                if u.plan != "FREE":
                    result.append(u.user_id)
            elif filter_ == BroadcastFilter.ACTIVE:
                if u.plan != "FREE" and u.plan_expiry:
                    try:
                        exp = datetime.fromisoformat(u.plan_expiry)
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=timezone.utc)
                        if exp > now:
                            result.append(u.user_id)
                    except Exception:
                        pass
                elif u.plan == "FREE":
                    result.append(u.user_id)
            elif filter_ == BroadcastFilter.EXPIRING_SOON:
                if u.plan != "FREE" and u.plan_expiry:
                    try:
                        exp = datetime.fromisoformat(u.plan_expiry)
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=timezone.utc)
                        delta = exp - now
                        if timedelta(0) < delta <= timedelta(hours=expiring_hours):
                            result.append(u.user_id)
                    except Exception:
                        pass
        return result

    async def start_send(
        self,
        broadcast_id: str,
        progress_chat_id: int,
        progress_message_id: int,
    ) -> None:
        job = self._jobs.get(broadcast_id)
        if not job or not self._bot:
            return

        async with self._lock:
            if self._active_broadcast:
                job.status = BroadcastStatus.FAILED
                return
            self._active_broadcast = broadcast_id

        job.status = BroadcastStatus.SENDING
        job.started_at = utcnow_iso()
        job.progress_chat_id = progress_chat_id
        job.progress_message_id = progress_message_id

        recipients = await self.resolve_recipients(job.filter)
        job.total_recipients = len(recipients)

        rt = await settings_repo.get()
        limiter = RateLimiter(rt.broadcast_rate)
        cancel_ev = self._cancel_events.get(broadcast_id) or asyncio.Event()

        for uid in recipients:
            if cancel_ev.is_set():
                job.status = BroadcastStatus.CANCELLED
                break
            await limiter.acquire()
            try:
                await self._send_one(job, uid)
                job.sent += 1
            except TelegramForbiddenError:
                job.blocked += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.5)
                try:
                    await self._send_one(job, uid)
                    job.sent += 1
                except Exception:
                    job.failed += 1
            except Exception as e:
                logger.debug("Broadcast to %s failed: %s", uid, e)
                job.failed += 1

            # Periodic progress update
            done = job.sent + job.failed + job.blocked
            if done % 10 == 0 or done == job.total_recipients:
                await self._update_progress(job)

        if job.status != BroadcastStatus.CANCELLED:
            job.status = BroadcastStatus.COMPLETED
        job.completed_at = utcnow_iso()
        await self._update_progress(job)
        await self._send_completion(job)

        async with self._lock:
            self._active_broadcast = None

    async def _send_one(self, job: BroadcastJob, user_id: int) -> None:
        assert self._bot
        if job.message_type == "text":
            await self._bot.send_message(user_id, job.text or "", parse_mode="HTML")
        elif job.message_type == "photo" and job.file_id:
            await self._bot.send_photo(user_id, job.file_id, caption=job.caption, parse_mode="HTML")
        elif job.message_type == "video" and job.file_id:
            await self._bot.send_video(user_id, job.file_id, caption=job.caption, parse_mode="HTML")
        elif job.message_type == "document" and job.file_id:
            await self._bot.send_document(user_id, job.file_id, caption=job.caption, parse_mode="HTML")
        else:
            await self._bot.send_message(user_id, job.text or "", parse_mode="HTML")

    async def _update_progress(self, job: BroadcastJob) -> None:
        if not self._bot or not job.progress_chat_id or not job.progress_message_id:
            return
        total = max(1, job.total_recipients)
        done = job.sent + job.failed + job.blocked
        pct = done / total * 100
        bar = progress_bar(pct)
        text = (
            f"📢 <b>BROADCASTING</b>\n"
            f"<code>{bar}</code> {pct:.0f}%\n\n"
            f"👥 Total: {job.total_recipients:,}\n"
            f"✅ Sent: {job.sent:,}\n"
            f"❌ Failed: {job.failed:,}\n"
            f"🚫 Blocked: {job.blocked:,}\n\n"
            f"<code>{job.broadcast_id}</code>"
        )
        try:
            await self._bot.edit_message_text(
                text, job.progress_chat_id, job.progress_message_id, parse_mode="HTML"
            )
        except Exception:
            pass

    async def _send_completion(self, job: BroadcastJob) -> None:
        if not self._bot or not job.progress_chat_id:
            return
        duration = "—"
        if job.started_at and job.completed_at:
            try:
                s = datetime.fromisoformat(job.started_at)
                e = datetime.fromisoformat(job.completed_at)
                duration = str(e - s).split(".")[0]
            except Exception:
                pass
        text = (
            f"📢 <b>BROADCAST COMPLETE</b>\n\n"
            f"Job: <code>{job.broadcast_id}</code>\n"
            f"👥 Recipients: {job.total_recipients:,}\n"
            f"✅ Sent: {job.sent:,}\n"
            f"❌ Failed: {job.failed:,}\n"
            f"🚫 Blocked: {job.blocked:,}\n"
            f"⏱ Duration: {duration}"
        )
        try:
            await self._bot.send_message(job.progress_chat_id, text, parse_mode="HTML")
        except Exception:
            pass

    async def cancel(self, broadcast_id: str) -> bool:
        job = self._jobs.get(broadcast_id)
        if not job or job.status not in (BroadcastStatus.SENDING, BroadcastStatus.QUEUED):
            return False
        ev = self._cancel_events.get(broadcast_id)
        if ev:
            ev.set()
        return True


broadcast_manager = BroadcastManager()
