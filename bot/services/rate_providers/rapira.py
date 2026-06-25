import logging
from decimal import Decimal

import aiohttp

from .base import RateProvider

logger = logging.getLogger(__name__)

_API_URL = "https://api.rapira.net/open/market/rates"
_SYMBOL = "USDT/RUB"


class RapiraProvider(RateProvider):
    def __init__(self, markup: Decimal = Decimal("1.045")):
        self._markup = markup

    async def get_rate(self) -> Decimal:
        async with aiohttp.ClientSession() as session:
            async with session.get(_API_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                data = await resp.json()

        pairs = data.get("data", [])
        pair = next((p for p in pairs if p.get("symbol") == _SYMBOL), None)
        if pair is None:
            raise ValueError(f"Pair {_SYMBOL} not found in Rapira response")

        raw = pair.get("close") or (
            (Decimal(str(pair["bidPrice"])) + Decimal(str(pair["askPrice"]))) / 2
        )
        return Decimal(str(raw)) * self._markup
