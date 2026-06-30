import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal

from aiogram import F, Router
from aiogram.types import LinkPreviewOptions, Message

from services.rate_cache import RateCache
from services.usd_markup import MOSCOW_TZ, get_usd_rate_info

router = Router()

_CACHE_KEY_USDT = "usdt_rub"
_CACHE_KEY_USD = "usd_rub"
_TTL_STALE_WARN = 1800
_FACTOR = Decimal("1") / Decimal("6.7")

_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_LOADING_FRAMES = [
    "⏳ Вычисляем актуальный курс",
    "⏳ Вычисляем актуальный курс.",
    "⏳ Вычисляем актуальный курс..",
    "⏳ Вычисляем актуальный курс...",
]


async def _animate_loading(msg: Message) -> None:
    i = 0
    while True:
        await asyncio.sleep(0.8)
        i = (i + 1) % len(_LOADING_FRAMES)
        try:
            await msg.edit_text(_LOADING_FRAMES[i])
        except Exception:
            return


def _fmt(rate: Decimal) -> str:
    return f"{rate:.2f}"


def _yuan_result(rate: Decimal) -> Decimal:
    return (_FACTOR * rate).quantize(Decimal("0.01"))


def _usd_result(base: Decimal, markup: Decimal) -> Decimal:
    return (_FACTOR * (base + markup)).quantize(Decimal("0.01"))


@router.message(F.text == "💱 Купить Юань")
async def cmd_rates(
    message: Message,
    rate_cache: RateCache,
    manager_tg_username: str,
) -> None:
    loading = await message.answer(_LOADING_FRAMES[0])
    anim_task = asyncio.create_task(_animate_loading(loading))

    try:
        usdt_entry = rate_cache.get(_CACHE_KEY_USDT)
        usd_entry = rate_cache.get(_CACHE_KEY_USD)
    finally:
        anim_task.cancel()
        try:
            await anim_task
        except asyncio.CancelledError:
            pass

    if usdt_entry is None or usd_entry is None:
        await loading.edit_text("⚠️ Курс пока не загружен, попробуйте через минуту.")
        return

    now = datetime.now(tz=timezone.utc)
    moscow = now.astimezone(MOSCOW_TZ)
    usd_info = get_usd_rate_info(now)
    base_usd = usd_entry.rate

    day = _WEEKDAYS[moscow.weekday()]
    date_str = moscow.strftime("%d.%m")
    time_str = moscow.strftime("%H:%M")

    lines = [
        f"💱 <b><u>Курс на {date_str} ({day}, {time_str})</u></b>",
        "",
        "<b>QR-оплата (Юань/Руб):</b>",
        f"→ {_fmt(_yuan_result(usdt_entry.rate))} ₽",
        "",
        "<b>Наличные в Москве</b>",
        "+ дистанционное снятие по QR с вашего банка.",
        "(Юань/Руб):",
        f"→ Любая сумма — {_fmt(_usd_result(base_usd, usd_info.standard.markup))} ₽",
        "",
        "<b>VIP курс действует с 12:30 до 16:00 в будний день</b>",
    ]

    for tier in usd_info.discount_tiers:
        lines.append(f"→ {tier.label} — {_fmt(_usd_result(base_usd, tier.markup))} ₽")

    username = manager_tg_username.lstrip("@")
    manager_link = f'<a href="https://t.me/{username}">менеджер</a>' if username else "менеджер"

    lines.extend([
        "",
        "<i>(Курс доллара скачет каждую секунду и не фиксируется до момента передачи денег)</i>",
        "",
        f"Место и время сделки поможет выбрать {manager_link}.",
    ])

    stale = []
    if time.time() - usdt_entry.fetched_at > _TTL_STALE_WARN:
        t = datetime.fromtimestamp(usdt_entry.fetched_at, tz=MOSCOW_TZ).strftime("%H:%M")
        stale.append(f"⚠️ Юань/RUB: данные на {t}, не удалось обновить")
    if time.time() - usd_entry.fetched_at > _TTL_STALE_WARN:
        t = datetime.fromtimestamp(usd_entry.fetched_at, tz=MOSCOW_TZ).strftime("%H:%M")
        stale.append(f"⚠️ USD/RUB: данные на {t}, не удалось обновить")

    if stale:
        lines.append("")
        lines.extend(stale)

    await loading.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
