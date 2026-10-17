import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal

from aiogram import F, Router
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message

from handlers.payment import payment_entry_keyboard
from services.rate_cache import RateCache
from services.usd_markup import MOSCOW_TZ
from services.yuan import DIVISOR, yuan_price

router = Router()

_CACHE_KEY_USDT = "usdt_rub"
_CACHE_KEY_ALFABIT = "usdt_rub_alfabit"
_TTL_STALE_WARN = 1800
_USDT_MAX_AGE = 180
# Наличные: чистый USDT/RUB Rapira (без наценки QR) + фиксированная надбавка
# в рублях, затем тот же делитель юаня. Формула клиента, 17.10.2026.
_CASH_SURCHARGE_RUB = Decimal("1.3")

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


@router.message(F.text == "💱 Купить Юань")
async def cmd_rates(
    message: Message,
    rate_cache: RateCache,
    rapira_usdt_markup: Decimal,
) -> None:
    loading = await message.answer(_LOADING_FRAMES[0])
    anim_task = asyncio.create_task(_animate_loading(loading))

    try:
        await rate_cache.refresh(_CACHE_KEY_USDT, max_age_seconds=_USDT_MAX_AGE)
        if rate_cache.is_registered(_CACHE_KEY_ALFABIT):
            await rate_cache.refresh(_CACHE_KEY_ALFABIT, max_age_seconds=_USDT_MAX_AGE)
        usdt_entry = rate_cache.get(_CACHE_KEY_USDT)
        alfabit_entry = rate_cache.get(_CACHE_KEY_ALFABIT)
    finally:
        anim_task.cancel()
        try:
            await anim_task
        except asyncio.CancelledError:
            pass

    if usdt_entry is None:
        await loading.edit_text("⚠️ Курс пока не загружен, попробуйте через минуту.")
        return

    keyboard: InlineKeyboardMarkup | None = None
    now = datetime.now(tz=timezone.utc)
    moscow = now.astimezone(MOSCOW_TZ)

    day = _WEEKDAYS[moscow.weekday()]
    date_str = moscow.strftime("%d.%m")
    time_str = moscow.strftime("%H:%M")

    lines = [
        f"💱 <b><u>Курс на {date_str} ({day}, {time_str})</u></b>",
        "",
        f"<b>Курс (юань/руб): — {DIVISOR}</b>",
        "",
        "<b>QR-оплата (Юань/Руб):</b>",
        f"→ {_fmt(yuan_price(usdt_entry.rate))} ₽",
        "",
    ]

    # В кеше курс Rapira уже с наценкой QR — снимаем её обратно, чтобы обе
    # строки считались от одного снимка биржи.
    rapira_raw = usdt_entry.rate / rapira_usdt_markup
    lines += [
        "<b>Курс наличные (Юань/Руб):</b>",
        f"→ {_fmt(yuan_price(rapira_raw + _CASH_SURCHARGE_RUB))} ₽",
        "<i>(курс приблизительный, так же может отличаться от суммы)</i>",
        "",
    ]

    if alfabit_entry is not None:
        lines += [
            "<b>QR + KYC (верификация по фото паспорта 18+)</b>",
            "(Юань/Руб):",
            f"→ {_fmt(yuan_price(alfabit_entry.rate))} ₽",
            "<b>— Данные при регистрации должны совпадать с данными "
            "отправителя средств в банке.</b>",
            "— Может не работать с Альфа-Банком и Озоном",
            "(на больших суммах не всегда срабатывает)",
            "",
        ]
        keyboard = payment_entry_keyboard()

    # Наличные в Москве убраны из сообщения по просьбе клиента (30.09.2026).
    # USD/RUB по-прежнему обновляется в main.py, но здесь не читается —
    # его сбой больше не должен скрывать курс целиком.
    while lines and lines[-1] == "":
        lines.pop()

    stale = []
    if time.time() - usdt_entry.fetched_at > _TTL_STALE_WARN:
        t = datetime.fromtimestamp(usdt_entry.fetched_at, tz=MOSCOW_TZ).strftime("%H:%M")
        stale.append(f"⚠️ Юань/RUB: данные на {t}, не удалось обновить")

    if stale:
        lines.append("")
        lines.extend(stale)

    await loading.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        reply_markup=keyboard,
    )
