import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import load_config
from handlers import start, rates, orders, catalog
from services.catalog_cache import CatalogCache
from services.drive_photos import DrivePhotoCache
from services.rate_cache import RateCache
from services.rate_providers.base import RateProvider
from services.rate_providers.fallback import FallbackProvider
from services.rate_providers.investing import InvestingComProvider
from services.rate_providers.rapira import RapiraProvider
from services.rate_providers.twelvedata import TwelveDataProvider
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
    rapira_provider = RapiraProvider()
    usd_provider: RateProvider = InvestingComProvider(chrome_binary=config.chrome_binary_path)
    if config.twelvedata_api_key:
        usd_provider = FallbackProvider(primary=usd_provider, secondary=TwelveDataProvider(api_key=config.twelvedata_api_key))
        logger.info("USD provider: InvestingCom → TwelveData fallback")
    else:
        logger.info("USD provider: InvestingCom (no fallback)")
    sheets_cache = SheetsCache(
        spreadsheet_id=config.google_sheet_id,
        credentials_path=config.google_credentials_path,
        sheet_name=config.google_sheet_name,
    )
    catalog_cache = CatalogCache(
        spreadsheet_id=config.google_sheet_id,
        credentials_path=config.google_credentials_path,
        sheet_name=config.google_catalog_sheet_name,
    )
    drive_cache = DrivePhotoCache(credentials_path=config.google_credentials_path)

    dp["rate_cache"] = rate_cache
    dp["usd_provider"] = usd_provider
    dp["sheets_cache"] = sheets_cache
    dp["catalog_cache"] = catalog_cache
    dp["drive_cache"] = drive_cache
    dp["manager_tg_username"] = config.manager_tg_username

    dp.include_router(start.router)
    dp.include_router(rates.router)
    dp.include_router(orders.router)
    dp.include_router(catalog.router)

    logger.info("Loading initial data...")
    await asyncio.gather(sheets_cache.refresh(), catalog_cache.refresh())

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sheets_cache.refresh,
        trigger="interval",
        minutes=config.sheets_refresh_minutes,
    )
    scheduler.add_job(
        catalog_cache.refresh,
        trigger="interval",
        minutes=config.sheets_refresh_minutes,
    )
    scheduler.add_job(
        rate_cache.get,
        trigger="interval",
        minutes=20,
        args=["usdt_rub", rapira_provider],
    )
    scheduler.add_job(
        rate_cache.get,
        trigger="interval",
        minutes=20,
        args=["usd_rub", usd_provider],
    )
    scheduler.start()

    logger.info("Pre-warming rate cache...")
    asyncio.create_task(rate_cache.get("usdt_rub", rapira_provider))
    asyncio.create_task(rate_cache.get("usd_rub", usd_provider))

    logger.info("Starting bot (long polling)")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
