"""Лист заказов: поиск по телефону для «Статуса заказа»."""
from datetime import datetime, timedelta

from services.sheet_reader import SheetReader
from utils.phone import normalize_phone

_SERIAL_DATE_EPOCH = datetime(1899, 12, 30)


def _parse_date(value: object) -> str:
    if not value:
        return ""
    try:
        serial = int(float(str(value)))
        return (_SERIAL_DATE_EPOCH + timedelta(days=serial)).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return str(value)


class SheetsCache(SheetReader):
    def __init__(self, spreadsheet_id: str, credentials_path: str, sheet_name: str = "Orders"):
        super().__init__(spreadsheet_id, credentials_path, sheet_name)

    def _parse_row(self, record: dict) -> dict:
        # Дата хранится серийным числом Excel/Sheets — приводим к ДД.ММ.ГГГГ.
        if "дата" in record:
            record["дата"] = _parse_date(record["дата"])
        return record

    def find_orders_by_phone(self, phone: str) -> list[dict]:
        """Все заказы по номеру: один телефон может встречаться в нескольких
        строках, номера заказа в таблице нет."""
        target = normalize_phone(phone)
        return [
            order for order in self._rows
            if normalize_phone(order.get("Телефон", "")) == target
        ]
