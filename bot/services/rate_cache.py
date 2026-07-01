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
    def __init__(self) -> None:
        self._store: dict[str, CacheEntry] = {}
        self._providers: dict[str, RateProvider] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def register(self, key: str, provider: RateProvider) -> None:
        self._providers[key] = provider
        self._locks[key] = asyncio.Lock()

    async def refresh(self, key: str, max_age_seconds: float | None = None) -> None:
        lock = self._locks[key]
        async with lock:
            if max_age_seconds is not None:
                cached = self._store.get(key)
                if cached and time.time() - cached.fetched_at < max_age_seconds:
                    return
            provider = self._providers[key]
            try:
                rate = await provider.get_rate()
            except Exception:
                logger.exception("Failed to refresh rate for key=%s", key)
                return
            self._store[key] = CacheEntry(rate=rate, fetched_at=time.time())
            logger.info("Rate refreshed: %s=%s", key, rate)

    def get(self, key: str) -> CacheEntry | None:
        return self._store.get(key)
