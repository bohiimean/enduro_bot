import asyncio
import io
import logging
import re

from aiogram.types import BufferedInputFile
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def _extract_file_id(url: str) -> str:
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    raise ValueError(f"Cannot extract Drive file ID from: {url}")


class DrivePhotoCache:
    def __init__(self, credentials_path: str):
        self._credentials_path = credentials_path
        self._cache: dict[str, str] = {}  # drive_file_id -> telegram file_id

    def _download(self, file_id: str) -> bytes:
        creds = Credentials.from_service_account_file(self._credentials_path, scopes=_SCOPES)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        request = service.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()

    async def get_photo(self, url: str) -> str | BufferedInputFile:
        """Returns cached Telegram file_id or BufferedInputFile for first upload."""
        drive_id = _extract_file_id(url)
        if drive_id in self._cache:
            return self._cache[drive_id]
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._download, drive_id)
        return BufferedInputFile(data, filename=f"{drive_id}.jpg")

    def store_file_id(self, url: str, telegram_file_id: str) -> None:
        drive_id = _extract_file_id(url)
        self._cache[drive_id] = telegram_file_id
