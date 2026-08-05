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
    alfabit_api_key: str | None
    alfabit_secret_key: str | None
    alfabit_base_url: str
    alfabit_rate_direction: str
    alfabit_usdt_markup: Decimal
    alfabit_payer_ip_fallback: str


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
    alfabit_api_key = os.environ.get("ALFABIT_API_KEY") or None
    alfabit_secret_key = os.environ.get("ALFABIT_SECRET_KEY") or None
    alfabit_base_url = os.environ.get("ALFABIT_BASE_URL", "https://alfabit.org")
    alfabit_rate_direction = os.environ.get("ALFABIT_RATE_DIRECTION", "sell")
    alfabit_usdt_markup = Decimal(os.environ.get("ALFABIT_USDT_MARKUP", "1.0"))
    # У Telegram-бота нет реального IP плательщика, а checkout требует payer_ip.
    # Временная заглушка до выяснения у alfabit, что слать (см. OPEN QUESTIONS).
    alfabit_payer_ip_fallback = os.environ.get("ALFABIT_PAYER_IP_FALLBACK", "")

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
        alfabit_api_key=alfabit_api_key,
        alfabit_secret_key=alfabit_secret_key,
        alfabit_base_url=alfabit_base_url,
        alfabit_rate_direction=alfabit_rate_direction,
        alfabit_usdt_markup=alfabit_usdt_markup,
        alfabit_payer_ip_fallback=alfabit_payer_ip_fallback,
    )
