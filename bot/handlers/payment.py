"""Оплата рублями по СБП с KYC (Alfabit checkout).

Порядок шагов сознательно отличается от схемы в доках alfabit: KYC делается
ДО расчёта суммы. В доках платёж создаётся первым и возвращает kyc_required,
но тогда зафиксированный курс жил бы весь KYC (а он может уйти на ручную
сверку — это не секунды). Здесь: телефон → KYC → и только потом количество
юаней, свежий курс, создание платежа и QR. Окно фиксации курса — от QR до
оплаты, минуты.

Состояние на сервере не храним (кроме подсказки для уведомления менеджера):
payment_uid кодируется прямо в callback_data кнопки «Я оплатил».

Отрезок «телефон → пройденный KYC» — самый хрупкий: половина комплекта в
Alfabit означает заявку в вечном pending, из которой обычный сценарий не
выпускает (ветка pending в _route_by_kyc только просит ждать). Поэтому здесь:
- документы копятся в буфере и уходят комплектом (_send_documents), а не по
  мере поступления: если человек пропал после первой страницы, в Alfabit не
  осталось ничего;
- альбом отклоняем — замок задаёт очередь, но не порядок, и пара снимков может
  переставиться местами (см. _reject_album);
- шаг сбора документов не сбрасывает ни одна кнопка меню (см. kyc_status);
- приём файлов идёт под замком на пользователя, чтобы два быстрых сообщения не
  заняли один слот;
- на этих шагах у пользователя видна только «Отмена»;
- любое сообщение, не подошедшее ни одному хендлеру, ловит handlers/fallback.py,
  чтобы фото паспорта не пропадало молча.

Результат верификации приходит человеку сам: заявка встаёт под наблюдение
(services/kyc_watch.py) и досматривается до approved/rejected. Раньше бот
опрашивал статус 20 секунд после загрузки и замолкал — одобрение, пришедшее
минутой позже, не доходило никогда, а человек считал, что верификация зависла.

Три разных «pending» различаются по полям ответа Alfabit и звучат по-разному:
processing=true — идёт распознавание; manual_review=true — заявку забрал
оператор (тогда переотправка тех же файлов запрещена докой и кнопки повтора
здесь нет); всё остальное — мы ждём от человека недостающий документ.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from handlers.start import (
    BTN_KYC_REDO,
    BTN_KYC_STATUS,
    MENU_BUTTONS,
    kyc_menu_keyboard,
    main_keyboard,
)
from services.alfabit.client import TRANSPORT_ERROR_CODES, AlfabitClient, AlfabitError
from services.kyc_trace import log_payer, log_upload
from services.kyc_watch import KycWatcher, Watch
from services.rate_cache import RateCache
from services.yuan import yuan_price
from utils.phone import normalize_phone

logger = logging.getLogger(__name__)
router = Router()

_CACHE_KEY_ALFABIT = "usdt_rub_alfabit"
_RATE_MAX_AGE = 180

# Загрузка документов: профиль приёма по умолчанию требует эти два.
_DOC_MAIN = "passport_main"
_DOC_REGISTRATION = "passport_registration"

# getFile не отдаёт боту файлы больше 20 МБ (у Alfabit предел 25 МБ, но до него
# дело не дойдёт). Проверяем по file_size из апдейта — до скачивания.
_MAX_FILE_BYTES = 20 * 1024 * 1024

# Поллинг статуса KYC после загрузки документов (processing=true).
_KYC_POLL_ATTEMPTS = 10
_KYC_POLL_DELAY = 2.0
# Поллинг реквизитов СБП после создания платежа (qr_url появляется за секунды).
_QR_POLL_ATTEMPTS = 8
_QR_POLL_DELAY = 2.0

_PAID_STATUSES = {"paid", "succeeded", "success", "completed"}
_FAILED_STATUSES = {"expired", "failed", "canceled", "cancelled", "rejected"}

# Ошибки доступа: у ключа нет прав / подпись не принята. Пользователь тут
# бессилен — повторять попытку бессмысленно, чинится в кабинете alfabit.
_ACCESS_ERROR_CODES = {"PERMISSION_DENIED", "UNAUTHORIZED", "FORBIDDEN", "INVALID_SIGNATURE"}

_CANCEL = "❌ Отмена"

# На шагах с документами и суммой в меню не должно остаться ничего, кроме
# «Отмена»: любая другая кнопка здесь — способ случайно выйти из сценария.
_cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=_CANCEL)]],
    resize_keyboard=True,
    input_field_placeholder="Пришлите фото документа",
)

# Загрузка документов по одному пользователю строго по очереди. Telegram шлёт
# альбом двумя update'ами, а aiogram обрабатывает их параллельными задачами
# (handle_as_tasks=True), поэтому без замка оба фото попадали в шаг
# passport_main и второе перезаписывало первое.
_upload_locks: dict[int, asyncio.Lock] = {}
_LOCK_LIMIT = 500


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _upload_locks.get(user_id)
    if lock is None:
        if len(_upload_locks) >= _LOCK_LIMIT:
            for stale_id, stale in list(_upload_locks.items()):
                if not stale.locked():
                    del _upload_locks[stale_id]
                    break
        lock = asyncio.Lock()
        _upload_locks[user_id] = lock
    return lock


class _FileError(Exception):
    """Файл не удалось взять из Telegram. Текст — готовый ответ пользователю."""


def _phone_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура ввода номера. Номер спрашиваем каждый раз заново — ничего
    о пользователе между заказами не храним.

    Совсем убрать поле ввода Bot API не позволяет — reply-клавиатура всегда
    рисуется над ним. Placeholder хотя бы подсказывает, что ждём кнопку.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
            [KeyboardButton(text=_CANCEL)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Нажмите кнопку ниже",
    )


class PayStates(StatesGroup):
    phone = State()
    passport_main = State()
    passport_registration = State()
    amount = State()


# doc_type → (ключ в данных FSM, шаг сценария, как называть страницу человеку).
# Порядок важен: комплект уходит в Alfabit в порядке этого словаря.
_DOC_SLOTS: dict[str, tuple[str, State, str]] = {
    _DOC_MAIN: ("doc_main", PayStates.passport_main, "главной страницы паспорта"),
    _DOC_REGISTRATION: ("doc_reg", PayStates.passport_registration, "страницы с пропиской"),
}


@dataclass
class PaymentContext:
    """Подсказка для уведомления менеджера. Живёт до перезапуска процесса —
    если её нет, менеджер получит то, что отдаёт API по uid."""

    user_id: int
    username: str | None
    phone: str
    payer_name: str | None
    cny: Decimal
    rub: Decimal


_pending: dict[str, PaymentContext] = {}
_PENDING_LIMIT = 500


def _remember(uid: str, ctx: PaymentContext) -> None:
    """Неоплаченные платежи никто не удаляет — держим только последние."""
    if len(_pending) >= _PENDING_LIMIT:
        _pending.pop(next(iter(_pending)))
    _pending[uid] = ctx


def payment_entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(
            text="🧾 Оплатить по QR + KYC",
            callback_data="pay:start",
        )]]
    )


def _to_e164(raw: str) -> str | None:
    """Российский номер в формате +7XXXXXXXXXX — как ждёт alfabit."""
    digits = normalize_phone(raw)
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    return None


async def _back_to_menu(message: Message, state: FSMContext, text: str) -> None:
    await state.clear()
    await message.answer(text, reply_markup=main_keyboard)


async def _park_until_kyc(
    state: FSMContext,
    watcher: KycWatcher | None,
    *,
    user_id: int,
    chat_id: int,
    phone: str,
) -> None:
    """Документы на проверке: выходим из шага, чтобы пользователь свободно
    ходил по боту, но телефон в данных оставляем — по нему кнопка «Статус
    верификации» узнаёт, чью заявку проверять. Хранилище FSM — in-memory,
    так что связка живёт до перезапуска процесса; после него кнопка честно
    предложит начать оплату заново.

    Заодно ставим заявку под наблюдение: ждать, пока человек сам нажмёт кнопку,
    больше не нужно — результат придёт ему сам (services/kyc_watch.py).
    """
    await state.set_state(None)
    if watcher is not None:
        watcher.watch(user_id, chat_id, phone)


def _is_exit(text: str) -> bool:
    return text == _CANCEL or text in MENU_BUTTONS


def _is_access_error(exc: AlfabitError) -> bool:
    return exc.code in _ACCESS_ERROR_CODES or exc.status in (401, 403)


def _user_message(exc: AlfabitError) -> str:
    """Текст ошибки для пользователя. Транспортные коды прячем: «NETWORK:
    ServerDisconnectedError» человеку ничего не объясняет."""
    if exc.code in TRANSPORT_ERROR_CODES:
        return "платёжный сервис не отвечает, попробуйте через пару минут"
    return exc.message or exc.code


# ---- Статус верификации (кнопка в меню) -----------------------------------

# Последнее сообщение о статусе на пользователя: следующая проверка удаляет
# предыдущее, чтобы в чате не копилась лента одинаковых «проверка ещё идёт».
_status_messages: dict[int, int] = {}
_STATUS_LIMIT = 500


async def _clear_status(bot: Bot, chat_id: int, user_id: int) -> None:
    """Убрать предыдущее сообщение о статусе.

    Принимает bot/chat_id, а не Message: то же самое делает фоновое уведомление
    о результате верификации, у которого никакого входящего сообщения нет.
    """
    old = _status_messages.pop(user_id, None)
    if old is None:
        return
    try:
        await bot.delete_message(chat_id, old)
    except TelegramBadRequest:
        # Сообщение уже удалено пользователем или слишком старое — не мешает.
        pass


async def _replace_status(
    message: Message,
    user_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Сообщение о статусе: предыдущее удаляем, новое шлём вниз чата.

    Разметка только инлайновая. Reply-клавиатуру такие сообщения не носят
    намеренно: удаление сообщения уносит с собой и меню, которое оно поставило,
    и человек остаётся без клавиатуры вообще. Всё, что ставит меню, шлётся через
    _menu_message и не удаляется никогда.
    """
    await _clear_status(message.bot, message.chat.id, user_id)
    sent = await message.answer(text, parse_mode="HTML", reply_markup=markup)
    if user_id not in _status_messages and len(_status_messages) >= _STATUS_LIMIT:
        _status_messages.pop(next(iter(_status_messages)))
    _status_messages[user_id] = sent.message_id


async def _menu_message(
    message: Message, user_id: int, text: str, keyboard: ReplyKeyboardMarkup
) -> None:
    """Сообщение, которое (пере)ставит меню. Как статусное не регистрируется —
    удалять его нельзя, вместе с ним исчезнет клавиатура."""
    await _clear_status(message.bot, message.chat.id, user_id)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


def _redo_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «отправить документы заново».

    Подставляется в одном месте — на сообщении статуса, когда Alfabit держит
    pending, а недостачи мы не видим (см. kyc_status). В меню кнопки нет: там
    она попадалась на глаза постоянно и читалась как второй штатный шаг.
    Заодно кнопка живёт в уже отправленных старых сообщениях.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=BTN_KYC_REDO, callback_data="kyc:redo")]]
    )


def _quality_hint(payer: dict, low_conf: Sequence[str] = ()) -> str:
    """Всё, что Alfabit сообщает о качестве снимков — а сообщает он мало.

    Прямой причины отказа в API нет. Есть два косвенных признака: low_confidence
    в ответе на загрузку («распозналось плохо») и пустой expected_payer_name
    после распознавания — значит, ФИО с главной страницы прочитать не удалось.
    Оба до сих пор молча выбрасывались, хотя это единственное, что можно сказать
    человеку до того, как заявка уйдёт к оператору на сутки.
    """
    if low_conf:
        pages = ", ".join(_DOC_SLOTS[doc][2] for doc in low_conf if doc in _DOC_SLOTS)
        return (
            f"\n\n⚠️ Сервис отметил, что фото {pages} читается плохо. "
            "Если проверка не пройдёт — переснимите эту страницу при хорошем "
            "свете, без бликов, чтобы края попали в кадр."
        )
    if payer.get("manual_review") and not payer.get("expected_payer_name"):
        return (
            "\n\n⚠️ С главной страницы паспорта не удалось считать ФИО — обычно "
            "дело в качестве снимка: блики, смазано или обрезан край."
        )
    return ""


def _waiting_text(payer: dict, low_conf: Sequence[str] = ()) -> str:
    """Честный текст ожидания. Раньше на все случаи был один — «это может
    занять время», из-за чего ручная сверка выглядела так же, как недошедший
    документ."""
    if payer.get("manual_review"):
        text = (
            "⏳ <b>Документы проверяет сотрудник.</b>\n\n"
            "Автоматическая проверка не смогла принять решение сама и передала "
            "заявку человеку — это дольше обычного.\n\n"
            "Присылать те же фото заново не нужно: повторная отправка проверку "
            "не ускорит. Как только она закончится, я напишу сюда сам."
        )
    else:
        text = (
            "⏳ <b>Документы на проверке.</b>\n\n"
            "Обычно это занимает меньше минуты. Как только проверка закончится, "
            "я напишу сюда сам — держать бота открытым не нужно."
        )
    return text + _quality_hint(payer, low_conf)


async def _pending_kyc_notice(
    message: Message, user_id: int, payer: dict, low_conf: Sequence[str] = ()
) -> None:
    """Документы ушли на проверку — дальше пользователь ждёт в меню.

    Это сообщение и ставит меню с кнопкой статуса, поэтому оно из неудаляемых.
    Кнопка остаётся как ручной способ проверить, но ключевого значения больше
    не имеет: результат придёт сам.
    """
    await _menu_message(
        message,
        user_id,
        _waiting_text(payer, low_conf)
        + f"\n\nПроверить статус вручную — кнопка «{BTN_KYC_STATUS}» в меню.",
        kyc_menu_keyboard,
    )


async def notify_kyc_resolved(
    bot: Bot, storage: BaseStorage, watch: Watch, payer: dict
) -> None:
    """Верификация закрылась, пока человек занимался своими делами.

    Зовётся из services/kyc_watch.py, входящего сообщения здесь нет — поэтому
    FSMContext собираем руками по тому же ключу, каким его строит aiogram для
    приватного чата.

    Если пользователь сейчас в другом сценарии (вводит телефон для статуса
    заказа, например), состояние не трогаем: перебить чужой шаг радостной
    новостью — тот самый класс ошибок, из-за которого 08.08 терялись документы.
    Тогда просто сообщаем результат, а к шагу суммы человек вернётся сам.
    """
    user_id, chat_id = watch.user_id, watch.chat_id
    log_payer("resolved in background", user_id=user_id, phone=watch.phone, payer=payer)
    _uploaded_docs.pop(watch.phone, None)

    state = FSMContext(
        storage=storage,
        key=StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=user_id),
    )
    busy = await state.get_state() is not None
    await _clear_status(bot, chat_id, user_id)

    if payer.get("kyc_status") == "rejected":
        if not busy:
            await state.clear()
        await bot.send_message(
            chat_id,
            "❌ <b>Верификация не пройдена.</b>\n\n"
            "Свяжитесь с менеджером — оплату оформим вручную.",
            parse_mode="HTML",
            reply_markup=None if busy else main_keyboard,
        )
        return

    if busy:
        await bot.send_message(
            chat_id,
            "✅ <b>Верификация пройдена!</b>\n\n"
            "Вернитесь к оплате через «💱 Купить Юань» — документы больше не нужны.",
            parse_mode="HTML",
        )
        return

    payer_name = payer.get("expected_payer_name")
    await state.update_data(phone=watch.phone, payer_name=payer_name)
    await state.set_state(PayStates.amount)
    await bot.send_message(
        chat_id,
        _amount_prompt(payer_name),
        parse_mode="HTML",
        reply_markup=_cancel_keyboard,
    )


@router.callback_query(F.data == "kyc:redo")
async def redo_documents(callback: CallbackQuery, state: FSMContext) -> None:
    """Аварийный выход из зависшего pending.

    Штатно недостачу закрывает _missing_docs: бот помнит, что успел отправить,
    и сам просит недостающую страницу. Но эта память живёт в процессе, и после
    перезапуска бота случай «Alfabit ждёт документ, а бот думает, что ждать
    должен человек» снова неразличим. Тогда остаётся отправить комплект заново —
    повторная загрузка того же doc_type идемпотентна, так что это безопасно и
    когда на самом деле идёт ручная сверка.
    """
    await callback.answer()
    if callback.message is None:
        return
    message = callback.message
    user_id = callback.from_user.id
    phone = (await state.get_data()).get("phone")
    if phone is None:
        await state.clear()
        await _menu_message(
            message,
            user_id,
            "Не нашли заявку на верификацию — начните оплату заново.",
            main_keyboard,
        )
        return

    await state.set_state(PayStates.passport_main)
    await _clear_docs(state)
    _uploaded_docs.pop(phone, None)
    await _clear_status(message.bot, message.chat.id, user_id)
    await message.answer(
        "Отправим документы заново. Пришлите фото <b>главной страницы паспорта</b>.",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard,
    )


# Что бот ждёт на каждом шаге — для ответа на кнопку меню, нажатую посреди
# сценария.
_STEP_HINTS = {
    PayStates.phone.state: "Жду номер телефона, с которого будете платить.",
    PayStates.passport_main.state: "Жду фото главной страницы паспорта.",
    PayStates.passport_registration.state: "Жду фото страницы с пропиской.",
    PayStates.amount.state: "Жду количество юаней.",
}


def _step_keyboard(current: str | None) -> ReplyKeyboardMarkup:
    return _phone_keyboard() if current == PayStates.phone.state else _cancel_keyboard


@router.message(F.text == BTN_KYC_STATUS)
async def kyc_status(
    message: Message,
    state: FSMContext,
    alfabit_client: AlfabitClient | None,
    kyc_watcher: KycWatcher | None,
) -> None:
    """Зарегистрирован раньше FSM-хендлеров: кнопка живёт в меню и должна
    работать, в каком бы сценарии пользователь сейчас ни находился.

    Именно поэтому шаг здесь трогать нельзя. Раньше хендлер безусловно делал
    set_state(None), и нажатие кнопки посреди загрузки документов уничтожало
    состояние: следующее фото не подходило ни одному хендлеру и молча
    выбрасывалось. В Alfabit оставался один документ из двух, заявка навсегда
    зависала в pending. Проверять статус имеет смысл только когда сценарий
    завершён (состояние None) — в остальных случаях подсказываем, чего ждём.
    """
    user_id = message.from_user.id
    current = await state.get_state()
    if current is not None:
        await message.answer(
            "Сначала закончим оформление оплаты — проверять пока нечего.\n"
            f"{_STEP_HINTS.get(current, '')}\n\n"
            f"Чтобы выйти, нажмите «{_CANCEL}».",
            reply_markup=_step_keyboard(current),
        )
        return

    phone = (await state.get_data()).get("phone")

    if phone is None or alfabit_client is None:
        await state.clear()
        await _menu_message(
            message,
            user_id,
            "Не нашли заявку на верификацию — начните оплату заново.",
            main_keyboard,
        )
        return

    try:
        payer = await alfabit_client.get_payer(phone)
    except AlfabitError as exc:
        logger.warning("alfabit get_payer status failed: user=%s %s", user_id, exc)
        await _menu_message(
            message,
            user_id,
            "⚠️ Сервис проверки не отвечает. Попробуйте позже.",
            kyc_menu_keyboard,
        )
        return

    log_payer("status button", user_id=user_id, phone=phone, payer=payer)

    # Кнопку жмут ровно тогда, когда результата ждут. Если заявка почему-то
    # выпала из наблюдения (истёк срок, уведомление не доставилось), сейчас
    # самое время вернуть её туда: телефон и чат под рукой.
    if kyc_watcher is not None and payer.get("kyc_status") not in ("approved", "rejected"):
        kyc_watcher.watch(user_id, message.chat.id, phone)

    # Идёт распознавание — недостачу разбирать рано, документы сейчас как раз
    # читают. Кнопки повтора здесь нет: переотправлять комплект посреди чтения
    # незачем, а повод сбивает.
    if payer.get("processing"):
        await _replace_status(
            message,
            user_id,
            "⏳ Идёт распознавание документов — это несколько секунд.\n"
            "Результат придёт сюда сам.",
        )
        return

    # Заявку забрал оператор. Дока про этот случай говорит прямо: «те же файлы
    # заново не отправляйте», поэтому кнопки повтора здесь нет — раньше она
    # висела именно тут и советовала ровно запрещённое. Живой случай
    # 16.08.2026: pending + manual_review + пустой expected_payer_name, то есть
    # ФИО не считалось и решение принимает человек.
    if payer.get("manual_review"):
        await _replace_status(message, user_id, _waiting_text(payer))
        return

    # Alfabit держит pending, оператор заявку не брал, а недостачи мы не видим.
    # Скорее всего до сервиса дошла половина комплекта, а наша память об
    # отправленном не пережила перезапуск процесса (_uploaded_docs). Здесь
    # кнопка повтора уместна и безопасна: повторная загрузка того же doc_type
    # дубликата не создаёт.
    if payer.get("kyc_status") == "pending" and not _missing_docs(phone):
        await _replace_status(
            message,
            user_id,
            "⏳ Проверка ещё идёт.\n\n"
            "Если ждёте больше суток — возможно, до сервиса дошла не вся пара "
            "документов. Кнопка ниже отправит комплект заново.",
            _redo_keyboard(),
        )
        return

    await _route_by_kyc(
        message, state, payer, user_id=user_id, phone=phone, watcher=kyc_watcher
    )


# ---- Шаг 1: телефон -------------------------------------------------------


@router.callback_query(F.data == "pay:start")
async def start_payment(
    callback: CallbackQuery,
    state: FSMContext,
    alfabit_client: AlfabitClient | None,
    alfabit_payer_ip: str | None,
) -> None:
    await callback.answer()
    if callback.message is None:
        return
    if alfabit_client is None or not alfabit_payer_ip:
        await callback.message.answer(
            "⚠️ Оплата по QR + KYC сейчас недоступна. Напишите менеджеру."
        )
        return

    await state.set_state(PayStates.phone)
    await callback.message.answer(
        "🧾 <b>Оплата по QR + KYC</b>\n\n"
        "Верификация делается один раз: потом оплата по этому номеру идёт без "
        "повторной загрузки документов.\n\n"
        "Отправьте номер телефона, с которого будете платить.",
        parse_mode="HTML",
        reply_markup=_phone_keyboard(),
    )


@router.message(PayStates.phone)
async def handle_phone(
    message: Message,
    state: FSMContext,
    alfabit_client: AlfabitClient | None,
    kyc_watcher: KycWatcher | None,
) -> None:
    raw = message.contact.phone_number if message.contact else (message.text or "")
    if not message.contact and _is_exit(raw):
        await _back_to_menu(message, state, "Отменено.")
        return

    phone = _to_e164(raw)
    if phone is None:
        await message.answer(
            "Не похоже на российский номер. Введите в формате +7 900 123-45-67."
        )
        return
    if alfabit_client is None:
        await _back_to_menu(message, state, "⚠️ Оплата сейчас недоступна.")
        return

    user_id = message.from_user.id
    try:
        payer = await alfabit_client.get_payer(phone)
    except AlfabitError as exc:
        logger.warning("alfabit get_payer failed: user=%s %s", user_id, exc)
        await _back_to_menu(message, state, "⚠️ Сервис оплаты не отвечает. Попробуйте позже.")
        return

    log_payer("phone entered", user_id=user_id, phone=phone, payer=payer)
    await _route_by_kyc(
        message, state, payer, user_id=user_id, phone=phone, watcher=kyc_watcher
    )


async def _route_by_kyc(
    message: Message,
    state: FSMContext,
    payer: dict,
    *,
    user_id: int,
    phone: str,
    watcher: KycWatcher | None = None,
    low_conf: Sequence[str] = (),
) -> None:
    """Развилка по статусу верификации плательщика."""
    await state.update_data(phone=phone)
    status = payer.get("kyc_status")

    if status == "approved":
        _uploaded_docs.pop(phone, None)
        if watcher is not None:
            watcher.forget(user_id)
        await _ask_amount(message, state, payer.get("expected_payer_name"), user_id)
        return

    if status == "rejected":
        _uploaded_docs.pop(phone, None)
        if watcher is not None:
            watcher.forget(user_id)
        await _clear_status(message.bot, message.chat.id, user_id)
        await _back_to_menu(
            message,
            state,
            "❌ Верификация не пройдена. Свяжитесь с менеджером — оплату оформим вручную.",
        )
        return

    if status == "pending":
        # Alfabit держит в pending в трёх разных случаях. Ручную сверку он
        # называет прямо (manual_review) — тогда документ от человека не нужен,
        # ждёт оператор. В остальных случаях недостачу может показать только
        # наша память о том, что мы отправляли.
        missing = [] if payer.get("manual_review") else _missing_docs(phone)
        if missing:
            doc_type = missing[0]
            await _clear_status(message.bot, message.chat.id, user_id)
            await _ask_again(
                message,
                state,
                doc_type,
                "Верификация не завершена: до сервиса дошла только часть документов.",
            )
            return
        await _park_until_kyc(
            state, watcher, user_id=user_id, chat_id=message.chat.id, phone=phone
        )
        await _pending_kyc_notice(message, user_id, payer, low_conf)
        return

    # not_started — просим документы. Если про номер что-то помнилось, сведения
    # устарели: у сервиса ничего нет, верить надо ему.
    _uploaded_docs.pop(phone, None)
    await state.set_state(PayStates.passport_main)
    await _clear_docs(state)
    await _clear_status(message.bot, message.chat.id, user_id)
    await message.answer(
        "Для оплаты нужна верификация: <b>главная страница паспорта</b> и "
        "<b>страница с пропиской</b>.\n\n"
        "⚠️ Фото уйдут в платёжный сервис для проверки и останутся в истории "
        "этого чата — удалите их у себя после верификации, если не хотите хранить.\n\n"
        "Пришлите фото <b>главной страницы паспорта</b>.",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard,
    )


# ---- Шаг 2: документы -----------------------------------------------------


def _describe_file(message: Message) -> dict[str, str]:
    """Ссылка на присланный файл: file_id, имя, mime. Ничего не скачивает.

    Пока комплект не собран, байты не нужны — файл и так лежит у Telegram, нам
    достаточно ссылки. Размер проверяем здесь же, по file_size из апдейта:
    раньше про слишком большой файл узнавали только когда падало скачивание.
    """
    if message.photo:
        photo = message.photo[-1]
        file_id, filename, mime, size = photo.file_id, "passport.jpg", "image/jpeg", photo.file_size
    elif message.document and (message.document.mime_type or "").startswith(("image/", "application/pdf")):
        document = message.document
        file_id = document.file_id
        filename = document.file_name or "passport"
        mime = document.mime_type or "application/octet-stream"
        size = document.file_size
    else:
        raise _FileError(
            "Нужно фото документа — пришлите изображение или PDF.\n"
            "Лучше отправлять файлом, без сжатия: так текст остаётся читаемым."
        )

    if size and size > _MAX_FILE_BYTES:
        raise _FileError(
            "Файл больше 20 МБ — Telegram не отдаёт такие ботам.\n"
            "Сфотографируйте страницу отдельно или уменьшите размер."
        )
    return {"file_id": file_id, "filename": filename, "mime": mime}


async def _download_file(bot: Bot, ref: dict[str, str]) -> bytes:
    buffer = BytesIO()
    try:
        await bot.download(ref["file_id"], destination=buffer)
    except TelegramAPIError as exc:
        logger.warning("не удалось скачать документ из Telegram: %s", exc)
        raise _FileError("Не смог скачать файл из Telegram.") from exc
    return buffer.getvalue()


async def _clear_docs(state: FSMContext) -> None:
    """Сброс собранного комплекта — при новом заходе на верификацию."""
    await state.update_data(doc_main=None, doc_reg=None, uploaded=[])


# Что из комплекта Alfabit уже принял, по номеру телефона. Своя память нужна
# потому, что сервис этого не говорит: GET /payers/{phone} отдаёт
# required_documents — требования профиля, а не недостачу. Проверено на живом
# API: у approved-плательщика с обоими документами список ровно тот же, что у
# номера, который сервис видит впервые.
#
# Живёт до перезапуска процесса. На диск не пишем сознательно — это снова был
# бы телефон в файле, от чего 08.08 отказались. После рестарта недостача
# неизвестна, и остаётся ручной путь (кнопка «отправить заново»).
_uploaded_docs: dict[str, set[str]] = {}
_UPLOADED_LIMIT = 500


def _remember_upload(phone: str, doc_type: str) -> None:
    if phone not in _uploaded_docs and len(_uploaded_docs) >= _UPLOADED_LIMIT:
        _uploaded_docs.pop(next(iter(_uploaded_docs)))
    _uploaded_docs.setdefault(phone, set()).add(doc_type)


def _missing_docs(phone: str) -> list[str]:
    """Чего не хватает Alfabit по нашим сведениям. Пустой список — либо всё
    отправлено, либо мы про этот номер ничего не знаем."""
    known = _uploaded_docs.get(phone)
    if known is None:
        return []
    return [doc_type for doc_type in _DOC_SLOTS if doc_type not in known]


# Альбомы отклоняем; помним последний отклонённый на пользователя, чтобы на
# второй снимок того же альбома не отвечать второй раз.
_rejected_albums: dict[int, str] = {}
_ALBUM_LIMIT = 500


async def _reject_album(message: Message, user_id: int, doc_type: str) -> None:
    """Несколько снимков одним сообщением принять нельзя.

    Замок не даёт двум фото уехать в один doc_type, но порядок он не задаёт:
    задачи приходят к нему в порядке планировщика, а не по message_id. Если
    пара переставится, прописка уедет как главная страница — оба файла на
    месте, бот доволен, а разбирается человек на стороне Alfabit. Просить по
    одному дешевле, тем более что бот и так спрашивает страницы по очереди.
    """
    group = message.media_group_id
    if _rejected_albums.get(user_id) == group:
        # Второй снимок того же альбома — молча, иначе два упрёка подряд.
        return
    if len(_rejected_albums) >= _ALBUM_LIMIT:
        _rejected_albums.pop(next(iter(_rejected_albums)))
    _rejected_albums[user_id] = group

    await message.answer(
        "Пришлите снимки <b>по одному</b> — так я точно не перепутаю страницы "
        "местами.\n\n"
        f"Сейчас жду фото <b>{_DOC_SLOTS[doc_type][2]}</b>.",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard,
    )


async def _has_registration(state: FSMContext) -> bool:
    """Прописка уже в буфере или уже принята Alfabit.

    Бывает после сбоя на первой странице: её переспросили, человек прислал —
    и просить прописку второй раз незачем, она никуда не девалась.
    """
    data = await state.get_data()
    return data.get("doc_reg") is not None or _DOC_REGISTRATION in (data.get("uploaded") or [])


@router.message(PayStates.passport_main)
@router.message(PayStates.passport_registration)
async def handle_document(
    message: Message,
    state: FSMContext,
    bot: Bot,
    alfabit_client: AlfabitClient | None,
    alfabit_payer_ip: str | None,
    kyc_watcher: KycWatcher | None,
) -> None:
    """Оба шага загрузки — один хендлер: какую страницу сейчас ждём, решается
    ПОД замком, по актуальному состоянию.

    В Alfabit отсюда ничего не уходит: файл кладётся в буфер, а отправляется
    комплект целиком — см. _send_documents. Замок нужен, чтобы два быстрых
    сообщения подряд не заняли один и тот же слот.
    """
    if message.text and _is_exit(message.text):
        await _back_to_menu(message, state, "Отменено.")
        return

    user_id = message.from_user.id
    async with _user_lock(user_id):
        current = await state.get_state()
        if current == PayStates.passport_main.state:
            doc_type = _DOC_MAIN
        elif current == PayStates.passport_registration.state:
            doc_type = _DOC_REGISTRATION
        else:
            # Пока ждали очереди, сценарий закончился или его отменили. Молчать
            # тут нельзя — человек только что прислал документ.
            await message.answer(
                "Этот файл я <b>никуда не отправил</b> — сейчас документы не нужны.",
                parse_mode="HTML",
            )
            return

        if message.media_group_id is not None:
            await _reject_album(message, user_id, doc_type)
            return

        try:
            ref = _describe_file(message)
        except _FileError as exc:
            await message.answer(str(exc), reply_markup=_cancel_keyboard)
            return

        await state.update_data(**{_DOC_SLOTS[doc_type][0]: ref})

        if doc_type == _DOC_MAIN and not await _has_registration(state):
            await state.set_state(PayStates.passport_registration)
            await message.answer(
                "✅ Первая страница у меня.\n"
                "Теперь пришлите фото <b>страницы с пропиской</b> — отправлю оба "
                "документа сразу, как только комплект будет собран.",
                parse_mode="HTML",
                reply_markup=_cancel_keyboard,
            )
            return

        # Комплект собран — только теперь идём в Alfabit. Отправка идёт под тем
        # же замком: лишнее фото, присланное следом, дождётся конца и получит
        # ответ, а не уедет третьим запросом.
        await _send_documents(
            message, state, bot, alfabit_client, alfabit_payer_ip, user_id, kyc_watcher
        )


async def _ask_again(message: Message, state: FSMContext, doc_type: str, reason: str) -> None:
    """Переспрашиваем ровно тот документ, который не дошёл: остальной комплект
    остаётся в буфере, повторять его целиком человеку не нужно."""
    slot_key, step, human = _DOC_SLOTS[doc_type]
    await state.update_data(**{slot_key: None})
    await state.set_state(step)
    await message.answer(
        f"{reason}\n\nПришлите фото <b>{human}</b> ещё раз.",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard,
    )


async def _send_documents(
    message: Message,
    state: FSMContext,
    bot: Bot,
    alfabit_client: AlfabitClient | None,
    alfabit_payer_ip: str | None,
    user_id: int,
    watcher: KycWatcher | None = None,
) -> None:
    """Отправляет собранный комплект в Alfabit.

    Эндпоинт у Alfabit поштучный, батча нет — комплект всё равно уходит двумя
    запросами. Но окно «в сервисе половина комплекта» сжимается с человеческого
    масштаба (пока ищут вторую страницу — а то и никогда) до промежутка между
    двумя вызовами. И если второй упадёт, ссылки на файлы у нас на руках:
    переспрашиваем только тот документ, который не дошёл, уже принятые
    повторно не шлём (uploaded в данных FSM).
    """
    if alfabit_client is None or not alfabit_payer_ip:
        await _back_to_menu(message, state, "⚠️ Оплата сейчас недоступна.")
        return

    data = await state.get_data()
    phone: str = data["phone"]
    uploaded: list[str] = list(data.get("uploaded") or [])
    # Страницы, про которые Alfabit сказал «распозналось плохо». Поле приходит
    # только в ответе на загрузку, в GET /payers его нет — не соберём здесь,
    # больше не увидим нигде.
    low_conf: list[str] = []

    status = await message.answer("⏳ Отправляем документы…")

    for doc_type, (slot_key, _step, human) in _DOC_SLOTS.items():
        if doc_type in uploaded:
            continue
        ref = data.get(slot_key)
        if ref is None:
            await status.delete()
            await _ask_again(message, state, doc_type, f"Не хватает фото {human}.")
            return

        try:
            file_bytes = await _download_file(bot, ref)
        except _FileError as exc:
            await status.delete()
            await _ask_again(message, state, doc_type, str(exc))
            return

        try:
            result = await alfabit_client.upload_payer_document(
                phone,
                doc_type,
                file_bytes,
                ref["filename"],
                payer_ip=alfabit_payer_ip,
                content_type=ref["mime"],
            )
        except AlfabitError as exc:
            await status.delete()
            if _is_access_error(exc):
                # Прав у ключа нет — другое фото не поможет, не гоняем по кругу.
                logger.error(
                    "alfabit upload %s: доступ запрещён user=%s (%s)", doc_type, user_id, exc
                )
                await _back_to_menu(
                    message,
                    state,
                    "⚠️ Сервис верификации сейчас недоступен. Напишите менеджеру — "
                    "оплату оформим вручную.",
                )
                return
            logger.warning("alfabit upload %s failed: user=%s %s", doc_type, user_id, exc)
            if exc.code in TRANSPORT_ERROR_CODES:
                reason = "⚠️ Сервис верификации не отвечает — документ не отправился."
            else:
                reason = (
                    "⚠️ Сервис не принял документ. Нужно фото, где страница "
                    "попадает целиком и текст читается."
                )
            await _ask_again(message, state, doc_type, reason)
            return

        # След того, ЧТО именно уехало в Alfabit и что он об этом сказал.
        log_upload("upload ok", user_id=user_id, phone=phone, doc_type=doc_type, result=result)
        if result.get("low_confidence"):
            low_conf.append(doc_type)
        uploaded.append(doc_type)
        await state.update_data(uploaded=uploaded)
        # Данные FSM пропадут при «Отмене», а этот след — нет: по нему бот
        # узнает недостачу, даже если человек вернётся через час с чистого места.
        _remember_upload(phone, doc_type)

    await _park_until_kyc(
        state, watcher, user_id=user_id, chat_id=message.chat.id, phone=phone
    )
    await status.edit_text("⏳ Проверяем документы…")
    payer = await _poll_kyc(alfabit_client, phone, user_id=user_id)
    await status.delete()

    if payer is None:
        # Документы уже в Alfabit — важно вернуть меню с кнопкой статуса,
        # иначе человек остаётся с одной «Отменой» и без пути назад. Заявка при
        # этом под наблюдением: результат придёт сам, когда сервис ответит.
        await message.answer(
            "⚠️ Сервис проверки не отвечает, но документы отправлены.\n"
            "Как только он ответит, я пришлю результат сюда. Проверить вручную — "
            f"кнопка «{BTN_KYC_STATUS}» в меню.",
            reply_markup=kyc_menu_keyboard,
        )
        return

    log_payer("after upload", user_id=user_id, phone=phone, payer=payer)
    if payer.get("kyc_status") in ("approved", "rejected"):
        await _route_by_kyc(
            message,
            state,
            payer,
            user_id=user_id,
            phone=phone,
            watcher=watcher,
            low_conf=low_conf,
        )
        return

    await _pending_kyc_notice(message, user_id, payer, low_conf)


async def _poll_kyc(client: AlfabitClient, phone: str, *, user_id: int) -> dict | None:
    """Опрашивает статус, пока processing=true. None — если API недоступен.

    Короткая пачка на несколько секунд, как просит дока: распознавание обычно
    успевает закончиться, пока человек ещё смотрит в чат. Всё, что не уложилось,
    досматривает services/kyc_watch.py — раньше на этом месте наблюдение
    заканчивалось совсем.
    """
    payer: dict | None = None
    for attempt in range(_KYC_POLL_ATTEMPTS):
        try:
            payer = await client.get_payer(phone)
        except AlfabitError as exc:
            logger.warning("alfabit get_payer poll failed: user=%s %s", user_id, exc)
            return payer
        if not payer.get("processing"):
            return payer
        if attempt < _KYC_POLL_ATTEMPTS - 1:
            await asyncio.sleep(_KYC_POLL_DELAY)
    return payer


@router.callback_query(F.data == "pay:kyc")
async def recheck_kyc_legacy(callback: CallbackQuery) -> None:
    """Кнопка из старых сообщений: раньше статус проверялся инлайном, теперь —
    кнопкой в меню. Оставлено, чтобы у тех, кто уже в проверке, кнопка не висела
    мёртвой."""
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.answer(
        f"Проверить статус можно кнопкой «{BTN_KYC_STATUS}» в меню.",
        reply_markup=kyc_menu_keyboard,
    )


# ---- Шаг 3: сумма и платёж ------------------------------------------------


async def _ask_amount(
    message: Message, state: FSMContext, payer_name: str | None, user_id: int
) -> None:
    await state.update_data(payer_name=payer_name)
    await state.set_state(PayStates.amount)
    # Убираем «проверка ещё идёт» — её место занимает сообщение об успехе.
    await _clear_status(message.bot, message.chat.id, user_id)

    await message.answer(
        _amount_prompt(payer_name), parse_mode="HTML", reply_markup=_cancel_keyboard
    )


def _amount_prompt(payer_name: str | None) -> str:
    """Общий текст для обоих путей к шагу суммы: пользователь пришёл сам или
    верификация закрылась фоном и бот написал первым."""
    lines = ["✅ <b>Верификация пройдена.</b>"]
    if payer_name:
        lines.append(f"Плательщик: <b>{payer_name}</b>")
    lines += [
        "",
        "Лимиты:",
        "- не больше 100 тыс рублей за 1 операцию.",
        "- не больше 100 тыс рублей в общем за сутки всех операций.",
        "",
        "Сколько юаней вы хотите купить?",
    ]
    return "\n".join(lines)


@router.message(PayStates.amount)
async def handle_amount(
    message: Message,
    state: FSMContext,
    bot: Bot,
    rate_cache: RateCache,
    alfabit_client: AlfabitClient | None,
    alfabit_payer_ip: str | None,
    kyc_watcher: KycWatcher | None,
) -> None:
    text = (message.text or "").strip()
    if _is_exit(text):
        await _back_to_menu(message, state, "Отменено.")
        return
    if alfabit_client is None or not alfabit_payer_ip:
        await _back_to_menu(message, state, "⚠️ Оплата сейчас недоступна.")
        return

    try:
        cny = Decimal(text.replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        await message.answer("Введите количество юаней числом, например 5000.")
        return
    if cny <= 0:
        await message.answer("Количество юаней должно быть больше нуля.")
        return

    await rate_cache.refresh(_CACHE_KEY_ALFABIT, max_age_seconds=_RATE_MAX_AGE)
    entry = rate_cache.get(_CACHE_KEY_ALFABIT)
    if entry is None:
        await _back_to_menu(message, state, "⚠️ Курс сейчас недоступен. Попробуйте позже.")
        return

    price = yuan_price(entry.rate)
    rub = (cny * price).quantize(Decimal("0.01"))

    data = await state.get_data()
    phone: str = data["phone"]
    payer_name: str | None = data.get("payer_name")

    external_id = f"TG{message.from_user.id}-{int(time.time())}"
    creating = await message.answer("⏳ Формируем платёж…")

    try:
        payment = await alfabit_client.create_checkout_payment(
            amount=f"{rub:.2f}",
            payer_phone=phone,
            payer_ip=alfabit_payer_ip,
            external_payment_id=external_id,
            description=f"Покупка {cny} CNY",
        )
    except AlfabitError as exc:
        if _is_access_error(exc):
            # Внутренняя ошибка доступа — пользователю её текст ничего не даёт.
            logger.error(
                "alfabit create payment: доступ запрещён user=%s ext=%s (%s)",
                message.from_user.id, external_id, exc,
            )
            await creating.edit_text(
                "⚠️ Оплата сейчас недоступна. Напишите менеджеру — оформим вручную."
            )
            return
        logger.warning(
            "alfabit create payment failed: user=%s ext=%s %s",
            message.from_user.id, external_id, exc,
        )
        await creating.edit_text(f"⚠️ Не удалось создать платёж: {_user_message(exc)}")
        return

    if payment.get("status") == "kyc_required":
        # Профиль приёма требует верификацию, а плательщик её не прошёл. Сюда
        # попадаем, если статус успел измениться между approved и созданием
        # платежа — редко, но тогда возвращаемся в KYC-развилку.
        await creating.delete()
        payer = {
            "kyc_status": payment.get("payer_status", "not_started"),
            "processing": payment.get("processing"),
            "manual_review": payment.get("manual_review"),
        }
        log_payer("kyc_required on payment", user_id=message.from_user.id, phone=phone, payer=payer)
        await _route_by_kyc(
            message,
            state,
            payer,
            user_id=message.from_user.id,
            phone=phone,
            watcher=kyc_watcher,
        )
        return

    uid = payment.get("payment_uid")
    if not uid:
        logger.error(
            "alfabit create payment: нет payment_uid user=%s ext=%s ответ=%s",
            message.from_user.id, external_id, payment,
        )
        await creating.edit_text("⚠️ Сервис оплаты вернул неожиданный ответ. Напишите менеджеру.")
        return

    # Связка «наш номер заявки ↔ платёж у Alfabit ↔ пользователь»: без неё
    # платёж в кабинете не сопоставить с человеком в чате.
    logger.info(
        "payment created: user=%s ext=%s uid=%s amount=%s RUB (%s CNY)",
        message.from_user.id, external_id, uid, f"{rub:.2f}", cny,
    )
    _remember(uid, PaymentContext(
        user_id=message.from_user.id,
        username=message.from_user.username,
        phone=phone,
        payer_name=payer_name,
        cny=cny,
        rub=rub,
    ))
    await state.clear()

    qr = await _poll_qr(alfabit_client, uid)
    await creating.delete()
    await _send_qr(message, bot, uid, qr, cny, rub, price, payer_name)


async def _poll_qr(client: AlfabitClient, uid: str) -> dict:
    """Ждёт появления реквизитов СБП. Возвращает последний ответ по платежу."""
    payment: dict = {}
    for attempt in range(_QR_POLL_ATTEMPTS):
        try:
            payment = await client.get_checkout_payment(uid)
        except AlfabitError as exc:
            logger.warning("alfabit get payment failed: %s", exc)
            return payment
        if payment.get("qr_url") or payment.get("qr_payload"):
            return payment
        if attempt < _QR_POLL_ATTEMPTS - 1:
            await asyncio.sleep(_QR_POLL_DELAY)
    return payment


def _qr_image(payload: str) -> bytes | None:
    """PNG с QR-кодом. None — если библиотека недоступна."""
    try:
        import qrcode
    except ImportError:
        logger.warning("qrcode не установлен — отправляем только ссылку на оплату")
        return None

    buffer = BytesIO()
    qrcode.make(payload).save(buffer, format="PNG")
    return buffer.getvalue()


def _paid_keyboard(uid: str, qr_url: str | None) -> InlineKeyboardMarkup:
    rows = []
    if qr_url:
        rows.append([InlineKeyboardButton(text="🏦 Открыть в приложении банка", url=qr_url)])
    rows.append([InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"pay:check:{uid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_qr(
    message: Message,
    bot: Bot,
    uid: str,
    payment: dict,
    cny: Decimal,
    rub: Decimal,
    price: Decimal,
    payer_name: str | None,
) -> None:
    qr_url = payment.get("qr_url")
    qr_payload = payment.get("qr_payload") or qr_url

    lines = [
        "🧾 <b>Платёж создан</b>",
        "",
        f"Покупаете: <b>{cny} ¥</b>",
        f"К оплате: <b>{rub:.2f} ₽</b>  (курс {price:.2f} ₽ за юань)",
    ]
    if payer_name:
        lines += [
            "",
            f"⚠️ Оплата принимается <b>только от {payer_name}</b> — "
            "банк сверяет отправителя с паспортом.",
        ]
    lines += [
        "",
        "Отсканируйте QR в приложении банка или нажмите кнопку ниже.",
        "После оплаты нажмите «✅ Я оплатил».",
    ]
    caption = "\n".join(lines)
    keyboard = _paid_keyboard(uid, qr_url)

    if not qr_payload:
        await message.answer(
            caption + "\n\n⏳ Реквизиты СБП ещё формируются — нажмите «✅ Я оплатил» "
            "через минуту, чтобы обновить.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        await message.answer("Главное меню:", reply_markup=main_keyboard)
        return

    png = _qr_image(qr_payload)
    if png is None:
        await message.answer(caption, parse_mode="HTML", reply_markup=keyboard)
    else:
        await message.answer_photo(
            BufferedInputFile(png, filename="sbp_qr.png"),
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    await message.answer("Главное меню:", reply_markup=main_keyboard)


# ---- Шаг 4: «Я оплатил» ---------------------------------------------------


@router.callback_query(F.data.startswith("pay:check:"))
async def check_payment(
    callback: CallbackQuery,
    bot: Bot,
    alfabit_client: AlfabitClient | None,
    manager_tg_chat_id: int | None,
    manager_tg_username: str,
) -> None:
    uid = callback.data.removeprefix("pay:check:")
    if alfabit_client is None or callback.message is None:
        await callback.answer("Оплата сейчас недоступна.", show_alert=True)
        return

    try:
        payment = await alfabit_client.get_checkout_payment(uid)
    except AlfabitError as exc:
        logger.warning("alfabit check payment failed: %s", exc)
        await callback.answer("Сервис оплаты не отвечает, попробуйте ещё раз.", show_alert=True)
        return

    status = str(payment.get("status", "")).lower()

    if status in _PAID_STATUSES:
        await callback.answer()
        await callback.message.reply(
            "✅ <b>Оплата получена!</b>\n\n"
            f"Менеджер {_manager_link(manager_tg_username)} свяжется с вами "
            "для выдачи юаней.",
            parse_mode="HTML",
        )
        await _notify_manager(bot, manager_tg_chat_id, uid, payment, callback)
        _pending.pop(uid, None)
        return

    if status in _FAILED_STATUSES:
        await callback.answer()
        await callback.message.reply(
            f"❌ Платёж не прошёл (статус: {status}). Оформите оплату заново "
            f"или напишите менеджеру {_manager_link(manager_tg_username)}.",
            parse_mode="HTML",
        )
        _pending.pop(uid, None)
        return

    await callback.answer(
        "Оплата ещё не поступила. Банк может проводить перевод до нескольких минут — "
        "нажмите ещё раз чуть позже.",
        show_alert=True,
    )


def _manager_link(username: str) -> str:
    clean = username.lstrip("@")
    return f'<a href="https://t.me/{clean}">@{clean}</a>' if clean else "менеджер"


async def _notify_manager(
    bot: Bot,
    manager_chat_id: int | None,
    uid: str,
    payment: dict,
    callback: CallbackQuery,
) -> None:
    ctx = _pending.get(uid)
    user = callback.from_user

    lines = ["💰 <b>Оплата по QR + KYC получена</b>", ""]
    if ctx:
        lines += [
            f"Сумма: <b>{ctx.rub:.2f} ₽</b> за <b>{ctx.cny} ¥</b>",
            f"Телефон: <code>{ctx.phone}</code>",
        ]
        if ctx.payer_name:
            lines.append(f"Плательщик: <b>{ctx.payer_name}</b>")
    else:
        # Бот перезапускался — контекста нет, показываем что отдал API.
        lines.append(f"Сумма: <b>{payment.get('amount', '—')} {payment.get('currency', '')}</b>")

    handle = f"@{user.username}" if user.username else f"id {user.id}"
    lines += [
        f"Клиент: {handle} (id {user.id})",
        f"Платёж: <code>{uid}</code>",
    ]
    text = "\n".join(lines)

    if manager_chat_id is None:
        logger.warning("MANAGER_TG_CHAT_ID не задан, уведомление не отправлено:\n%s", text)
        return
    try:
        await bot.send_message(manager_chat_id, text, parse_mode="HTML")
    except Exception:
        logger.exception("Не удалось уведомить менеджера о платеже %s", uid)
