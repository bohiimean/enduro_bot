from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


@dataclass
class MarkupTier:
    label: str
    markup: Decimal


@dataclass
class UsdRateInfo:
    standard: MarkupTier
    discount_tiers: list[MarkupTier]


def get_usd_rate_info(now: datetime) -> UsdRateInfo:
    moscow = now.astimezone(MOSCOW_TZ)
    is_weekend = moscow.weekday() >= 5

    if is_weekend:
        return UsdRateInfo(
            standard=MarkupTier("Стандарт", Decimal("2.7")),
            discount_tiers=[
                MarkupTier("от 300 000 ₽", Decimal("2.5")),
                MarkupTier("от 500 000 ₽", Decimal("2.1")),
            ],
        )

    return UsdRateInfo(
        standard=MarkupTier("Стандарт", Decimal("2.6")),
        discount_tiers=[
            MarkupTier("от 300 000 ₽", Decimal("2.1")),
            MarkupTier("от 500 000 ₽", Decimal("1.7")),
            MarkupTier("от 1 000 000 ₽", Decimal("1.5")),
        ],
    )
