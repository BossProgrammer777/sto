"""Построители ReplyKeyboardMarkup для шагов бота."""

from __future__ import annotations

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove

import config
import calendar_utils as cal

BTN_CANCEL = "❌ Отмена"
BTN_BACK = "⬅️ Назад"
BTN_SKIP = "⏭ Пропустить"


def _chunk(items: list[str], per_row: int) -> list[list[str]]:
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]


def cities_kb() -> ReplyKeyboardMarkup:
    # По 2 города в ряд, чтобы все (включая Львов) помещались на один экран.
    rows = _chunk([c.title for c in config.CITIES.values()], 2)
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def dates_kb(dates) -> ReplyKeyboardMarkup:
    rows = _chunk([cal.date_button(d) for d in dates], 3)
    rows.append([BTN_BACK, BTN_CANCEL])
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
