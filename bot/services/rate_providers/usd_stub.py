from decimal import Decimal

from .base import RateProvider


class UsdRubStubProvider(RateProvider):
    """Источник USD/RUB не определён клиентом. Заменить, когда решится."""

    async def get_rate(self) -> Decimal:
        return Decimal("0")
