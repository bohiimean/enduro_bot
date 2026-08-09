from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

router = Router()

BTN_RATES = "💱 Купить Юань"
BTN_ORDERS = "📦 Статус заказа"
BTN_CATALOG = "🛍 Байки в наличии в Москве"
BTN_KYC_STATUS = "🔎 Статус верификации"
BTN_KYC_REDO = "📄 Отправить документы заново"

# По этим текстам FSM-хендлеры понимают, что пользователь ушёл из сценария,
# и не принимают нажатие кнопки меню за телефон или сумму. BTN_KYC_REDO сюда
# не входит: это инлайн-кнопка, в клавиатуре её нет.
MENU_BUTTONS = {BTN_RATES, BTN_ORDERS, BTN_CATALOG, BTN_KYC_STATUS}

_MAIN_ROWS = [
    [KeyboardButton(text=BTN_RATES)],
    [KeyboardButton(text=BTN_ORDERS)],
    [KeyboardButton(text=BTN_CATALOG)],
]

main_keyboard = ReplyKeyboardMarkup(keyboard=_MAIN_ROWS, resize_keyboard=True)

# Пока документы на проверке, в меню появляется четвёртая кнопка. Убирать её
# отдельно не нужно: любой возврат в меню с main_keyboard заменит клавиатуру.
#
# BTN_KYC_REDO здесь нет намеренно: аварийный выход из зависшего pending живёт
# инлайн-кнопкой на сообщении «проверка ещё идёт» (handlers/payment.py). В меню
# он попадался на глаза постоянно и читался как второй штатный шаг, хотя нужен
# в редком случае — когда бот перезапустился и не помнит, что уже отправлял.
kyc_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=_MAIN_ROWS + [[KeyboardButton(text=BTN_KYC_STATUS)]],
    resize_keyboard=True,
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Выберите действие:",
        reply_markup=main_keyboard,
    )
