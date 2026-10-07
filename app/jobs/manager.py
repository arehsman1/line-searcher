"""
LINE SEARCHER - Search Job Queue & Worker Manager

Explicit job states, concurrency control, isolation per user/job,
progress updates, cancellation, restart recovery, cleanup.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import orjson

from app.core.config import get_settings
from app.core.database import plans_repo, settings_repo, storage_repo, users_repo
from app.core.models import JobStatus, SearchJob, utcnow_iso
from app.search.engine import run_search
from app.storage.catalog import catalog_manager
from app.storage.downloader import FileDownloader
from app.utils.helpers import format_bytes, format_duration, progress_bar, job_id_from_seq

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, SearchJob] = {}
        self._cancel_events: Dict[str, asyncio.Event] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._active: set[str] = set()
        self._workers: List[asyncio.Task] = []
        self._seq = 0
        self._seq_lock = asyncio.Lock()
        self._bot = None  # set later
        self._progress_callbacks: Dict[str, Callable] = {}
        self._running = False

    def set_bot(self, bot) -> None:
        self._bot = bot

    async def start(self, num_workers: int | None = None) -> None:
        if self._running:
            return
        self._running = True
        await self._load_persisted()
        await self._recover_interrupted()
        cfg = get_settings()
        n = num_workers or cfg.max_concurrent_searches
        for i in range(n):
            t = asyncio.create_task(self._worker_loop(i), name=f"search-worker-{i}")
            self._workers.append(t)
        # Cleanup loop
        asyncio.create_task(self._cleanup_loop(), name="job-cleanup")
        logger.info("JobManager started with %d workers", n)

    async def stop(self) -> None:
        self._running = False
        for t in self._workers:
            t.cancel()
        self._workers.clear()

    async def _next_id(self) -> str:
        async with self._seq_lock:
            self._seq += 1
            return job_id_from_seq(self._seq)

    def _job_path(self, job_id: str) -> Path:
        return get_settings().jobs_dir / f"{job_id}.json"

    async def _persist(self, job: SearchJob) -> None:
        path = self._job_path(job.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(orjson.dumps(job.model_dump(mode="json"), option=orjson.OPT_INDENT_2))

    async def _load_persisted(self) -> None:
        jobs_dir = get_settings().jobs_dir
        if not jobs_dir.exists():
            return
        max_seq = 0
        for f in jobs_dir.glob("JOB-*.json"):
            try:
                data = orjson.loads(f.read_bytes())
                job = SearchJob.model_validate(data)
                self._jobs[job.job_id] = job
                try:
                    n = int(job.job_id.split("-")[1])
                    max_seq = max(max_seq, n)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Could not load job %s: %s", f, e)
        self._seq = max_seq

    async def _recover_interrupted(self) -> None:
        active_states = {
            JobStatus.CREATED, JobStatus.QUEUED, JobStatus.PREPARING,
            JobStatus.DOWNLOADING, JobStatus.SEARCHING, JobStatus.GENERATING_RESULTS,
            JobStatus.DELIVERING, JobStatus.CANCEL_REQUESTED,
        }
        for job in list(self._jobs.values()):
            if job.status in active_states:
                if job.status == JobStatus.CANCEL_REQUESTED:
                    job.status = JobStatus.CANCELLED
                else:
                    job.status = JobStatus.INTERRUPTED
                    job.error_message = "Interrupted by server restart"
                job.completed_at = utcnow_iso()
                await self._persist(job)
                logger.info("Recovered interrupted job %s → %s", job.job_id, job.status)

    async def create_job(
        self,
        user_id: int,
        keywords: List[str],
        case_sensitive: bool = False,
        storage_ids: Optional[List[str]] = None,
        progress_chat_id: Optional[int] = None,
        progress_message_id: Optional[int] = None,
    ) -> SearchJob:
        user = await users_repo.get(user_id)
        plan = user.plan if user else "FREE"
        job_id = await self._next_id()
        result_dir = str(get_settings().results_dir / str(user_id) / job_id)
        job = SearchJob(
            job_id=job_id,
            user_id=user_id,
            plan=plan,
            keywords=keywords,
            case_sensitive=case_sensitive,
            storage_ids=storage_ids or ["public"],
            status=JobStatus.CREATED,
            progress_chat_id=progress_chat_id,
            progress_message_id=progress_message_id,
            result_dir=result_dir,
        )
        self._jobs[job_id] = job
        self._cancel_events[job_id] = asyncio.Event()
        await self._persist(job)

        # Queue
        job.status = JobStatus.QUEUED
        await self._persist(job)
        await self._queue.put(job_id)
        logger.info("Job %s queued for user %s keywords=%s", job_id, user_id, keywords)
        return job

    def get(self, job_id: str) -> Optional[SearchJob]:
        return self._jobs.get(job_id)

    def user_jobs(self, user_id: int, limit: int = 20) -> List[SearchJob]:
        jobs = [j for j in self._jobs.values() if j.user_id == user_id]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def active_count(self) -> int:
        return len(self._active)

    def queue_size(self) -> int:
        return self._queue.qsize()

    async def cancel(self, job_id: str, by_admin: bool = False) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        if job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.EXPIRED):
            return False
        job.status = JobStatus.CANCEL_REQUESTED
        await self._persist(job)
        ev = self._cancel_events.get(job_id)
        if ev:
            ev.set()
        logger.info("Cancel requested for %s (admin=%s)", job_id, by_admin)
        return True

    async def _worker_loop(self, worker_id: int) -> None:
        while self._running:
            try:
                job_id = await asyncio.wait_for(self._queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            job = self._jobs.get(job_id)
            if not job:
                continue
            if job.status == JobStatus.CANCEL_REQUESTED:
                job.status = JobStatus.CANCELLED
                await self._persist(job)
                continue

            self._active.add(job_id)
            try:
                await self._execute_job(job)
            except Exception as e:
                logger.exception("Worker %s job %s crashed: %s", worker_id, job_id, e)
                job.status = JobStatus.FAILED
                job.error_message = "Internal error"
                job.completed_at = utcnow_iso()
                await self._persist(job)
            finally:
                self._active.discard(job_id)

    async def _execute_job(self, job: SearchJob) -> None:
        cancel_ev = self._cancel_events.get(job.job_id) or asyncio.Event()
        settings = get_settings()
        downloader = FileDownloader(self._bot) if self._bot else None

        async def update_progress(data: dict) -> None:
            job.processed_bytes = data.get("processed_bytes", 0)
            job.total_bytes = data.get("total_bytes", 0)
            job.progress_percent = data.get("percent", 0)
            job.current_file = data.get("current_file", "")
            job.files_scanned = data.get("files_scanned", 0)
            job.speed_bps = data.get("speed_bps", 0)
            job.eta_seconds = data.get("eta_seconds")
            job.matches_found = data.get("matches", 0)
            await self._persist(job)
            await self._edit_progress_message(job)

        try:
            # PREPARING
            job.status = JobStatus.PREPARING
            job.started_at = utcnow_iso()
            await self._persist(job)
            await self._edit_progress_message(job)

            # Collect catalog entries
            entries = []
            for sid in job.storage_ids:
                entries.extend(await catalog_manager.list_available(sid))

            if not entries:
                job.status = JobStatus.FAILED
                job.error_message = "No searchable files in storage. Admin must configure/refresh Public Storage."
                job.completed_at = utcnow_iso()
                await self._persist(job)
                await self._edit_progress_message(job)
                return

            total_size = sum(e.file_size for e in entries)
            job.total_bytes = total_size
            await self._persist(job)

            # Count search against daily limit when processing begins
            user = await users_repo.get(job.user_id)
            if user:
                user = await users_repo.reset_daily_if_needed(user, settings.timezone)
                user.daily_searches_used += 1
                user.total_searches += 1
                await users_repo.save(user)

            # DOWNLOADING
            job.status = JobStatus.DOWNLOADING
            await self._persist(job)
            await self._edit_progress_message(job)

            download_dir = Path(job.result_dir or "") / "_sources"
            download_dir.mkdir(parents=True, exist_ok=True)
            local_files: List[Path] = []

            if downloader:
                for entry in entries:
                    if cancel_ev.is_set():
                        break
                    p = await downloader.download_entry(entry, download_dir, cancel_ev)
                    if p:
                        local_files.append(p)
                    else:
                        logger.warning("Skipping unavailable file %s", entry.file_name)
            else:
                job.status = JobStatus.FAILED
                job.error_message = "Downloader not initialized"
                job.completed_at = utcnow_iso()
                await self._persist(job)
                return

            if cancel_ev.is_set():
                job.status = JobStatus.CANCELLED
                job.completed_at = utcnow_iso()
                await self._persist(job)
                await self._cleanup_job_files(job)
                return

            if not local_files:
                job.status = JobStatus.FAILED
                job.error_message = "Could not download any source files"
                job.completed_at = utcnow_iso()
                await self._persist(job)
                await self._edit_progress_message(job)
                return

            # SEARCHING
            job.status = JobStatus.SEARCHING
            await self._persist(job)
            await self._edit_progress_message(job)

            result_out = Path(job.result_dir or "") / "out"
            result_out.mkdir(parents=True, exist_ok=True)

            summary = await run_search(
                source_files=local_files,
                keywords=job.keywords,
                output_dir=result_out,
                case_sensitive=job.case_sensitive,
                max_part_bytes=settings.max_result_file_size_mb * 1024 * 1024,
                cancel_event=cancel_ev,
                progress_callback=update_progress,
            )

            if summary.get("cancelled") or cancel_ev.is_set():
                job.status = JobStatus.CANCELLED
                job.completed_at = utcnow_iso()
                await self._persist(job)
                await self._cleanup_job_files(job)
                await self._edit_progress_message(job)
                return

            # GENERATING_RESULTS / DELIVERING
            job.status = JobStatus.GENERATING_RESULTS
            job.matches_found = summary["total_matches"]
            job.files_scanned = summary["files_scanned"]
            job.processed_bytes = summary["processed_bytes"]
            all_results = []
            for kw, paths in summary.get("result_map", {}).items():
                all_results.extend(paths)
            job.result_files = all_results
            await self._persist(job)

            job.status = JobStatus.DELIVERING
            await self._persist(job)

            # Deliver results
            await self._deliver_results(job, summary)

            job.status = JobStatus.COMPLETED
            job.completed_at = utcnow_iso()
            job.progress_percent = 100.0
            await self._persist(job)
            await self._edit_progress_message(job)

            # Remove source downloads (keep results for retention period)
            if download_dir.exists():
                shutil.rmtree(download_dir, ignore_errors=True)

        except Exception as e:
            logger.exception("Job %s failed: %s", job.job_id, e)
            job.status = JobStatus.FAILED
            job.error_message = str(e)[:200]
            job.completed_at = utcnow_iso()
            await self._persist(job)
            await self._edit_progress_message(job)

    async def _edit_progress_message(self, job: SearchJob) -> None:
        if not self._bot or not job.progress_chat_id or not job.progress_message_id:
            return
        try:
            text = self._format_progress(job)
            await self._bot.edit_message_text(
                text=text,
                chat_id=job.progress_chat_id,
                message_id=job.progress_message_id,
                parse_mode="HTML",
            )
        except Exception as e:
            # Message not modified / flood etc. — ignore
            if "message is not modified" not in str(e).lower():
                logger.debug("Progress edit failed: %s", e)

    def _format_progress(self, job: SearchJob) -> str:
        from app.utils.helpers import escape_html
        status_icons = {
            JobStatus.QUEUED: "⏳",
            JobStatus.PREPARING: "🟠",
            JobStatus.DOWNLOADING: "📥",
            JobStatus.SEARCHING: "🔎",
            JobStatus.GENERATING_RESULTS: "📄",
            JobStatus.DELIVERING: "📤",
            JobStatus.COMPLETED: "✅",
            JobStatus.CANCELLED: "❌",
            JobStatus.FAILED: "🔴",
            JobStatus.CANCEL_REQUESTED: "⚠️",
        }
        icon = status_icons.get(job.status, "⚪")
        bar = progress_bar(job.progress_percent)
        lines = [
            f"{icon} <b>SEARCHING</b>",
            f"<code>{bar}</code> {job.progress_percent:.0f}%",
            f"",
            f"📂 File: <code>{escape_html(job.current_file or '—')}</code>",
            f"⚡ Speed: {format_bytes(job.speed_bps)}/s",
            f"⏱ ETA: {format_duration(job.eta_seconds) if job.eta_seconds else 'Calculating...'}",
            f"📄 Matches: {job.matches_found:,}",
            f"📊 Scanned: {format_bytes(job.processed_bytes)} / {format_bytes(job.total_bytes)}",
            f"",
            f"<code>{job.job_id}</code>",
        ]
        if job.status == JobStatus.COMPLETED:
            lines = [
                f"✅ <b>SEARCH COMPLETE</b>",
                f"",
                f"<code>{job.job_id}</code>",
                f"📄 Matches: {job.matches_found:,}",
                f"📂 Files scanned: {job.files_scanned}",
                f"📊 Data: {format_bytes(job.processed_bytes)}",
            ]
        elif job.status == JobStatus.FAILED:
            lines = [
                f"❌ <b>SEARCH FAILED</b>",
                f"",
                f"<code>{job.job_id}</code>",
                f"{escape_html(job.error_message or 'Unknown error')}",
            ]
        elif job.status == JobStatus.CANCELLED:
            lines = [
                f"❌ <b>SEARCH CANCELLED</b>",
                f"",
                f"<code>{job.job_id}</code>",
            ]
        return "\n".join(lines)

    async def _deliver_results(self, job: SearchJob, summary: dict) -> None:
        if not self._bot:
            return
        chat_id = job.progress_chat_id or job.user_id
        result_map = summary.get("result_map", {})

        # Summary first
        no_match_kws = [kw for kw in job.keywords if kw not in result_map]
        if no_match_kws:
            for kw in no_match_kws:
                try:
                    await self._bot.send_message(
                        chat_id,
                        f"🔎 <b>{kw}</b>\nNo matches found.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        from aiogram.types import FSInputFile
        for kw, paths in result_map.items():
            for p in paths:
                path = Path(p)
                if not path.exists():
                    continue
                try:
                    await self._bot.send_document(
                        chat_id,
                        document=FSInputFile(path, filename=path.name),
                        caption=f"🔎 {kw}" if len(paths) == 1 else f"🔎 {kw} — {path.name}",
                    )
                except Exception as e:
                    logger.error("Deliver result failed %s: %s", path, e)
                    # Keep file longer for retry
                    pass

        # Final summary
        try:
            await self._bot.send_message(
                chat_id,
                (
                    f"🎉 <b>SEARCH COMPLETE</b>\n\n"
                    f"<code>{job.job_id}</code>\n"
                    f"⏱ Time: {format_duration(summary.get('duration_seconds'))}\n"
                    f"📂 Files scanned: {summary.get('files_scanned', 0)}\n"
                    f"📊 Data scanned: {format_bytes(summary.get('processed_bytes', 0))}\n"
                    f"🔎 Keywords: {len(job.keywords)}\n"
                    f"📄 Result files: {len(job.result_files)}"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

    async def _cleanup_job_files(self, job: SearchJob) -> None:
        if job.result_dir:
            p = Path(job.result_dir)
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

    async def _cleanup_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(300)
                settings = get_settings()
                now = datetime.now(timezone.utc)
                for job in list(self._jobs.values()):
                    if not job.completed_at:
                        continue
                    try:
                        done = datetime.fromisoformat(job.completed_at)
                        if done.tzinfo is None:
                            done = done.replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    retention = settings.result_retention_seconds
                    if job.status == JobStatus.FAILED:
                        retention = settings.failed_result_retention_seconds
                    if (now - done).total_seconds() > retention:
                        await self._cleanup_job_files(job)
                        # Optionally remove old job record after longer period
                        if (now - done).total_seconds() > retention * 3:
                            path = self._job_path(job.job_id)
                            if path.exists():
                                path.unlink(missing_ok=True)
                            self._jobs.pop(job.job_id, None)
                            self._cancel_events.pop(job.job_id, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Cleanup error: %s", e)


job_manager = JobManager()
