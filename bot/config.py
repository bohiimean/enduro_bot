from dataclasses import dataclass
from decimal import Decimal
from dotenv import load_dotenv
import os

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    google_sheet_id: str
    google_credentials_path: str
    google_sheet_name: str
    google_catalog_sheet_name: str
    rapira_usdt_markup: Decimal
    twelvedata_api_key: str | None
    chrome_binary_path: str | None
    sheets_refresh_minutes: int
    manager_tg_username: str
    manager_tg_chat_id: int | None
    alfabit_api_key: str | None
    alfabit_secret_key: str | None
    alfabit_base_url: str
    alfabit_rate_direction: str
    alfabit_usdt_markup: Decimal
    alfabit_payer_ip: str | None


def load_config() -> Config:
    bot_token = os.environ["BOT_TOKEN"]
    google_sheet_id = os.environ["GOOGLE_SHEET_ID"]
    google_credentials_path = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials.json")
    google_sheet_name = os.environ.get("GOOGLE_SHEET_NAME", "Orders")
    google_catalog_sheet_name = os.environ.get("GOOGLE_CATALOG_SHEET_NAME", "Лист2")
    rapira_usdt_markup = Decimal(os.environ.get("RAPIRA_USDT_MARKUP", "1.045"))
    twelvedata_api_key = os.environ.get("TWELVEDATA_API_KEY") or None
    chrome_binary_path = os.environ.get("CHROME_BINARY_PATH") or None
    sheets_refresh_minutes = int(os.environ.get("SHEETS_REFRESH_MINUTES", "10"))
    manager_tg_username = os.environ.get("MANAGER_TG_USERNAME", "")
    # Уведомления об оплате. Бот не может писать по юзернейму — нужен chat_id,
    # и менеджер должен хотя бы раз нажать /start у бота.
    raw_manager_chat_id = os.environ.get("MANAGER_TG_CHAT_ID", "").strip()
    manager_tg_chat_id = int(raw_manager_chat_id) if raw_manager_chat_id else None
    alfabit_api_key = os.environ.get("ALFABIT_API_KEY") or None
    alfabit_secret_key = os.environ.get("ALFABIT_SECRET_KEY") or None
    alfabit_base_url = os.environ.get("ALFABIT_BASE_URL", "https://alfabit.org")
    # buy: рубли от клиента мы конвертируем в USDT, то есть покупаем его —
    # котировка нужна с той стороны, по которой платим мы. На sell курс ниже
    # примерно на 2%, это цена продажи USDT, не наша.
    alfabit_rate_direction = os.environ.get("ALFABIT_RATE_DIRECTION", "buy")
    # +0.7% сверх курса витрины alfabit (спред конвертера + комиссия метода
    # оплаты уже внутри курса) — цена юаня по QR+KYC = курс/6.63 × 1.007.
    # Дефолт не 1.0 намеренно: если переменную забыть в .env на сервере,
    # наценка не должна молча пропасть.
    alfabit_usdt_markup = Decimal(os.environ.get("ALFABIT_USDT_MARKUP", "1.007"))
    # Telegram не отдаёт IP пользователя, а checkout требует payer_ip.
    # Поддержка alfabit: слать IP сервера, на котором работает бот.
    # Пусто — определяем публичный IP автоматически при старте (services/public_ip.py).
    alfabit_payer_ip = os.environ.get("ALFABIT_PAYER_IP") or None

    return Config(
        bot_token=bot_token,
        google_sheet_id=google_sheet_id,
        google_credentials_path=google_credentials_path,
        google_sheet_name=google_sheet_name,
        google_catalog_sheet_name=google_catalog_sheet_name,
        rapira_usdt_markup=rapira_usdt_markup,
        twelvedata_api_key=twelvedata_api_key,
        chrome_binary_path=chrome_binary_path,
        sheets_refresh_minutes=sheets_refresh_minutes,
        manager_tg_username=manager_tg_username,
        manager_tg_chat_id=manager_tg_chat_id,
        alfabit_api_key=alfabit_api_key,
        alfabit_secret_key=alfabit_secret_key,
        alfabit_base_url=alfabit_base_url,
        alfabit_rate_direction=alfabit_rate_direction,
        alfabit_usdt_markup=alfabit_usdt_markup,
        alfabit_payer_ip=alfabit_payer_ip,
    )
