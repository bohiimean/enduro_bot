import asyncio
import logging
from datetime import datetime, timedelta

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from utils.phone import normalize_phone

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_SERIAL_DATE_EPOCH = datetime(1899, 12, 30)


def _parse_date(value: object) -> str:
    if not value:
        return ""
    try:
        serial = int(float(str(value)))
        return (_SERIAL_DATE_EPOCH + timedelta(days=serial)).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return str(value)


def to_catalog_card(item: dict) -> dict:
    return {
        "Марка": item.get("Марка", ""),
        "Модель": item.get("Модель", ""),
        "Цвет": item.get("Цвет", ""),
        "Цена": item.get("Цена", ""),
    }


class SheetsCache:
    def __init__(self, spreadsheet_id: str, credentials_path: str, sheet_name: str = "Orders"):
        self._spreadsheet_id = spreadsheet_id
        self._credentials_path = credentials_path
        self._sheet_name = sheet_name
        self._orders: list[dict] = []

    def _sync_refresh(self) -> None:
        creds = Credentials.from_service_account_file(
            self._credentials_path, scopes=_SCOPES
        )
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=self._sheet_name)
            .execute()
        )
        rows = result.get("values", [])
        if not rows:
            logger.warning("Sheet %s returned no rows", self._sheet_name)
            return

        headers = rows[0]
        orders = []
        for row in rows[1:]:
            padded = row + [""] * (len(headers) - len(row))
            record = dict(zip(headers, padded))
            if "дата" in record:
                record["дата"] = _parse_date(record["дата"])
            orders.append(record)

        self._orders = orders
        logger.info("Sheets refreshed: %d rows", len(orders))

    async def refresh(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._sync_refresh)
        except Exception:
            logger.exception("Google Sheets refresh failed, keeping previous data")

    def find_orders_by_phone(self, phone: str) -> list[dict]:
        target = normalize_phone(phone)
        return [
            o for o in self._orders
            if normalize_phone(o.get("Телефон", "")) == target
        ]

    def available_items(self) -> list[dict]:
        return [o for o in self._orders if o.get("Статус") == "в наличии"]
