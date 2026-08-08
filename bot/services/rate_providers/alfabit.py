import logging
from decimal import Decimal

from services.alfabit.client import AlfabitClient

from .base import RateProvider

logger = logging.getLogger(__name__)


class AlfabitProvider(RateProvider):
    """USDT/RUB по «обменнику» Alfabit (converter/fiat/rate).

    Курс уже включает спред Alfabit. markup — дополнительная наценка сверху,
    по умолчанию +0.7% (ALFABIT_USDT_MARKUP). direction — buy: USDT покупаем мы
    (см. config). Цена юаня считается из этого курса делением на 6.67
    (services/yuan.py).
    """

    def __init__(
        self,
        client: AlfabitClient,
        direction: str = "buy",
        markup: Decimal = Decimal("1.007"),
    ):
        self._client = client
        self._direction = direction
        self._markup = markup

    async def get_rate(self) -> Decimal:
        rate = await self._client.converter_fiat_rate(
            crypto="USDT", fiat="RUB", direction=self._direction
        )
        return rate * self._markup
