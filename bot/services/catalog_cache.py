import asyncio
import logging

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class CatalogCache:
    def __init__(self, spreadsheet_id: str, credentials_path: str, sheet_name: str = "Лист2"):
        self._spreadsheet_id = spreadsheet_id
        self._credentials_path = credentials_path
        self._sheet_name = sheet_name
        self._items: list[dict] = []

    def _sync_refresh(self) -> None:
        creds = Credentials.from_service_account_file(self._credentials_path, scopes=_SCOPES)
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=self._sheet_name)
            .execute()
        )
        rows = result.get("values", [])
        if not rows:
            logger.warning("Catalog sheet %s returned no rows", self._sheet_name)
            return
        headers = [h.lower() for h in rows[0]]
        items = []
        for row in rows[1:]:
            padded = row + [""] * (len(headers) - len(row))
            items.append(dict(zip(headers, padded)))
        self._items = items
        logger.info("Catalog refreshed: %d items", len(items))

    async def refresh(self) -> None:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._sync_refresh)
        except Exception:
            logger.exception("Catalog sheet refresh failed, keeping previous data")

    def all_items(self) -> list[dict]:
        return self._items

    def get_item(self, idx: int) -> dict | None:
        if 0 <= idx < len(self._items):
            return self._items[idx]
        return None
