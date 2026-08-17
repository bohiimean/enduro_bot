import logging
from decimal import Decimal
from typing import Any

import aiohttp

from services.alfabit.client import AlfabitClient

from .base import RateProvider

logger = logging.getLogger(__name__)

# Публичный API сайта alfabit — тот, которым пользуется виджет «Купить USDT»
# в кабинете. Ни ключа, ни подписи не требует. Префикс именно такой: под
# /api/v1 (без /wallet-web) отдаётся HTML одностраничника, а не JSON.
_PUBLIC_PREFIX = "/wallet-web/api/v1"
# Метод оплаты «QR» в списке банков. Комиссия у всех методов сейчас одинаковая
# (1.80%), но берём именно свой код — если Alfabit разведёт тарифы по методам,
# цена поедет за нужным.
_QR_METHOD_CODE = "SBPRUB"
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class AlfabitWidgetProvider(RateProvider):
    """USDT/RUB по цене витрины Alfabit — виджет «Купить USDT», метод оплаты QR.

    Зачем не подписанный /integration/converter/fiat/rate: он отдаёт
    rate_effective — базовый курс конвертера (08.08.2026: 84.13). В кабинете
    поверх него начисляется комиссия метода оплаты (QR: 1.80%), и покупатель
    видит 85.64. Юань мы продаём по цене витрины, значит и считать надо от неё —
    иначе бот постоянно дешевле кабинета на 1.8%, что больше всей наценки.

    Проверено 08.08.2026 на боевых данных:
        rate_effective 84.1304 × 1.018 = 85.6447 против 85.645467 на экране
        кабинета (QR). Расхождение 0.0007 ₽ — дрожание базы между запросами.
    S-Pay на скрине ложился на 84.1304/(1-0.018); разница между «×(1+f)» и
    «/(1-f)» — 0.03%, на цене юаня в копейках не видна. Взят вариант ×(1+f),
    он совпал именно с QR — нашим методом.

    Комиссия НЕ зашита в код: читается из /public/buy-sell/banks/{fiat} при
    каждом обновлении. Поменяют 1.80 на 1.60 — бот поедет за кабинетом сам.

    Важно: оба публичных эндпоинта недокументированы (это внутренний API
    сайта, не integration API). Могут смениться без предупреждения, поэтому
    предусмотрены запасные пути — см. get_rate.
    """

    def __init__(
        self,
        client: AlfabitClient,
        direction: str = "buy",
        markup: Decimal = Decimal("1.007"),
        base_url: str = "https://alfabit.org",
        fiat: str = "RUB",
        crypto: str = "USDT",
        method_code: str = _QR_METHOD_CODE,
    ):
        self._client = client
        self._direction = direction
        self._markup = markup
        self._base = base_url.rstrip("/") + _PUBLIC_PREFIX
        self._fiat = fiat
        self._crypto = crypto
        self._method_code = method_code
        # Последняя успешно прочитанная комиссия метода. Нужна, если список
        # банков временно недоступен: считать без комиссии нельзя — это тихо
        # уронило бы цену на 1.8%, ровно та ошибка, ради которой всё делалось.
        self._last_fee_percent: Decimal | None = None

    async def _get_json(self, session: aiohttp.ClientSession, path: str, **params: Any) -> Any:
        async with session.get(self._base + path, params=params or None) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _base_rate(self, session: aiohttp.ClientSession) -> Decimal:
        """rate_effective — курс конвертера со спредом, без комиссии метода."""
        try:
            data = await self._get_json(
                session,
                "/public/fiat-converter/rate",
                crypto_symbol=self._crypto,
                fiat_code=self._fiat,
                direction=self._direction,
            )
            return Decimal(str(data["rate_effective"]))
        except Exception:
            # Публичный эндпоинт лёг — тот же курс доступен по подписанному
            # integration API (проверено: цифра совпадает до знака).
            logger.warning(
                "Alfabit: публичный fiat-converter/rate недоступен, "
                "берём курс по подписанному API",
                exc_info=True,
            )
            return await self._client.converter_fiat_rate(
                crypto=self._crypto, fiat=self._fiat, direction=self._direction
            )

    async def _fee_percent(self, session: aiohttp.ClientSession) -> Decimal:
        """Комиссия метода оплаты в процентах (QR → deposit_fee_percent)."""
        try:
            payload = await self._get_json(session, f"/public/buy-sell/banks/{self._fiat}")
            banks = payload["data"] if isinstance(payload, dict) else payload
            for bank in banks:
                if bank.get("bestchange_code") == self._method_code:
                    fee = Decimal(str(bank["deposit_fee_percent"]))
                    if fee != self._last_fee_percent:
                        logger.info(
                            "Alfabit: комиссия метода %s = %s%%", self._method_code, fee
                        )
                    self._last_fee_percent = fee
                    return fee
            raise LookupError(f"метод {self._method_code} не найден в списке банков")
        except Exception:
            if self._last_fee_percent is None:
                # Без комиссии курс занижен на ~1.8% — лучше не отдавать ничего.
                # RateCache сохранит предыдущее значение, а на холодном старте
                # строка QR+KYC просто не покажется.
                raise
            logger.warning(
                "Alfabit: список банков недоступен, берём последнюю известную "
                "комиссию %s%%",
                self._last_fee_percent,
                exc_info=True,
            )
            return self._last_fee_percent

    async def get_rate(self) -> Decimal:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            base = await self._base_rate(session)
            fee = await self._fee_percent(session)
        showcase = base * (1 + fee / 100)
        logger.info(
            "Alfabit витрина: база %s + %s%% = %s (×%s = %s)",
            base, fee, showcase, self._markup, showcase * self._markup,
        )
        return showcase * self._markup
