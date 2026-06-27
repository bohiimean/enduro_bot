import time
from datetime import datetime, timezone
from decimal import Decimal

from aiogram import F, Router
from aiogram.types import Message

from services.rate_cache import RateCache
from services.rate_providers.base import RateProvider
from services.rate_providers.rapira import RapiraProvider
from services.usd_markup import MOSCOW_TZ, get_usd_rate_info

router = Router()

_rapira = RapiraProvider()
_CACHE_KEY_USDT = "usdt_rub"
_CACHE_KEY_USD = "usd_rub"
_TTL_STALE_WARN = 600

_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _fmt(rate: Decimal) -> str:
    return f"{rate:.2f}"


@router.message(F.text == "💱 Курс валют")
async def cmd_rates(message: Message, rate_cache: RateCache, usd_provider: RateProvider) -> None:
    now = datetime.now(tz=timezone.utc)
    moscow = now.astimezone(MOSCOW_TZ)

    usdt_entry = await rate_cache.get(_CACHE_KEY_USDT, _rapira)
    usd_entry = await rate_cache.get(_CACHE_KEY_USD, usd_provider)

    usd_info = get_usd_rate_info(now)
    base_usd = usd_entry.rate

    day = _WEEKDAYS[moscow.weekday()]
    date_str = moscow.strftime("%d.%m")
    time_str = moscow.strftime("%H:%M")

    lines = [
        f"💱 <b>Курс на {date_str} ({day}, {time_str})</b>",
        "",
        "QR-оплата (USDT/RUB):",
        f"→ {_fmt(usdt_entry.rate)} ₽",
        "",
        "Наличные в Москве (USD/RUB):",
        f"→ {usd_info.standard.label}: {_fmt(base_usd + usd_info.standard.markup)} ₽",
    ]

    if usd_info.discount_tiers:
        if usd_info.is_weekday:
            lines.append("")
            lines.append("Скидка до 12:00:")
        for tier in usd_info.discount_tiers:
            lines.append(f"→ {tier.label} — {_fmt(base_usd + tier.markup)} ₽")
    elif usd_info.is_weekday and not usd_info.discount_active:
        lines.append("")
        lines.append("Скидка на крупные суммы действует до 12:00 по московскому времени.")

    stale = []
    if time.time() - usdt_entry.fetched_at > _TTL_STALE_WARN:
        t = datetime.fromtimestamp(usdt_entry.fetched_at, tz=MOSCOW_TZ).strftime("%H:%M")
        stale.append(f"⚠️ USDT/RUB: данные на {t}, не удалось обновить")
    if time.time() - usd_entry.fetched_at > _TTL_STALE_WARN:
        t = datetime.fromtimestamp(usd_entry.fetched_at, tz=MOSCOW_TZ).strftime("%H:%M")
        stale.append(f"⚠️ USD/RUB: данные на {t}, не удалось обновить")

    if stale:
        lines.append("")
        lines.extend(stale)

    await message.answer("\n".join(lines), parse_mode="HTML")
