import logging
import math

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from handlers.start import main_keyboard
from keyboards.inline import ITEMS_PER_PAGE, catalog_list_keyboard, item_back_keyboard
from services.catalog_cache import CatalogCache
from services.drive_photos import DrivePhotoCache

logger = logging.getLogger(__name__)
router = Router()

_MAX_DESC_LEN = 900


def _build_caption(item: dict) -> str:
    название = item.get("название", "—")
    описание = item.get("описание", "")

    if len(описание) > _MAX_DESC_LEN:
        описание = описание[:_MAX_DESC_LEN].rstrip() + "…"

    parts = [f"<b>{название}</b>"]
    if описание:
        parts.append(описание)
    return "\n\n".join(parts)


def _build_list_text(page: int, total_pages: int) -> str:
    return f"🛍 <b>Каталог товаров</b> — стр. {page}/{total_pages}\nВыберите товар:"


def _page_items(items: list[dict], page: int) -> list[tuple[int, dict]]:
    start = (page - 1) * ITEMS_PER_PAGE
    return list(enumerate(items))[start : start + ITEMS_PER_PAGE]


@router.message(F.text == "🛍 Каталог товаров")
async def cmd_catalog(message: Message, catalog_cache: CatalogCache) -> None:
    items = catalog_cache.all_items()
    if not items:
        await message.answer("Каталог пока пуст.")
        return
    total_pages = max(1, math.ceil(len(items) / ITEMS_PER_PAGE))
    sent = await message.answer(_build_list_text(1, total_pages), parse_mode="HTML")
    keyboard = catalog_list_keyboard(_page_items(items, 1), 1, total_pages, sent.message_id)
    await sent.edit_reply_markup(reply_markup=keyboard)


@router.callback_query(F.data.startswith("cat:page:"))
async def cb_catalog_page(query: CallbackQuery, catalog_cache: CatalogCache) -> None:
    page = int(query.data.split(":")[2])
    list_msg_id = query.message.message_id
    await query.answer()

    items = catalog_cache.all_items()
    if not items:
        await query.message.edit_text("Каталог пока пуст.")
        return
    total_pages = max(1, math.ceil(len(items) / ITEMS_PER_PAGE))
    page = max(1, min(page, total_pages))
    keyboard = catalog_list_keyboard(_page_items(items, page), page, total_pages, list_msg_id)
    await query.message.edit_text(
        _build_list_text(page, total_pages), parse_mode="HTML", reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("cat:back:"))
async def cb_catalog_back(query: CallbackQuery, catalog_cache: CatalogCache) -> None:
    parts = query.data.split(":")
    page = int(parts[2])
    list_msg_id = int(parts[3])
    await query.answer()
    await query.message.delete()

    items = catalog_cache.all_items()
    if not items:
        return
    total_pages = max(1, math.ceil(len(items) / ITEMS_PER_PAGE))
    page = max(1, min(page, total_pages))
    keyboard = catalog_list_keyboard(_page_items(items, page), page, total_pages, list_msg_id)
    try:
        await query.bot.edit_message_text(
            _build_list_text(page, total_pages),
            chat_id=query.message.chat.id,
            message_id=list_msg_id,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data.startswith("item:"))
async def cb_item(
    query: CallbackQuery,
    catalog_cache: CatalogCache,
    drive_cache: DrivePhotoCache,
    manager_tg_username: str,
) -> None:
    parts = query.data.split(":")
    idx = int(parts[1])
    list_msg_id = int(parts[2])
    await query.answer()

    item = catalog_cache.get_item(idx)
    if not item:
        await query.message.answer("Товар не найден.")
        return

    page = idx // ITEMS_PER_PAGE + 1
    keyboard = item_back_keyboard(page, list_msg_id, manager_tg_username)
    caption = _build_caption(item)

    photo_url = item.get("фото", "")
    if photo_url:
        try:
            photo = await drive_cache.get_photo(photo_url)
            msg = await query.message.answer_photo(
                photo, caption=caption, parse_mode="HTML", reply_markup=keyboard
            )
            if isinstance(photo, BufferedInputFile):
                drive_cache.store_file_id(photo_url, msg.photo[-1].file_id)
            return
        except Exception:
            logger.exception("Failed to load photo for item %s", idx)

    await query.message.answer(caption, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "cat:menu")
async def cb_catalog_menu(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.delete()
    await query.message.answer("Выберите действие:", reply_markup=main_keyboard)


@router.callback_query(F.data == "noop")
async def cb_noop(query: CallbackQuery) -> None:
    await query.answer()
