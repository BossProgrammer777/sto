"""Построители ReplyKeyboardMarkup для шагов бота."""

from __future__ import annotations

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove

import config
import calendar_utils as cal

BTN_CANCEL = "❌ Отмена"
BTN_BACK = "⬅️ Назад"
BTN_SKIP = "⏭ Пропустить"
BTN_NEW = "➕ Новая запись"
BTN_FIND = "🔍 Найти / изменить запись"
BTN_DELETE = "🗑 Удалить запись"
BTN_MOVE = "🔁 Перенести"
BTN_MENU = "⬅️ В меню"
BTN_YES_DELETE = "✅ Да, удалить"
BTN_NO_STORAGE = "⏭ Без хранения"
BTN_SETS_DONE = "✔️ Готово"
BTN_OTHER_DIAM = "⭕ Другой диаметр"


def _chunk(items: list[str], per_row: int) -> list[list[str]]:
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]


def cities_kb() -> ReplyKeyboardMarkup:
    # По 2 города в ряд, чтобы все (включая Львов) помещались на один экран.
    rows = _chunk([c.title for c in config.button_cities()], 2)
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def busy_button(d) -> str:
    """Кнопка дня, где всё занято (при новой записи её нельзя выбрать)."""
    return f"❌ {cal.date_button(d)} занято"


def dates_kb(dates, busy=frozenset()) -> ReplyKeyboardMarkup:
    rows = _chunk([busy_button(d) if d in busy else cal.date_button(d) for d in dates], 3)
    rows.append([BTN_BACK, BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def redate_kb(dates) -> ReplyKeyboardMarkup:
    """На выбранную дату мест нет: другая дата — или сменить диаметр."""
    rows = _chunk([cal.date_button(d) for d in dates], 3)
    rows.append([BTN_OTHER_DIAM, BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def times_kb(times: list[str]) -> ReplyKeyboardMarkup:
    rows = _chunk(times, 4)
    rows.append([BTN_BACK, BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def choice_kb(values: list[str], per_row: int = 3, allow_skip: bool = False) -> ReplyKeyboardMarkup:
    rows = _chunk(values, per_row)
    if allow_skip:
        rows.append([BTN_SKIP])
    rows.append([BTN_BACK, BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def text_kb(allow_skip: bool = False) -> ReplyKeyboardMarkup:
    rows = []
    if allow_skip:
        rows.append([BTN_SKIP])
    rows.append([BTN_BACK, BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["✅ Подтвердить"], [BTN_BACK, BTN_CANCEL]],
        resize_keyboard=True, one_time_keyboard=True,
    )


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


# ── Меню и поиск/изменение записи ──

def menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_NEW], [BTN_FIND]],
                               resize_keyboard=True, one_time_keyboard=True)


def search_prompt_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_MENU], [BTN_CANCEL]], resize_keyboard=True)


def matches_kb(labels: list[str]) -> ReplyKeyboardMarkup:
    rows = [[lbl] for lbl in labels]
    rows.append([BTN_MENU, BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def actions_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_DELETE], [BTN_MOVE], [BTN_MENU]],
                               resize_keyboard=True, one_time_keyboard=True)


def confirm_delete_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_YES_DELETE], [BTN_BACK]],
                               resize_keyboard=True, one_time_keyboard=True)


# ── Хранение шин ──

def storage_ask_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_NO_STORAGE], [BTN_BACK, BTN_CANCEL]], resize_keyboard=True)


def storage_pick_kb(labels: list[str]) -> ReplyKeyboardMarkup:
    rows = [[lbl] for lbl in labels]
    rows.append([BTN_NO_STORAGE])
    rows.append([BTN_BACK, BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def storage_sets_kb(labels: list[str]) -> ReplyKeyboardMarkup:
    rows = [[lbl] for lbl in labels]
    rows.append([BTN_SETS_DONE])
    rows.append([BTN_NO_STORAGE])
    rows.append([BTN_BACK, BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)
