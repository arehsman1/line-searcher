"""
LINE SEARCHER — Application Entry Point

Production Telegram bot for large-scale TXT search.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import setup_routers
from app.bot.middlewares import BlockMiddleware, MaintenanceMiddleware, UserMiddleware
from app.broadcast.manager import broadcast_manager
from app.core.config import get_settings
from app.jobs.manager import job_manager


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "line-searcher.log"
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Quiet noisy libs
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def build_bot(settings) -> Bot:
    session = None
    if settings.use_local_bot_api:
        api = TelegramAPIServer.from_base(settings.local_bot_api_url, is_local=True)
        session = AiohttpSession(api=api)
        logging.getLogger(__name__).info(
            "Using Local Bot API at %s (large file support enabled)",
            settings.local_bot_api_url,
        )
    else:
        logging.getLogger(__name__).warning(
            "Local Bot API DISABLED — downloads limited to 20MB, uploads to 50MB"
        )

    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def on_startup(bot: Bot) -> None:
    me = await bot.get_me()
    logging.getLogger(__name__).info("Bot started: @%s (%s)", me.username, me.id)
    job_manager.set_bot(bot)
    broadcast_manager.set_bot(bot)
    await job_manager.start()
    logging.getLogger(__name__).info("Job manager & workers started")


async def on_shutdown(bot: Bot) -> None:
    logging.getLogger(__name__).info("Shutting down…")
    await job_manager.stop()
    await bot.session.close()


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_dir)
    settings.ensure_dirs()

    if not settings.admin_ids:
        logging.error("ADMIN_TELEGRAM_IDS is empty — set at least one admin")
        sys.exit(1)

    bot = build_bot(settings)
    dp = Dispatcher(storage=MemoryStorage())

    # Middlewares (order matters)
    dp.message.middleware(UserMiddleware())
    dp.callback_query.middleware(UserMiddleware())
    dp.message.middleware(BlockMiddleware())
    dp.callback_query.middleware(BlockMiddleware())
    dp.message.middleware(MaintenanceMiddleware())
    dp.callback_query.middleware(MaintenanceMiddleware())

    dp.include_router(setup_routers())

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logging.getLogger(__name__).info(
        "LINE SEARCHER starting | admins=%s | data=%s",
        settings.admin_ids,
        settings.data_dir,
    )

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(main())
