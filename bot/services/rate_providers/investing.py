import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from seleniumbase import SB

from .base import RateProvider

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1)
_SELECTOR = '[data-test="instrument-price-last"]'
_URL = "https://www.investing.com/currencies/usd-rub"


_CONSENT_SELECTOR = "#onetrust-accept-btn-handler"


def _fetch_sync(chrome_binary: str | None) -> Decimal:
    kwargs: dict = dict(
        uc=True,
        headless=True,
        chromium_arg="--no-sandbox --disable-dev-shm-usage",
    )
    if chrome_binary:
        kwargs["binary_location"] = chrome_binary
    with SB(**kwargs) as sb:
        sb.open(_URL)
        try:
            sb.click(_CONSENT_SELECTOR, timeout=8)
            logger.info("InvestingCom: consent popup accepted")
        except Exception:
            pass
        sb.wait_for_element(_SELECTOR, timeout=15)
        text = sb.get_text(_SELECTOR).strip().replace(",", ".")
        rate = Decimal(text)
        logger.info("InvestingCom USD/RUB fetched: %s", rate)
        return rate


class InvestingComProvider(RateProvider):
    def __init__(self, chrome_binary: str | None = None):
        self._chrome_binary = chrome_binary

    async def get_rate(self) -> Decimal:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, _fetch_sync, self._chrome_binary)
