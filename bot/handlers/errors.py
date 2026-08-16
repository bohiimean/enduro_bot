"""Последняя сеть под всеми хендлерами.

Глобального error-хендлера у бота не было: любое исключение вне `except
AlfabitError` уходило в лог aiogram, а пользователь не получал вообще ничего.
Молчание — худший ответ в этом боте: человек, приславший фото паспорта, уверен,
что документ ушёл. Здесь исключение попадает в лог с user_id (без него по логу
не найти, чей это был апдейт), а человеку приходит короткое «не получилось».

Регистрируется на Dispatcher, а не на роутере: ошибки нужны от всех.
"""
from __future__ import annotations

import logging

from aiogram.types import ErrorEvent, Update

logger = logging.getLogger(__name__)


def _addressee(update: Update) -> tuple[int | None, int | None]:
    """(chat_id, user_id) из апдейта любого вида, какой мы обрабатываем."""
    if update.message:
        return update.message.chat.id, update.message.from_user.id if update.message.from_user else None
    if update.callback_query:
        message = update.callback_query.message
        return (message.chat.id if message else None), update.callback_query.from_user.id
    return None, None


async def on_unhandled_error(event: ErrorEvent) -> bool:
    chat_id, user_id = _addressee(event.update)
    logger.error(
        "Необработанная ошибка: update=%s user=%s",
        event.update.update_id,
        user_id,
        exc_info=event.exception,
    )

    if chat_id is None:
        return True
    try:
        await event.update.bot.send_message(
            chat_id,
            "⚠️ Что-то пошло не так на моей стороне — сообщение не обработалось.\n"
            "Попробуйте ещё раз или напишите менеджеру.",
        )
    except Exception:
        # Ответить не вышло (бота заблокировали, чат недоступен) — не роняем
        # обработку ошибки об ошибку.
        logger.warning("Не удалось сообщить об ошибке в чат %s", chat_id)
    return True
