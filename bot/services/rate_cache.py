import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from services.rate_providers.base import RateProvider

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    rate: Decimal
    fetched_at: float


class RateCache:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._store: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, provider: RateProvider) -> CacheEntry:
        async with self._lock:
            entry = self._store.get(key)
            if entry and time.time() - entry.fetched_at < self._ttl:
                return entry
            try:
                rate = await provider.get_rate()
                entry = CacheEntry(rate=rate, fetched_at=time.time())
                self._store[key] = entry
                return entry
            except Exception:
                logger.exception("Failed to refresh rate for key=%s", key)
                if entry:
                    # возвращаем устаревшее значение, не роняем хендлер
                    return entry
                raise
