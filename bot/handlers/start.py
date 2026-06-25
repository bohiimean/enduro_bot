from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

router = Router()

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💱 Курс валют")],
        [KeyboardButton(text="📦 Статус заказа")],
        [KeyboardButton(text="🛍 Каталог товаров")],
    ],
    resize_keyboard=True,
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Выберите действие:",
        reply_markup=main_keyboard,
    )
