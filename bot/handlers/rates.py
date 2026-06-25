import time
from datetime import datetime

from aiogram import F, Router
from aiogram.types import Message

from services.rate_cache import RateCache
from services.rate_providers.rapira import RapiraProvider

router = Router()

_rapira = RapiraProvider()
_CACHE_KEY = "usdt_rub"
_TTL_STALE_WARN = 600  # показываем предупреждение если данные старше 10 мин


@router.message(F.text == "💱 Курс валют")
async def cmd_rates(message: Message, rate_cache: RateCache) -> None:
    entry = await rate_cache.get(_CACHE_KEY, _rapira)

    fetched_time = datetime.fromtimestamp(entry.fetched_at).strftime("%H:%M")
    is_stale = time.time() - entry.fetched_at > _TTL_STALE_WARN

    rate_str = f"{entry.rate:,.2f}".replace(",", " ")

    lines = [
        "💱 <b>Курс валют</b>",
        "",
        "1 USDT",
        f"<b>{rate_str} ₽</b>  <i>(с комиссией +4.5%)</i>",
    ]

    if is_stale:
        lines += ["", f"⚠️ Данные на {fetched_time} — не удалось обновить"]
    else:
        lines += ["", f"Обновлено в {fetched_time}"]

    await message.answer("\n".join(lines), parse_mode="HTML")
