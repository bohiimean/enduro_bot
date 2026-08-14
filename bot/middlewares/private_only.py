"""Бот обслуживает только личные сообщения — всё остальное отбрасывается.

13.08.2026 бот ответил «Не понял. Выберите действие в меню ниже.» в группе
«VELMoto Чат». Виноват не catch-all из handlers/fallback.py — он лишь сделал
видимым то, что происходило и раньше молча. Настоящая связка такая: у бота и у
скрапера новостей один токен на двоих (@SKLmoto_bot), скраперу нужна админка
(без неё slow mode группы рубит вторую часть длинного поста), а
бот-администратор получает **все** сообщения группы независимо от privacy mode.
Хендлеры при этом написаны так, будто существует только приватный чат.

Фильтр стоит внешним middleware на диспетчере, а не фильтром на каждом роутере:
про роутер, добавленный завтра, забудут, а сюда апдейт попадает до
маршрутизации — и сообщение, и коллбэк, и всё, что появится потом.

Это не отменяет разделения токенов: оно убирает причину, по которой права
пересеклись, а фильтр держит бота корректным при любых правах в любом чате —
в том числе когда бота добавит в группу посторонний (`can_join_groups: true`,
то есть добавить его может кто угодно и без нашего участия).

Групповой апдейт именно **отбрасывается**, а не получает вежливый отказ: любой
ответ в группе — это снова сообщение от бота в чужом чате, а на шагах KYC ещё и
запрос телефона и паспорта при свидетелях. Нажатие инлайн-кнопки из группы
останется без ответа (у пользователя покрутится и погаснет спиннер) — это
осознанно, отвечать на коллбэк значит опять писать в группу.
"""
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Chat, TelegramObject, Update

logger = logging.getLogger(__name__)


def _chat_of(update: Update) -> Chat | None:
    """Чат, которому принадлежит апдейт, или None, если чата у него нет.

    Поля перечислены явно, а не через `Update.event`: то — property, которое на
    незнакомом типе апдейта бросает UpdateTypeLookupError, и ронять на этом
    приём обновлений незачем.
    """
    event = (
        update.message
        or update.edited_message
        or update.channel_post
        or update.edited_channel_post
        or update.my_chat_member
        or update.chat_member
        or update.chat_join_request
    )
    if event is not None:
        return event.chat
    if update.callback_query is not None and update.callback_query.message is not None:
        return update.callback_query.message.chat
    return None


class PrivateOnlyMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        # Чтобы не сыпать одинаковым предупреждением на каждое сообщение
        # оживлённой группы. Растёт по числу разных чатов, а не сообщений.
        self._reported: set[int] = set()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = _chat_of(event) if isinstance(event, Update) else None

        # Чата нет (inline-запросы, ответы на опросы) — пропускаем: решать
        # там нечего, а тихо ронять неизвестный апдейт хуже, чем отдать его
        # хендлерам, которых на него всё равно нет.
        if chat is None or chat.type == ChatType.PRIVATE:
            return await handler(event, data)

        self._report_once(chat)
        return None

    def _report_once(self, chat: Chat) -> None:
        """Отбросить молча для пользователя, но не молча для нас.

        Прошлый раз связку «бот сидит админом в группе» пришлось выяснять по
        скриншоту: в логи не попадали ни chat_id, ни тип чата, и по ним нельзя
        было ни найти чат, ни доказать, что апдейт вообще оттуда. Фильтр
        убирает симптом, поэтому причину надо назвать вслух — иначе бот снова
        годами сидит админом в чужом чате, и никто об этом не знает.
        """
        if chat.id in self._reported:
            return
        self._reported.add(chat.id)
        logger.warning(
            "Апдейты из чата %s (id=%s, %s) отбрасываются: бот работает только "
            "в личных сообщениях. Скорее всего, бот добавлен туда админом — "
            "проверьте, нужен ли он там.",
            chat.title or chat.username or "без названия",
            chat.id,
            chat.type,
        )
