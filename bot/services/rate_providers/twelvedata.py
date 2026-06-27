import logging
from decimal import Decimal

import aiohttp

from .base import RateProvider

logger = logging.getLogger(__name__)

_API_URL = "https://api.twelvedata.com/exchange_rate"
_SYMBOL = "USD/RUB"


class TwelveDataProvider(RateProvider):
    def __init__(self, api_key: str):
        self._api_key = api_key

    async def get_rate(self) -> Decimal:
        params = {"symbol": _SYMBOL, "apikey": self._api_key}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        if data.get("status") == "error":
            raise ValueError(f"TwelveData error: {data.get('message')}")

        return Decimal(str(data["rate"]))
