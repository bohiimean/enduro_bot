import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import psutil
from selenium.common.exceptions import NoSuchWindowException
from seleniumbase import SB

from .base import RateProvider

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1)
_SELECTOR = '[data-test="instrument-price-last"]'
_URL = "https://www.investing.com/currencies/usd-rub"
_SCREENSHOT_PATH = "/tmp/investing_fail.png"

_POPUP_SELECTORS = [
    "#onetrust-accept-btn-handler",
    ".popupCloseIcon",
    "[data-test='modal-close']",
    ".modal-close-button",
    "button[aria-label='Close']",
    "#PromoteSignUpPopUp .close",
]


def _sys_stats() -> str:
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=None)
    return f"CPU={cpu:.0f}% RAM={mem.used // 1024 // 1024}MB/{mem.total // 1024 // 1024}MB"


class InvestingComProvider(RateProvider):
    def __init__(self, chrome_binary: str | None = None):
        self._chrome_binary = chrome_binary
        self._cm = None
        self._sb: SB | None = None
        self._lock = threading.Lock()

    def _start(self) -> None:
        logger.info("InvestingCom: starting browser [%s]", _sys_stats())
        t0 = time.monotonic()
        kwargs: dict = dict(
            uc=True,
            headless=True,
            chromium_arg="--no-sandbox --disable-dev-shm-usage",
        )
        if self._chrome_binary:
            kwargs["binary_location"] = self._chrome_binary
        self._cm = SB(**kwargs)
        self._sb = self._cm.__enter__()
        logger.info(
            "InvestingCom: browser started in %.1fs [%s]",
            time.monotonic() - t0,
            _sys_stats(),
        )

    def _alive(self) -> bool:
        if self._sb is None:
            return False
        try:
            _ = self._sb.get_current_url()
            return True
        except Exception:
            return False

    def _dismiss_popups(self) -> None:
        for sel in _POPUP_SELECTORS:
            try:
                self._sb.click(sel, timeout=3)
                logger.info("InvestingCom: dismissed popup [%s]", sel)
                return
            except Exception:
                pass

    def _screenshot(self) -> None:
        try:
            self._sb.save_screenshot(_SCREENSHOT_PATH)
            logger.info("InvestingCom: debug screenshot → %s", _SCREENSHOT_PATH)
        except Exception:
            pass

    def _reset(self) -> None:
        try:
            if self._cm is not None:
                self._cm.__exit__(None, None, None)
        except Exception:
            logger.exception("InvestingCom: teardown failed")
        finally:
            self._cm = None
            self._sb = None
            logger.info("InvestingCom: browser reset [%s]", _sys_stats())

    def _fetch_once(self, retry: bool = True) -> Decimal:
        if not self._alive():
            self._reset()
            self._start()

        t0 = time.monotonic()
        try:
            self._sb.open(_URL)
            self._dismiss_popups()
            self._sb.wait_for_element(_SELECTOR, timeout=20)
            text = self._sb.get_text(_SELECTOR).strip().replace(",", ".")
            rate = Decimal(text)
            logger.info(
                "InvestingCom: USD/RUB=%s in %.1fs [%s]",
                rate,
                time.monotonic() - t0,
                _sys_stats(),
            )
            return rate
        except NoSuchWindowException:
            logger.warning(
                "InvestingCom: window closed unexpectedly [%s]%s",
                _sys_stats(),
                ", restarting" if retry else ", giving up",
            )
            self._reset()
            if retry:
                return self._fetch_once(retry=False)
            raise
        except Exception:
            self._screenshot()
            logger.warning(
                "InvestingCom: fetch failed after %.1fs [%s], resetting browser",
                time.monotonic() - t0,
                _sys_stats(),
            )
            self._reset()
            if retry:
                return self._fetch_once(retry=False)
            raise

    def _fetch_sync(self) -> Decimal:
        with self._lock:
            return self._fetch_once()

    async def get_rate(self) -> Decimal:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, self._fetch_sync)