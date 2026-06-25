import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import load_config
from handlers import start, rates, orders
from services.rate_cache import RateCache
from services.sheets import SheetsCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()

    bot = Bot(token=config.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    rate_cache = RateCache(ttl_seconds=config.rate_cache_ttl_seconds)
    sheets_cache = SheetsCache(
        spreadsheet_id=config.google_sheet_id,
        credentials_path=config.google_credentials_path,
        sheet_name=config.google_sheet_name,
    )

    dp["rate_cache"] = rate_cache
    dp["sheets_cache"] = sheets_cache

    dp.include_router(start.router)
    dp.include_router(rates.router)
    dp.include_router(orders.router)

    # начальная загрузка таблицы
    logger.info("Loading Google Sheets data...")
    await sheets_cache.refresh()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sheets_cache.refresh,
        trigger="interval",
        minutes=config.sheets_refresh_minutes,
    )
    scheduler.start()

    logger.info("Starting bot (long polling)")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
