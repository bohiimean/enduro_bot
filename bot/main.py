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

    rate_cache = RateCache()
    rapira_provider = RapiraProvider()
    usd_provider: RateProvider = InvestingComProvider(chrome_binary=config.chrome_binary_path)
    if config.twelvedata_api_key:
        usd_provider = FallbackProvider(primary=usd_provider, secondary=TwelveDataProvider(api_key=config.twelvedata_api_key))
        logger.info("USD provider: InvestingCom → TwelveData fallback")
    else:
        logger.info("USD provider: InvestingCom (no fallback)")
    rate_cache.register("usdt_rub", rapira_provider)
    rate_cache.register("usd_rub", usd_provider)
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
    dp["sheets_cache"] = sheets_cache
    dp["catalog_cache"] = catalog_cache
    dp["drive_cache"] = drive_cache
    dp["manager_tg_username"] = config.manager_tg_username

    dp.include_router(start.router)
    dp.include_router(rates.router)
    dp.include_router(orders.router)
    dp.include_router(catalog.router)

    logger.info("Loading initial data...")
    await asyncio.gather(
        sheets_cache.refresh(),
        catalog_cache.refresh(),
        rate_cache.refresh("usdt_rub"),
        rate_cache.refresh("usd_rub"),
    )

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
        rate_cache.refresh,
        trigger="interval",
        minutes=20,
        args=["usdt_rub"],
    )
    scheduler.add_job(
        rate_cache.refresh,
        trigger="interval",
        minutes=20,
        args=["usd_rub"],
    )
    scheduler.start()

    logger.info("Starting bot (long polling)")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
