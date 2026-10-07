"""
LINE SEARCHER - Telegram File Downloader

Uses Local Bot API when configured so multi-GB files are accessible.
Downloads only when a search needs the file; caches temporarily.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import File

from app.core.config import get_settings
from app.core.models import CatalogEntry
from app.utils.helpers import safe_path_join

logger = logging.getLogger(__name__)


class FileDownloader:
    """Download Telegram files to a local temp path for searching."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.settings = get_settings()

    async def download_entry(
        self,
        entry: CatalogEntry,
        dest_dir: Path,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Optional[Path]:
        """
        Download a catalog entry to dest_dir.
        Returns local Path or None on failure.
        With Local Bot API (--local), getFile may return a local absolute path
        which we can use directly (or copy if needed).
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = entry.file_name or f"{entry.message_id}.txt"
        # Prevent path traversal in original filename
        safe_name = Path(safe_name).name
        dest = safe_path_join(dest_dir, safe_name)

        # Avoid re-download if already present and size matches
        if dest.exists() and entry.file_size > 0 and dest.stat().st_size == entry.file_size:
            return dest

        try:
            tg_file: File = await self.bot.get_file(entry.file_id)

            # Local Bot API in --local mode: file_path is absolute local path
            if self.settings.use_local_bot_api and tg_file.file_path:
                local_path = Path(tg_file.file_path)
                if local_path.is_absolute() and local_path.exists():
                    # Prefer symlink or hardlink to avoid full copy of multi-GB files
                    try:
                        if dest.exists():
                            dest.unlink()
                        dest.symlink_to(local_path)
                        logger.info("Linked local file %s → %s", local_path, dest)
                        return dest
                    except OSError:
                        # Fallback: stream copy
                        pass

            # Stream download via Bot API
            await self.bot.download_file(
                tg_file.file_path,
                destination=dest,
            )
            logger.info("Downloaded %s (%s bytes)", dest.name, dest.stat().st_size if dest.exists() else 0)
            return dest

        except Exception as e:
            logger.error("Download failed for %s (file_id=%s): %s", entry.file_name, entry.file_id[:20], e)
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            return None

    async def download_many(
        self,
        entries: list[CatalogEntry],
        dest_dir: Path,
        cancel_event: Optional[asyncio.Event] = None,
        on_file_done=None,
    ) -> list[Path]:
        paths = []
        for entry in entries:
            if cancel_event and cancel_event.is_set():
                break
            p = await self.download_entry(entry, dest_dir, cancel_event)
            if p:
                paths.append(p)
            if on_file_done:
                await on_file_done(entry, p is not None)
        return paths
