"""Оплата рублями по СБП с KYC (Alfabit checkout).

Порядок шагов сознательно отличается от схемы в доках alfabit: KYC делается
ДО расчёта суммы. В доках платёж создаётся первым и возвращает kyc_required,
но тогда зафиксированный курс жил бы весь KYC (а он может уйти на ручную
сверку — это не секунды). Здесь: телефон → KYC → и только потом количество
юаней, свежий курс, создание платежа и QR. Окно фиксации курса — от QR до
оплаты, минуты.

Состояние на сервере не храним (кроме подсказки для уведомления менеджера):
payment_uid кодируется прямо в callback_data кнопки «Я оплатил».
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from handlers.start import BTN_KYC_STATUS, MENU_BUTTONS, kyc_menu_keyboard, main_keyboard
from services.alfabit.client import AlfabitClient, AlfabitError
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

_cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=_CANCEL)]],
    resize_keyboard=True,
)


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


async def _park_until_kyc(state: FSMContext) -> None:
    """Документы на проверке: выходим из шага, чтобы пользователь свободно
    ходил по боту, но телефон в данных оставляем — по нему кнопка «Статус
    верификации» узнаёт, чью заявку проверять. Хранилище FSM — in-memory,
    так что связка живёт до перезапуска процесса; после него кнопка честно
    предложит начать оплату заново."""
    await state.set_state(None)


def _is_exit(text: str) -> bool:
    return text == _CANCEL or text in MENU_BUTTONS


def _is_access_error(exc: AlfabitError) -> bool:
    return exc.code in _ACCESS_ERROR_CODES or exc.status in (401, 403)


# ---- Статус верификации (кнопка в меню) -----------------------------------

# Последнее сообщение о статусе на пользователя: следующая проверка удаляет
# предыдущее, чтобы в чате не копилась лента одинаковых «проверка ещё идёт».
_status_messages: dict[int, int] = {}


async def _clear_status(message: Message, user_id: int) -> None:
    old = _status_messages.pop(user_id, None)
    if old is None:
        return
    try:
        await message.bot.delete_message(message.chat.id, old)
    except TelegramBadRequest:
        # Сообщение уже удалено пользователем или слишком старое — не мешает.
        pass


async def _replace_status(
    message: Message,
    user_id: int,
    text: str,
    reply_markup: ReplyKeyboardMarkup | None = None,
) -> None:
    await _clear_status(message, user_id)
    sent = await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
    _status_messages[user_id] = sent.message_id


async def _pending_kyc_notice(message: Message, user_id: int) -> None:
    """Документы ушли на проверку — дальше пользователь ждёт в меню."""
    await _replace_status(
        message,
        user_id,
        "⏳ Документы ушли на дополнительную проверку — это может занять время.\n\n"
        f"Нажмите «{BTN_KYC_STATUS}» в меню, чтобы узнать результат.",
        kyc_menu_keyboard,
    )


@router.message(F.text == BTN_KYC_STATUS)
async def kyc_status(
    message: Message,
    state: FSMContext,
    alfabit_client: AlfabitClient | None,
) -> None:
    """Зарегистрирован раньше FSM-хендлеров: кнопка живёт в меню и должна
    работать, в каком бы сценарии пользователь сейчас ни находился."""
    user_id = message.from_user.id
    phone = (await state.get_data()).get("phone")
    # Из текущего шага выходим, но данные не трогаем: телефон нужен этой же
    # кнопке при следующем нажатии.
    await state.set_state(None)

    if phone is None or alfabit_client is None:
        await state.clear()
        await _replace_status(
            message,
            user_id,
            "Не нашли заявку на верификацию — начните оплату заново.",
            main_keyboard,
        )
        return

    try:
        payer = await alfabit_client.get_payer(phone)
    except AlfabitError as exc:
        logger.warning("alfabit get_payer status failed: %s", exc)
        await _replace_status(
            message,
            user_id,
            "⚠️ Сервис проверки не отвечает. Попробуйте позже.",
            kyc_menu_keyboard,
        )
        return

    if payer.get("processing") or payer.get("kyc_status") == "pending":
        await _replace_status(message, user_id, "⏳ Проверка ещё идёт.", kyc_menu_keyboard)
        return

    await _route_by_kyc(message, state, payer, user_id=user_id, phone=phone)


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

    try:
        payer = await alfabit_client.get_payer(phone)
    except AlfabitError as exc:
        logger.warning("alfabit get_payer failed: %s", exc)
        await _back_to_menu(message, state, "⚠️ Сервис оплаты не отвечает. Попробуйте позже.")
        return

    await _route_by_kyc(message, state, payer, user_id=message.from_user.id, phone=phone)


async def _route_by_kyc(
    message: Message, state: FSMContext, payer: dict, *, user_id: int, phone: str
) -> None:
    """Развилка по статусу верификации плательщика."""
    await state.update_data(phone=phone)
    status = payer.get("kyc_status")

    if status == "approved":
        await _ask_amount(message, state, payer.get("expected_payer_name"), user_id)
        return

    if status == "rejected":
        await _clear_status(message, user_id)
        await _back_to_menu(
            message,
            state,
            "❌ Верификация не пройдена. Свяжитесь с менеджером — оплату оформим вручную.",
        )
        return

    if status == "pending":
        await _park_until_kyc(state)
        await _pending_kyc_notice(message, user_id)
        return

    # not_started — просим документы
    await state.set_state(PayStates.passport_main)
    await _clear_status(message, user_id)
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


async def _extract_file(message: Message, bot: Bot) -> tuple[bytes, str, str] | None:
    """(байты, имя файла, mime) из фото или документа. None — если не то прислали."""
    if message.photo:
        file_id = message.photo[-1].file_id
        filename, mime = "passport.jpg", "image/jpeg"
    elif message.document and (message.document.mime_type or "").startswith(("image/", "application/pdf")):
        file_id = message.document.file_id
        filename = message.document.file_name or "passport"
        mime = message.document.mime_type or "application/octet-stream"
    else:
        return None

    buffer = BytesIO()
    await bot.download(file_id, destination=buffer)
    return buffer.getvalue(), filename, mime


async def _upload_document(
    message: Message,
    state: FSMContext,
    bot: Bot,
    alfabit_client: AlfabitClient | None,
    alfabit_payer_ip: str | None,
    doc_type: str,
) -> dict | None:
    """Скачивает фото из Telegram и отдаёт его в alfabit. None — если не вышло."""
    if alfabit_client is None or not alfabit_payer_ip:
        await _back_to_menu(message, state, "⚠️ Оплата сейчас недоступна.")
        return None

    extracted = await _extract_file(message, bot)
    if extracted is None:
        await message.answer("Нужно фото документа — пришлите изображение или PDF.")
        return None

    file_bytes, filename, mime = extracted
    data = await state.get_data()
    phone = data["phone"]

    try:
        return await alfabit_client.upload_payer_document(
            phone,
            doc_type,
            file_bytes,
            filename,
            payer_ip=alfabit_payer_ip,
            content_type=mime,
        )
    except AlfabitError as exc:
        if _is_access_error(exc):
            # Прав у ключа нет — другое фото не поможет, не гоняем человека по кругу.
            logger.error("alfabit upload %s: доступ запрещён (%s)", doc_type, exc)
            await _back_to_menu(
                message,
                state,
                "⚠️ Сервис верификации сейчас недоступен. Напишите менеджеру — "
                "оплату оформим вручную.",
            )
            return None
        logger.warning("alfabit upload %s failed: %s", doc_type, exc)
        await message.answer(
            "⚠️ Сервис не принял документ. Попробуйте другое фото — "
            "чтобы страница попадала целиком и текст был читаемым."
        )
        return None


@router.message(PayStates.passport_main)
async def handle_passport_main(
    message: Message,
    state: FSMContext,
    bot: Bot,
    alfabit_client: AlfabitClient | None,
    alfabit_payer_ip: str | None,
) -> None:
    if message.text and _is_exit(message.text):
        await _back_to_menu(message, state, "Отменено.")
        return

    result = await _upload_document(
        message, state, bot, alfabit_client, alfabit_payer_ip, _DOC_MAIN
    )
    if result is None:
        return

    await state.set_state(PayStates.passport_registration)
    await message.answer(
        "✅ Принято.\nТеперь пришлите фото <b>страницы с пропиской</b>.",
        parse_mode="HTML",
        reply_markup=_cancel_keyboard,
    )


@router.message(PayStates.passport_registration)
async def handle_passport_registration(
    message: Message,
    state: FSMContext,
    bot: Bot,
    alfabit_client: AlfabitClient | None,
    alfabit_payer_ip: str | None,
) -> None:
    if message.text and _is_exit(message.text):
        await _back_to_menu(message, state, "Отменено.")
        return

    result = await _upload_document(
        message, state, bot, alfabit_client, alfabit_payer_ip, _DOC_REGISTRATION
    )
    if result is None:
        return

    phone = (await state.get_data())["phone"]
    user_id = message.from_user.id
    status_msg = await message.answer("⏳ Проверяем документы…")
    assert alfabit_client is not None
    payer = await _poll_kyc(alfabit_client, phone)

    await status_msg.delete()
    if payer is None:
        await message.answer("⚠️ Сервис проверки не отвечает. Попробуйте позже.")
        return
    if payer.get("kyc_status") in ("approved", "rejected"):
        await _route_by_kyc(message, state, payer, user_id=user_id, phone=phone)
        return

    await _park_until_kyc(state)
    await _pending_kyc_notice(message, user_id)


async def _poll_kyc(client: AlfabitClient, phone: str) -> dict | None:
    """Опрашивает статус, пока processing=true. None — если API недоступен."""
    payer: dict | None = None
    for attempt in range(_KYC_POLL_ATTEMPTS):
        try:
            payer = await client.get_payer(phone)
        except AlfabitError as exc:
            logger.warning("alfabit get_payer poll failed: %s", exc)
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
    await _clear_status(message, user_id)

    lines = ["✅ <b>Верификация пройдена.</b>"]
    if payer_name:
        lines.append(f"Плательщик: <b>{payer_name}</b>")
    lines += [
        "",
        "Сколько юаней вы хотите купить? Введите число, например <code>5000</code>.",
    ]
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=_cancel_keyboard)


@router.message(PayStates.amount)
async def handle_amount(
    message: Message,
    state: FSMContext,
    bot: Bot,
    rate_cache: RateCache,
    alfabit_client: AlfabitClient | None,
    alfabit_payer_ip: str | None,
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
            logger.error("alfabit create payment: доступ запрещён (%s)", exc)
            await creating.edit_text(
                "⚠️ Оплата сейчас недоступна. Напишите менеджеру — оформим вручную."
            )
            return
        logger.warning("alfabit create payment failed: %s", exc)
        await creating.edit_text(f"⚠️ Не удалось создать платёж: {exc.message or exc.code}")
        return

    if payment.get("status") == "kyc_required":
        await creating.delete()
        await _route_by_kyc(
            message,
            state,
            {"kyc_status": payment.get("payer_status", "not_started")},
            user_id=message.from_user.id,
            phone=phone,
        )
        return

    uid = payment.get("payment_uid")
    if not uid:
        logger.error("alfabit create payment: нет payment_uid в ответе %s", payment)
        await creating.edit_text("⚠️ Сервис оплаты вернул неожиданный ответ. Напишите менеджеру.")
        return

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
