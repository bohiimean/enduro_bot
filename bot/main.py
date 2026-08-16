import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import load_config
from handlers import start, rates, orders, catalog, fallback, payment
from handlers.errors import on_unhandled_error
from handlers.payment import notify_kyc_resolved
from middlewares.private_only import PrivateOnlyMiddleware
from services.catalog_cache import CatalogCache
from services.drive_photos import DrivePhotoCache
from services.alfabit.client import AlfabitClient
from services.kyc_watch import POLL_INTERVAL_SECONDS, KycWatcher, Watch
from services.rate_cache import RateCache
from services.rate_providers.alfabit import AlfabitWidgetProvider
from services.rate_providers.base import RateProvider
from services.rate_providers.fallback import FallbackProvider
from services.rate_providers.investing import InvestingComProvider
from services.rate_providers.rapira import RapiraProvider
from services.rate_providers.twelvedata import TwelveDataProvider
from services.public_ip import detect_public_ip
from services.sheets import SheetsCache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    config = load_config()

    bot = Bot(token=config.bot_token)
    # Хранилище держим переменной: фоновое уведомление о результате верификации
    # собирает FSMContext само, без входящего апдейта (handlers/payment.py).
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    rate_cache = RateCache()
    rapira_provider = RapiraProvider(markup=config.rapira_usdt_markup)
    usd_provider: RateProvider = InvestingComProvider(chrome_binary=config.chrome_binary_path)
    if config.twelvedata_api_key:
        usd_provider = FallbackProvider(primary=usd_provider, secondary=TwelveDataProvider(api_key=config.twelvedata_api_key))
        logger.info("USD provider: InvestingCom → TwelveData fallback")
    else:
        logger.info("USD provider: InvestingCom (no fallback)")
    rate_cache.register("usdt_rub", rapira_provider)
    rate_cache.register("usd_rub", usd_provider)

    # Alfabit USDT/RUB по цене витрины кабинета — источник для строки QR+KYC.
    # Курс тянется публичными эндпоинтами, но провайдер держит и подписанный
    # клиент как запасной путь, поэтому подключается только при наличии ключей;
    # без них строка QR+KYC просто не показывается.
    alfabit_client: AlfabitClient | None = None
    alfabit_payer_ip: str | None = None
    kyc_watcher: KycWatcher | None = None
    if config.alfabit_api_key and config.alfabit_secret_key:
        alfabit_client = AlfabitClient(
            api_key=config.alfabit_api_key,
            secret_key=config.alfabit_secret_key,
            base_url=config.alfabit_base_url,
        )
        rate_cache.register(
            "usdt_rub_alfabit",
            AlfabitWidgetProvider(
                alfabit_client,
                direction=config.alfabit_rate_direction,
                markup=config.alfabit_usdt_markup,
                base_url=config.alfabit_base_url,
            ),
        )
        logger.info("Alfabit provider registered (usdt_rub_alfabit)")
        # payer_ip для checkout: Telegram не отдаёт IP пользователя, alfabit
        # разрешил слать IP сервера бота. Из .env или автоопределением.
        alfabit_payer_ip = config.alfabit_payer_ip or await detect_public_ip()
        if alfabit_payer_ip:
            logger.info("Alfabit payer_ip: %s", alfabit_payer_ip)
        else:
            logger.warning("Alfabit payer_ip неизвестен — checkout будет недоступен")

        # Вебхука на смену KYC у Alfabit нет, поэтому заявки досматриваются
        # опросом. Без этого результат верификации ждал, пока пользователь сам
        # нажмёт кнопку в меню, — и до одного из клиентов не дошёл за двое суток.
        async def on_kyc_resolved(watch: Watch, payer: dict) -> None:
            await notify_kyc_resolved(bot, storage, watch, payer)

        kyc_watcher = KycWatcher(alfabit_client, on_kyc_resolved)
    else:
        logger.info("Alfabit keys not set — QR+KYC rate disabled")
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
    dp["manager_tg_chat_id"] = config.manager_tg_chat_id
    dp["alfabit_client"] = alfabit_client
    dp["alfabit_payer_ip"] = alfabit_payer_ip
    dp["kyc_watcher"] = kyc_watcher
    if config.manager_tg_chat_id is None:
        logger.warning("MANAGER_TG_CHAT_ID не задан — уведомления об оплате только в лог")

    # Строго до роутеров: групповые апдейты отбрасываются, не доходя до
    # маршрутизации. Иначе catch-all из handlers/fallback.py отвечает в любом
    # чате, куда бота занесло, а сценарий оплаты спрашивает телефон и паспорт
    # при свидетелях. Подробности — в middlewares/private_only.py.
    dp.update.outer_middleware(PrivateOnlyMiddleware())

    dp.include_router(start.router)
    dp.include_router(rates.router)
    dp.include_router(payment.router)
    dp.include_router(orders.router)
    dp.include_router(catalog.router)
    # Строго последним: ловит всё, что не подошло хендлерам выше.
    dp.include_router(fallback.router)
    # Последняя сеть: всё, что упало мимо except в хендлерах, попадает в лог с
    # user_id, а пользователь получает ответ вместо тишины.
    dp.errors.register(on_unhandled_error)

    logger.info("Loading initial data...")
    initial_tasks = [
        sheets_cache.refresh(),
        catalog_cache.refresh(),
        rate_cache.refresh("usdt_rub"),
        rate_cache.refresh("usd_rub"),
    ]
    if alfabit_client is not None:
        initial_tasks.append(rate_cache.refresh("usdt_rub_alfabit"))
    await asyncio.gather(*initial_tasks)

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
    if alfabit_client is not None:
        scheduler.add_job(
            rate_cache.refresh,
            trigger="interval",
            minutes=20,
            args=["usdt_rub_alfabit"],
        )
    if kyc_watcher is not None:
        scheduler.add_job(
            kyc_watcher.tick,
            trigger="interval",
            seconds=POLL_INTERVAL_SECONDS,
        )
    scheduler.start()

    logger.info("Starting bot (long polling)")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
