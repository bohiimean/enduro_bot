"""Лист каталога — отдельный от заказов, со своими колонками
(название / описание / цена / фото). Персональных данных в нём нет.
"""
from services.sheet_reader import SheetReader


class CatalogCache(SheetReader):
    def __init__(self, spreadsheet_id: str, credentials_path: str, sheet_name: str = "Лист2"):
        super().__init__(spreadsheet_id, credentials_path, sheet_name)

    def _parse_header(self, header: list[str]) -> list[str]:
        # Ключи в нижнем регистре: в таблице заголовки пишут по-разному.
        return [h.lower() for h in header]

    def all_items(self) -> list[dict]:
        return self._rows

    def get_item(self, idx: int) -> dict | None:
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None
