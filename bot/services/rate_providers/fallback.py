import logging
from decimal import Decimal

from .base import RateProvider

logger = logging.getLogger(__name__)


class FallbackProvider(RateProvider):
    def __init__(self, primary: RateProvider, secondary: RateProvider):
        self._primary = primary
        self._secondary = secondary

    async def get_rate(self) -> Decimal:
        try:
            return await self._primary.get_rate()
        except Exception:
            logger.warning("Primary USD provider failed, switching to fallback", exc_info=True)
            return await self._secondary.get_rate()
