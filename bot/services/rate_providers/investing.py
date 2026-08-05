import asyncio
import logging
import os
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

# Один зависший Selenium-вызов навсегда занимает единственный поток пула и
# threading.Lock, из-за чего usd_rub переставал обновляться до рестарта контейнера.
# _BROWSER_HARD_TIMEOUT — сколько ждём одну попытку, прежде чем убить chrome
# (это заставляет зависший вызов упасть и освободить поток → само-восстановление).
# _GET_RATE_TIMEOUT — общий предел на async-стороне (покрывает обе попытки).
# Нормальный скрап investing.com долгий: первый ~3 мин, последующие ~2 мин.
# Порог watchdog должен быть заметно выше нормы, иначе будет резать здоровые скрапы.
_BROWSER_HARD_TIMEOUT = float(os.environ.get("INVESTING_BROWSER_TIMEOUT", "420"))
_GET_RATE_TIMEOUT = float(os.environ.get("INVESTING_GET_RATE_TIMEOUT", "900"))

_POPUP_SELECTORS = [
    "#onetrust-accept-btn-handler",
    ".popupCloseIcon",
    "[data-test='modal-close']",
    ".modal-close-button",
    "button[aria-label='Close']",
    "#PromoteSignUpPopUp .close",
]

_CHROMIUM_ARGS = (
    "--no-sandbox --disable-dev-shm-usage --disable-gpu "
    "--disable-extensions --blink-settings=imagesEnabled=false"
)


def _sys_stats() -> str:
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=None)
    return f"CPU={cpu:.0f}% RAM={mem.used // 1024 // 1024}MB/{mem.total // 1024 // 1024}MB"


def _kill_orphans() -> None:
    try:
        me_pid = os.getpid()
        seen: set[int] = set()
        targets = []

        def _consider(proc) -> None:
            try:
                if proc.pid in (me_pid, 1) or proc.pid in seen:
                    return
                name = proc.name().lower()
                if "chrome" in name or "chromium" in name or "driver" in name:
                    seen.add(proc.pid)
                    targets.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        for child in psutil.Process(me_pid).children(recursive=True):
            _consider(child)
        # UC/CDP-режим часто отвязывает Chrome от родителя (reparent к pid 1),
        # тогда children() его не видит — добираем сканом всех процессов.
        # Контейнер выделенный, Chrome в нём только наш, так что это безопасно.
        for proc in psutil.process_iter(["name"]):
            _consider(proc)

        if not targets:
            return
        for p in targets:
            try:
                p.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(targets, timeout=3)
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        logger.info("InvestingCom: cleaned %d chrome/driver process(es)", len(targets))
    except Exception:
        logger.exception("InvestingCom: kill_orphans failed")


class _KillWatchdog:
    """Убивает chrome/driver, если операция в браузере идёт дольше `timeout` секунд.

    Зависший Selenium-вызов иначе навсегда занимает единственный поток пула
    и threading.Lock. Убийство chrome заставляет вызов упасть с исключением,
    поток раскручивается до finally и освобождается — система лечит себя сама.
    """

    def __init__(self, timeout: float, label: str = ""):
        self._timeout = timeout
        self._label = label
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.fired = False

    def _run(self) -> None:
        if not self._done.wait(self._timeout):
            self.fired = True
            logger.warning(
                "InvestingCom: hard timeout %.0fs exceeded%s, killing chrome to recover [%s]",
                self._timeout,
                f" ({self._label})" if self._label else "",
                _sys_stats(),
            )
            _kill_orphans()

    def __enter__(self) -> "_KillWatchdog":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._done.set()
        self._thread.join(timeout=5)


class InvestingComProvider(RateProvider):
    def __init__(self, chrome_binary: str | None = None):
        self._chrome_binary = chrome_binary
        self._lock = threading.Lock()

    def _sb_kwargs(self) -> dict:
        kwargs: dict = dict(uc=True, headless=True, chromium_arg=_CHROMIUM_ARGS)
        if self._chrome_binary:
            kwargs["binary_location"] = self._chrome_binary
        return kwargs

    @staticmethod
    def _dismiss_popups(sb) -> None:
        for sel in _POPUP_SELECTORS:
            try:
                sb.click(sel, timeout=3)
                logger.info("InvestingCom: dismissed popup [%s]", sel)
                return
            except Exception:
                pass

    @staticmethod
    def _screenshot(sb) -> None:
        try:
            sb.save_screenshot(_SCREENSHOT_PATH)
            logger.info("InvestingCom: debug screenshot → %s", _SCREENSHOT_PATH)
        except Exception:
            pass

    def _fetch_once(self, retry: bool = True) -> Decimal:
        t0 = time.monotonic()
        logger.info("InvestingCom: starting browser [%s]", _sys_stats())
        try:
            with _KillWatchdog(_BROWSER_HARD_TIMEOUT, "fetch"), SB(**self._sb_kwargs()) as sb:
                logger.info(
                    "InvestingCom: browser started in %.1fs [%s]",
                    time.monotonic() - t0,
                    _sys_stats(),
                )
                try:
                    sb.open(_URL)
                    self._dismiss_popups(sb)
                    sb.wait_for_element(_SELECTOR, timeout=20)
                    text = sb.get_text(_SELECTOR).strip().replace(",", ".")
                    rate = Decimal(text)
                    logger.info(
                        "InvestingCom: USD/RUB=%s in %.1fs [%s]",
                        rate,
                        time.monotonic() - t0,
                        _sys_stats(),
                    )
                    return rate
                except Exception:
                    self._screenshot(sb)
                    raise
        except NoSuchWindowException:
            logger.warning(
                "InvestingCom: window closed unexpectedly [%s]%s",
                _sys_stats(),
                ", retrying" if retry else ", giving up",
            )
            if retry:
                return self._fetch_once(retry=False)
            raise
        except Exception:
            logger.warning(
                "InvestingCom: fetch failed after %.1fs [%s]",
                time.monotonic() - t0,
                _sys_stats(),
            )
            if retry:
                return self._fetch_once(retry=False)
            raise
        finally:
            _kill_orphans()

    def _fetch_sync(self) -> Decimal:
        with self._lock:
            return self._fetch_once()

    async def get_rate(self) -> Decimal:
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(_executor, self._fetch_sync),
                timeout=_GET_RATE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            # Поток пула ещё занят зависшим вызовом — форсим kill, чтобы watchdog
            # не ждал, и пробрасываем ошибку (FallbackProvider уйдёт на TwelveData).
            logger.error(
                "InvestingCom: get_rate exceeded %.0fs, aborting and killing chrome",
                _GET_RATE_TIMEOUT,
            )
            _kill_orphans()
            raise
