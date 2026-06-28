from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from services.sheets import SheetsCache

router = Router()


class OrderStates(StatesGroup):
    waiting_for_phone = State()


_ask_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
)

_MENU_BUTTONS = {"💱 Купить Юань", "📦 Статус заказа", "🛍 Каталог товаров"}


@router.message(F.text == "📦 Статус заказа")
async def cmd_orders(message: Message, state: FSMContext) -> None:
    await state.set_state(OrderStates.waiting_for_phone)
    await message.answer(
        "Введите номер телефона для поиска заказа:",
        reply_markup=_ask_keyboard,
    )


@router.message(OrderStates.waiting_for_phone)
async def handle_phone(message: Message, state: FSMContext, sheets_cache: SheetsCache) -> None:
    text = message.text or ""

    if text == "❌ Отмена" or text in _MENU_BUTTONS:
        await state.clear()
        from handlers.start import main_keyboard
        await message.answer("Отменено.", reply_markup=main_keyboard)
        return

    orders = sheets_cache.find_orders_by_phone(text)

    await state.clear()
    from handlers.start import main_keyboard

    if not orders:
        await message.answer(
            "Заказы по этому номеру не найдены.\n"
            "Проверьте номер и попробуйте снова.",
            reply_markup=main_keyboard,
        )
        return

    count = len(orders)
    noun = _order_noun(count)
    lines = [f"📦 <b>Найдено {count} {noun}</b>"]

    for i, order in enumerate(orders, start=1):
        brand = order.get("Марка", "—")
        model = order.get("Модель", "—")
        color = order.get("Цвет", "—")
        price = order.get("Остаток", "—")
        status = order.get("Статус поставки") or "—"
        date = order.get("дата") or "—"

        lines.append("")
        lines.append(f"<b>{i}. {brand} {model} — {color}</b>")
        lines.append(f"Остаток: {price}")
        lines.append(f"Статус: {status}")
        lines.append(f"Дата заказа: {date}")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard)


def _order_noun(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "заказов"
    rem = n % 10
    if rem == 1:
        return "заказ"
    if 2 <= rem <= 4:
        return "заказа"
    return "заказов"
