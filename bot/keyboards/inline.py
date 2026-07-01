from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

ITEMS_PER_PAGE = 5
_CHANNEL_URL = "https://t.me/vel_moto"


def catalog_list_keyboard(
    indexed_items: list[tuple[int, dict]], page: int, total_pages: int, list_msg_id: int
) -> InlineKeyboardMarkup:
    buttons = []
    for idx, item in indexed_items:
        название = item.get("название", "—")
        цена = item.get("цена", "")
        label = название
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"item:{idx}:{list_msg_id}")])

    buttons.append([InlineKeyboardButton(text="🏠 В меню", callback_data="cat:menu")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"cat:page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"cat:page:{page + 1}"))
    if nav:
        buttons.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def item_back_keyboard(page: int, list_msg_id: int, manager_username: str = "") -> InlineKeyboardMarkup:
    buttons = []
    buttons.append([InlineKeyboardButton(text="Подробнее", url=_CHANNEL_URL)])
    if manager_username:
        buttons.append([InlineKeyboardButton(
            text="📞 Связаться с менеджером",
            url=f"https://t.me/{manager_username.lstrip('@')}",
        )])
    buttons.append([InlineKeyboardButton(
        text="← Назад к каталогу",
        callback_data=f"cat:back:{page}:{list_msg_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
