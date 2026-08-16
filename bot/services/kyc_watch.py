"""Фоновое слежение за заявками на верификацию.

Вебхука на смену KYC у Alfabit нет — в доке это сказано прямым текстом
(«статус только GET или повторный POST /payments»), так что опрос остаётся
единственным способом узнать результат.

Опрос у бота был, но только пачкой в 20 секунд сразу после загрузки
(`_poll_kyc` в handlers/payment.py). Всё, что приходило позже, не доходило до
человека никогда: дальше бот ждал, пока тот сам нажмёт «Статус верификации».
Разбор 16.08.2026: плательщик одобрен 14.08 в 22:28 МСК, бот молчал двое суток,
клиент считал, что верификация зависла. Здесь заявка досматривается до
терминального статуса и результат приходит сам.

Реестр живёт в памяти процесса — как и остальное состояние в этом боте. После
перезапуска слежение теряется, и человек снова остаётся с кнопкой «Статус
верификации» в меню. Писать телефоны на диск было бы возвратом к тому, от чего
отказались 08.08.2026 (services/user_phones.py удалён вместе с volume'ом), —
это осознанный размен, а не недосмотр.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from services.alfabit.client import AlfabitClient, AlfabitError
from services.kyc_trace import log_payer, mask_phone

logger = logging.getLogger(__name__)

# Как часто досматривать заявки. Секунды здесь не нужны: пачку сразу после
# загрузки (там счёт на секунды, как просит дока) делает _poll_kyc, а сюда
# заявка попадает уже «надолго» — распознавание не уложилось или её забрал
# оператор.
POLL_INTERVAL_SECONDS = 60

# Дольше этого заявку не досматриваем: ручная сверка занимает часы, а не дни,
# и бесконечно дёргать API по забытому номеру незачем. Снятие с наблюдения
# пишется в лог — это как раз тот случай, который стоит разобрать руками.
_MAX_AGE_SECONDS = 72 * 3600

_TERMINAL = {"approved", "rejected"}


@dataclass
class Watch:
    user_id: int
    chat_id: int
    phone: str
    since: float = field(default_factory=time.time)


class KycWatcher:
    """Досматривает заявки до approved/rejected и зовёт `on_resolved`."""

    def __init__(
        self,
        client: AlfabitClient,
        on_resolved: Callable[[Watch, dict], Awaitable[None]],
    ) -> None:
        self._client = client
        self._on_resolved = on_resolved
        self._watches: dict[int, Watch] = {}

    def watch(self, user_id: int, chat_id: int, phone: str) -> None:
        if user_id in self._watches:
            self._watches[user_id].phone = phone
            return
        self._watches[user_id] = Watch(user_id=user_id, chat_id=chat_id, phone=phone)
        logger.info(
            "kyc watch: взяли под наблюдение user=%s phone=%s (всего %d)",
            user_id, mask_phone(phone), len(self._watches),
        )

    def forget(self, user_id: int) -> None:
        if self._watches.pop(user_id, None) is not None:
            logger.info("kyc watch: сняли с наблюдения user=%s", user_id)

    def is_watched(self, user_id: int) -> bool:
        return user_id in self._watches

    async def tick(self) -> None:
        """Один обход. Вызывается по расписанию из main.py."""
        for watch in list(self._watches.values()):
            if time.time() - watch.since > _MAX_AGE_SECONDS:
                logger.warning(
                    "kyc watch: заявка user=%s phone=%s не закрылась за %d ч — "
                    "снимаем с наблюдения, разбирайте руками",
                    watch.user_id, mask_phone(watch.phone), _MAX_AGE_SECONDS // 3600,
                )
                self._watches.pop(watch.user_id, None)
                continue

            try:
                payer = await self._client.get_payer(watch.phone)
            except AlfabitError as exc:
                # Заявку не снимаем: сеть отвалилась — придём на следующем круге.
                logger.warning(
                    "kyc watch: не удалось опросить user=%s phone=%s: %s",
                    watch.user_id, mask_phone(watch.phone), exc,
                )
                continue

            log_payer("watch", user_id=watch.user_id, phone=watch.phone, payer=payer)

            if payer.get("kyc_status") not in _TERMINAL:
                continue

            # Снимаем ДО уведомления: если отправка сообщения упадёт, второй
            # раз дёргать пользователя по кругу не нужно — статус он всё равно
            # увидит кнопкой в меню.
            self._watches.pop(watch.user_id, None)
            try:
                await self._on_resolved(watch, payer)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "kyc watch: не удалось сообщить user=%s результат верификации",
                    watch.user_id,
                )
