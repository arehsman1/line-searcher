"""
LINE SEARCHER - Shared helpers
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Optional


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Safe filename from keyword. Does NOT change the search keyword itself."""
    # Replace path separators and dangerous chars
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    if not name:
        name = "keyword"
    if len(name) > max_len:
        name = name[: max_len - 8] + "_" + hashlib.md5(name.encode()).hexdigest()[:7]
    return name


def format_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def progress_bar(percent: float, width: int = 10) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100))
    return "█" * filled + "░" * (width - filled)


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def job_id_from_seq(seq: int) -> str:
    return f"JOB-{seq}"


def broadcast_id_from_seq(seq: int) -> str:
    return f"B{seq}"


def safe_path_join(base: Path, *parts: str) -> Path:
    """Prevent path traversal."""
    result = base
    for p in parts:
        # Reject any path components that could escape
        clean = Path(p).name  # strips directories
        if not clean or clean in (".", ".."):
            raise ValueError(f"Unsafe path component: {p}")
        result = result / clean
    # Final check
    try:
        result.resolve().relative_to(base.resolve())
    except ValueError:
        raise ValueError("Path traversal detected")
    return result


class RateLimiter:
    """Simple token-bucket style rate limiter for broadcasts."""

    def __init__(self, rate_per_second: float):
        self.rate = rate_per_second
        self.tokens = rate_per_second
        self.updated = time.monotonic()

    async def acquire(self) -> None:
        import asyncio
        while True:
            now = time.monotonic()
            elapsed = now - self.updated
            self.updated = now
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            wait = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(max(0.01, wait))
