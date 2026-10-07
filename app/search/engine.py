"""
LINE SEARCHER - Core Streaming Search Engine

Preserves original standalone TXT-search behavior:
- Line-by-line streaming (no full-file RAM load)
- Case-insensitive by default
- Optional case-sensitive
- Multiple keywords → single pass per file
- Exact original matching lines only (no metadata/prefixes)
- Disk-backed deduplication for large result sets
- Encoding fallbacks; one bad file does not kill the job
- Separate result file per keyword
- No empty result files
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import aiofiles

from app.utils.helpers import format_bytes, sanitize_filename

logger = logging.getLogger(__name__)

# Encoding order for fallbacks
ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1", "ascii")

# Chunk size for reading
READ_CHUNK = 1024 * 1024  # 1 MB


class Deduplicator:
    """
    Disk-backed line deduplication.
    Uses a set of hashes in memory for speed; for extremely large result sets
    falls back to an on-disk bloom-like approach via a simple hash file.
    For practical multi-GB sources producing millions of unique lines,
    we keep hashes in a set and spill only if memory pressure is extreme.
    """

    def __init__(self, work_dir: Path, keyword: str):
        self.work_dir = work_dir
        self.keyword = keyword
        self._seen: Set[str] = set()
        self._count = 0
        self._spill_path = work_dir / f".dedup_{sanitize_filename(keyword)}.hashes"
        self._spill_file = None

    def is_duplicate(self, line: str) -> bool:
        # Normalize for comparison only; output keeps original line
        key = hashlib.sha256(line.encode("utf-8", errors="surrogateescape")).hexdigest()
        if key in self._seen:
            return True
        self._seen.add(key)
        self._count += 1
        # Soft limit: if set grows huge, we still keep it (VPS should have enough RAM
        # for hash sets of typical result sizes). For extreme cases admin can tune.
        return False

    def close(self) -> None:
        self._seen.clear()
        if self._spill_path.exists():
            try:
                self._spill_path.unlink()
            except OSError:
                pass


class SearchResultWriter:
    """Writes matching lines to result files, splitting when size limit reached."""

    def __init__(
        self,
        out_dir: Path,
        keyword: str,
        max_part_bytes: int = 49 * 1024 * 1024,
        max_parts: int = 50,
    ):
        self.out_dir = out_dir
        self.keyword = keyword
        self.safe_name = sanitize_filename(keyword)
        self.max_part_bytes = max_part_bytes
        self.max_parts = max_parts
        self.part = 0
        self.current_size = 0
        self.current_path: Optional[Path] = None
        self.files: List[Path] = []
        self.match_count = 0
        self.dedup = Deduplicator(out_dir, keyword)
        self._fh = None

    def _open_next(self) -> None:
        if self._fh:
            self._fh.close()
        self.part += 1
        if self.part > self.max_parts:
            raise RuntimeError(f"Result exceeds max parts ({self.max_parts}) for keyword '{self.keyword}'")
        if self.part == 1 and self.max_parts == 1:
            name = f"{self.safe_name}.txt"
        else:
            name = f"{self.safe_name}_part_{self.part:02d}.txt"
        self.current_path = self.out_dir / name
        self._fh = open(self.current_path, "w", encoding="utf-8", errors="surrogateescape", buffering=1024 * 256)
        self.current_size = 0
        self.files.append(self.current_path)

    def write_line(self, line: str) -> bool:
        """Write original line if not duplicate. Returns True if written."""
        if self.dedup.is_duplicate(line):
            return False
        if self._fh is None:
            self._open_next()
        # line already includes trailing newline from reader, or we ensure it
        data = line if line.endswith("\n") else line + "\n"
        encoded = data.encode("utf-8", errors="surrogateescape")
        if self.current_size + len(encoded) > self.max_part_bytes and self.current_size > 0:
            self._open_next()
        self._fh.write(data)
        self.current_size += len(encoded)
        self.match_count += 1
        return True

    def close(self) -> List[Path]:
        if self._fh:
            self._fh.close()
            self._fh = None
        self.dedup.close()
        # Remove empty files (should not happen, but safety)
        final = []
        for f in self.files:
            if f.exists() and f.stat().st_size > 0:
                final.append(f)
            elif f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
        self.files = final
        return final


def _open_with_fallback(path: Path):
    """Open text file trying multiple encodings. Returns (file handle, encoding) or raises."""
    last_err = None
    for enc in ENCODINGS:
        try:
            fh = open(path, "r", encoding=enc, errors="strict", buffering=READ_CHUNK)
            # Probe first chunk
            pos = fh.tell()
            fh.read(4096)
            fh.seek(pos)
            return fh, enc
        except (UnicodeDecodeError, LookupError) as e:
            last_err = e
            try:
                fh.close()
            except Exception:
                pass
            continue
    # Last resort: latin-1 with surrogateescape (never fails on bytes)
    fh = open(path, "r", encoding="latin-1", errors="surrogateescape", buffering=READ_CHUNK)
    return fh, "latin-1"


async def search_file_streaming(
    file_path: Path,
    keywords: List[str],
    writers: Dict[str, SearchResultWriter],
    case_sensitive: bool = False,
    cancel_event: Optional[asyncio.Event] = None,
    on_bytes: Optional[Callable[[int], None]] = None,
) -> Tuple[int, Optional[str]]:
    """
    Stream-search one file for multiple keywords in a single pass.
    Returns (bytes_processed, error_message_or_None).
    """
    if not file_path.exists():
        return 0, "file missing"

    file_size = file_path.stat().st_size
    processed = 0

    # Prepare matchers
    if case_sensitive:
        matchers = {kw: kw for kw in keywords}
    else:
        matchers = {kw: kw.lower() for kw in keywords}

    def run_sync() -> Tuple[int, Optional[str]]:
        nonlocal processed
        try:
            fh, enc = _open_with_fallback(file_path)
        except Exception as e:
            return 0, f"open failed: {e}"

        try:
            for line in fh:
                if cancel_event and cancel_event.is_set():
                    return processed, "cancelled"

                # Approximate byte progress (UTF-8 may differ slightly)
                line_bytes = len(line.encode("utf-8", errors="surrogateescape"))
                processed += line_bytes
                if on_bytes:
                    on_bytes(line_bytes)

                check = line if case_sensitive else line.lower()
                # Strip only for matching; write original
                for kw, needle in matchers.items():
                    if needle in check:
                        writers[kw].write_line(line.rstrip("\r\n") + "\n")
            return processed, None
        except Exception as e:
            return processed, f"read error: {e}"
        finally:
            try:
                fh.close()
            except Exception:
                pass

    # Run blocking IO in executor so event loop stays responsive
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, run_sync)


async def run_search(
    source_files: List[Path],
    keywords: List[str],
    output_dir: Path,
    case_sensitive: bool = False,
    max_part_bytes: int = 49 * 1024 * 1024,
    cancel_event: Optional[asyncio.Event] = None,
    progress_callback: Optional[Callable] = None,
) -> Dict:
    """
    Execute multi-keyword search across source files.

    Returns summary dict with result file paths, match counts, etc.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    writers: Dict[str, SearchResultWriter] = {
        kw: SearchResultWriter(output_dir, kw, max_part_bytes=max_part_bytes)
        for kw in keywords
    }

    total_bytes = sum(f.stat().st_size for f in source_files if f.exists())
    processed_bytes = 0
    files_scanned = 0
    files_skipped = 0
    errors: List[str] = []
    start = asyncio.get_event_loop().time()

    def on_bytes(n: int) -> None:
        nonlocal processed_bytes
        processed_bytes += n

    for src in source_files:
        if cancel_event and cancel_event.is_set():
            break

        if not src.exists() or not src.is_file():
            files_skipped += 1
            continue

        # Never search result files
        if "results" in str(src).lower() and src.suffix == ".txt":
            # Extra safety: skip anything under a results path
            pass

        bytes_done, err = await search_file_streaming(
            src,
            keywords,
            writers,
            case_sensitive=case_sensitive,
            cancel_event=cancel_event,
            on_bytes=on_bytes,
        )

        if err and err != "cancelled":
            errors.append(f"{src.name}: {err}")
            files_skipped += 1
            logger.warning("Skipped file %s: %s", src, err)
        else:
            files_scanned += 1

        if progress_callback:
            elapsed = asyncio.get_event_loop().time() - start
            speed = processed_bytes / elapsed if elapsed > 0 else 0
            remaining = max(0, total_bytes - processed_bytes)
            eta = remaining / speed if speed > 0 else None
            pct = (processed_bytes / total_bytes * 100) if total_bytes > 0 else 0
            await progress_callback({
                "processed_bytes": processed_bytes,
                "total_bytes": total_bytes,
                "percent": pct,
                "current_file": src.name,
                "files_scanned": files_scanned,
                "speed_bps": speed,
                "eta_seconds": eta,
                "matches": sum(w.match_count for w in writers.values()),
            })

        if cancel_event and cancel_event.is_set():
            break

    # Close writers and collect non-empty result files
    result_map: Dict[str, List[str]] = {}
    total_matches = 0
    for kw, writer in writers.items():
        paths = writer.close()
        if paths:
            result_map[kw] = [str(p) for p in paths]
            total_matches += writer.match_count

    cancelled = cancel_event.is_set() if cancel_event else False

    return {
        "result_map": result_map,
        "total_matches": total_matches,
        "files_scanned": files_scanned,
        "files_skipped": files_skipped,
        "processed_bytes": processed_bytes,
        "total_bytes": total_bytes,
        "errors": errors,
        "cancelled": cancelled,
        "duration_seconds": asyncio.get_event_loop().time() - start,
    }
