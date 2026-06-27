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
    # populated when tiers are shown (weekends always, weekdays only before noon)
    discount_tiers: list[MarkupTier]
    # True when discount_tiers are currently effective (vs. shown as "available before noon")
    discount_active: bool
    is_weekday: bool


def get_usd_rate_info(now: datetime) -> UsdRateInfo:
    moscow = now.astimezone(MOSCOW_TZ)
    is_weekend = moscow.weekday() >= 5
    is_before_noon = moscow.hour < 12

    if is_weekend:
        return UsdRateInfo(
            standard=MarkupTier("Стандарт", Decimal("3.0")),
            discount_tiers=[
                MarkupTier("от 300 000 ₽", Decimal("2.7")),
                MarkupTier("от 500 000 ₽", Decimal("2.5")),
            ],
            discount_active=True,
            is_weekday=False,
        )

    tiers = (
        [
            MarkupTier("от 300 000 ₽", Decimal("2.1")),
            MarkupTier("от 500 000 ₽", Decimal("1.8")),
            MarkupTier("от 1 000 000 ₽", Decimal("1.6")),
        ]
        if is_before_noon
        else []
    )
    return UsdRateInfo(
        standard=MarkupTier("Стандарт", Decimal("2.5")),
        discount_tiers=tiers,
        discount_active=is_before_noon,
        is_weekday=True,
    )
