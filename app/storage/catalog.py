"""
LINE SEARCHER - File Catalog / Index Layer

Telegram is the source of truth. We index metadata only.
Download source data only when a search actually needs it.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional

import orjson

from app.core.config import get_settings
from app.core.models import CatalogEntry, utcnow_iso

logger = logging.getLogger(__name__)

_catalog_locks: Dict[str, asyncio.Lock] = {}


def _lock_for(storage_id: str) -> asyncio.Lock:
    if storage_id not in _catalog_locks:
        _catalog_locks[storage_id] = asyncio.Lock()
    return _catalog_locks[storage_id]


def _catalog_path(storage_id: str) -> Path:
    return get_settings().catalog_dir / f"{storage_id}.json"


class CatalogManager:
    """Maintains an index of TXT files available in a Telegram storage chat."""

    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, CatalogEntry]] = {}

    def _key(self, chat_id: int, message_id: int) -> str:
        return f"{chat_id}:{message_id}"

    async def load(self, storage_id: str) -> Dict[str, CatalogEntry]:
        if storage_id in self._cache:
            return self._cache[storage_id]
        path = _catalog_path(storage_id)
        entries: Dict[str, CatalogEntry] = {}
        if path.exists():
            try:
                raw = orjson.loads(path.read_bytes())
                for k, v in raw.items():
                    entries[k] = CatalogEntry.model_validate(v)
            except Exception as e:
                logger.error("Catalog load failed %s: %s", storage_id, e)
        self._cache[storage_id] = entries
        return entries

    async def save(self, storage_id: str) -> None:
        async with _lock_for(storage_id):
            entries = self._cache.get(storage_id, {})
            path = _catalog_path(storage_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: e.model_dump(mode="json") for k, e in entries.items()}
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(orjson.dumps(data, option=orjson.OPT_INDENT_2))
            tmp.replace(path)

    async def upsert(self, storage_id: str, entry: CatalogEntry) -> None:
        entries = await self.load(storage_id)
        key = self._key(entry.chat_id, entry.message_id)
        entries[key] = entry
        self._cache[storage_id] = entries

    async def mark_missing(self, storage_id: str, chat_id: int, message_id: int) -> None:
        entries = await self.load(storage_id)
        key = self._key(chat_id, message_id)
        if key in entries:
            entries[key].status = "missing"
            await self.save(storage_id)

    async def list_available(self, storage_id: str) -> List[CatalogEntry]:
        entries = await self.load(storage_id)
        return [e for e in entries.values() if e.status == "available" and e.file_name.lower().endswith(".txt")]

    async def stats(self, storage_id: str) -> dict:
        entries = await self.load(storage_id)
        available = [e for e in entries.values() if e.status == "available"]
        return {
            "total": len(entries),
            "available": len(available),
            "total_size": sum(e.file_size for e in available),
            "missing": sum(1 for e in entries.values() if e.status == "missing"),
        }

    async def clear(self, storage_id: str) -> None:
        async with _lock_for(storage_id):
            self._cache[storage_id] = {}
            path = _catalog_path(storage_id)
            if path.exists():
                path.unlink()


catalog_manager = CatalogManager()
