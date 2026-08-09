"""Чтение листа Google Sheets в память.

Общая часть для листа заказов и листа каталога: оба читаются целиком по
таймеру и различаются только разбором строк. Сервис Google собирается на
каждое обновление заново и живёт внутри одного вызова — объекты
googleapiclient не потокобезопасны, а обновление уходит в исполнитель.
"""
import asyncio
import logging

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetReader:
    def __init__(self, spreadsheet_id: str, credentials_path: str, sheet_name: str):
        self._spreadsheet_id = spreadsheet_id
        self._credentials_path = credentials_path
        self._sheet_name = sheet_name
        self._rows: list[dict] = []

    # ---- переопределяется наследниками ------------------------------------

    def _parse_header(self, header: list[str]) -> list[str]:
        return header

    def _parse_row(self, record: dict) -> dict:
        return record

    # ----------------------------------------------------------------------

    def _fetch(self) -> list[list[str]]:
        creds = Credentials.from_service_account_file(self._credentials_path, scopes=_SCOPES)
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=self._sheet_name)
            .execute()
        )
        return result.get("values", [])

    def _sync_refresh(self) -> None:
        rows = self._fetch()
        if not rows:
            # Пустой ответ — почти наверняка сбой, а не опустевший лист.
            # Прежние данные не перезатираем.
            logger.warning("Sheet %s returned no rows", self._sheet_name)
            return

        headers = self._parse_header(rows[0])
        parsed = []
        for row in rows[1:]:
            padded = row + [""] * (len(headers) - len(row))
            parsed.append(self._parse_row(dict(zip(headers, padded))))

        self._rows = parsed
        logger.info("Sheet %s refreshed: %d rows", self._sheet_name, len(parsed))

    async def refresh(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._sync_refresh)
        except Exception:
            logger.exception(
                "Sheet %s refresh failed, keeping previous data", self._sheet_name
            )
