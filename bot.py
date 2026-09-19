"""
Telegram-бот записи на шиномонтаж (помощник оператора/МОП).

Поток: /start → город → дата → свободное время → данные клиента → подтверждение.
Поле «МОП Запись» бот заполняет сам по Telegram ID оператора.

Стек: python-telegram-bot (async, ConversationHandler), gspread.
"""

from __future__ import annotations

import asyncio
import logging
import re

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters,
)

import config
import calendar_utils as cal
import keyboards as kb
from sheets import SheetsClient, SheetError

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("shino-bot")

# Стейты диалога
(SELECT_CITY, SELECT_DATE, SELECT_TIME, COLLECT, CONFIRM,
 MENU, SEARCH_QUERY, SEARCH_PICK, ACTION, CONFIRM_DELETE,
 RS_CITY, RS_DATE, RS_TIME, SELECT_DIAMETER) = range(14)

sheets: SheetsClient  # инициализируется в main()


# ───────────────────────────── Вспомогательное ─────────────────────────────

def _deny_text() -> str:
    return ("⛔ У вас нет доступа к боту записи.\n"
            "Обратитесь к администратору, чтобы добавить ваш Telegram ID.")


async def _run(fn, *args):
    """Выполнить блокирующий вызов gspread в отдельном потоке."""
    return await asyncio.to_thread(fn, *args)


async def _warm_cache(dates) -> None:
    """Фоново прогреть кеш таблицы (слоты + список МОП), чтобы шаги были мгновенны."""
    try:
        await asyncio.to_thread(sheets.prefetch, dates)
    except Exception as e:  # noqa: BLE001
        log.info("Прогрев кеша не удался: %s", e)


def _summary_text(ud: dict) -> str:
    city: config.City = ud["city"]
    lines = [
        "📋 <b>Проверьте запись:</b>",
        f"🏙 Город: <b>{city.title}</b>",
        f"📅 Дата: <b>{cal.date_label(ud['date'])}</b>",
        f"⏰ Время: <b>{ud['time']}</b>",
    ]
    d = ud["data"]
    # Показываем только заполненные поля (пропущенные не выводим).
    labels = [
        ("phone", "📞 Телефон"),
        ("order_number", "🧾 Заказ"),
        ("diameter", "⭕ Диаметр"),
        ("car_type", "🚗 Тип авто"),
        ("mop", "✍️ МОП Запись"),
    ]
    for key, label in labels:
        if d.get(key):
            lines.append(f"{label}: <b>{d[key]}</b>")
    return "\n".join(lines)


# ───────────────────────────── Хэндлеры ─────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not config.is_allowed(user.id):
        log.info("Отказ в доступе: id=%s username=%s", user.id, user.username)
        await update.message.reply_text(_deny_text())
        return ConversationHandler.END

    context.user_data.clear()
    # Сразу фоном прогреваем таблицу: пока оператор в меню/выбирает город,
    # месячный лист уже подгрузится и слоты покажутся мгновенно.
    asyncio.create_task(_warm_cache(cal.upcoming_dates(config.DAYS_AHEAD)))
    await update.message.reply_text(
        f"👋 Привет, <b>{user.first_name}</b>!\nЧто делаем?",
        parse_mode="HTML", reply_markup=kb.menu_kb(),
    )
    return MENU


async def _show_menu(update: Update) -> int:
    await update.message.reply_text("🏠 Меню. Что делаем?", reply_markup=kb.menu_kb())
    return MENU


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == kb.BTN_NEW:
        context.user_data.clear()
        await update.message.reply_text("🏙 Выберите город:", reply_markup=kb.cities_kb())
        return SELECT_CITY
    if text == kb.BTN_FIND:
        await update.message.reply_text(
            "🔍 Введите <b>номер заказа</b> или <b>номер телефона</b> клиента:",
            parse_mode="HTML", reply_markup=kb.search_prompt_kb())
        return SEARCH_QUERY
    await update.message.reply_text("Выберите действие кнопкой.", reply_markup=kb.menu_kb())
    return MENU


async def select_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    city = config.city_by_title(update.message.text.strip())
    if city is None:
        await update.message.reply_text("🤔 Выберите город кнопкой.", reply_markup=kb.cities_kb())
        return SELECT_CITY

    context.user_data["city"] = city
    dates = cal.upcoming_dates(config.DAYS_AHEAD)
    context.user_data["dates"] = dates
    await update.message.reply_text(
        f"🏙 <b>{city.title}</b>\n📅 Выберите дату:",
        parse_mode="HTML", reply_markup=kb.dates_kb(dates),
    )
    return SELECT_DATE


async def select_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == kb.BTN_BACK:
        await update.message.reply_text("🏙 Выберите город:", reply_markup=kb.cities_kb())
        return SELECT_CITY

    d = cal.parse_date_button(text, context.user_data["dates"])
    if d is None:
        await update.message.reply_text("🤔 Выберите дату кнопкой.",
                                        reply_markup=kb.dates_kb(context.user_data["dates"]))
        return SELECT_DATE

    context.user_data["date"] = d
    return await _ask_diameter(update, context)


async def _ask_diameter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Спросить диаметр ДО времени (от него зависит число занимаемых слотов)."""
    city: config.City = context.user_data["city"]
    values = await _run(sheets.read_validation_values, city, "Диаметр", "diameter")
    context.user_data["_diam_choices"] = values
    await update.message.reply_text(
        "⭕ Выберите диаметр:",
        reply_markup=kb.choice_kb(values, per_row=4, allow_skip=True))
    return SELECT_DIAMETER


async def _show_free_times(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Прочитать и показать свободные слоты (с учётом диаметра). Вернуть стейт."""
    ud = context.user_data
    city, d = ud["city"], ud["date"]
    diameter = ud["data"].get("diameter", "")
    await update.message.reply_text("⏳ Читаю свободные слоты…")
    try:
        free = await _run(sheets.read_free_slots, city, d, False, diameter)
    except SheetError as e:
        log.warning("Ошибка чтения слотов: %s", e)
        await update.message.reply_text(f"⚠️ Не удалось прочитать расписание: {e}")
        return await _ask_diameter(update, context)
    if not free:
        note = "\n(для больших шин R20+ нужны 2 свободных слота подряд)" \
            if config.needs_two_slots(city, diameter) else ""
        await update.message.reply_text(
            f"😔 Свободных слотов на эту дату нет.{note}\n"
            "Выберите другой диаметр или дату:")
        return await _ask_diameter(update, context)
    ud["free"] = free
    big = " (займёт 2 слота)" if config.needs_two_slots(city, diameter) else ""
    await update.message.reply_text(
        f"⏰ Свободное время на <b>{cal.date_label(d)}</b>{big}:",
        parse_mode="HTML", reply_markup=kb.times_kb(free))
    return SELECT_TIME


async def select_diameter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    text = update.message.text.strip()
    if text == kb.BTN_BACK:
        await update.message.reply_text("📅 Выберите дату:",
                                        reply_markup=kb.dates_kb(ud["dates"]))
        return SELECT_DATE
    if text == kb.BTN_SKIP:
        diameter = ""
    elif text in ud.get("_diam_choices", []):
        diameter = text
    else:
        await update.message.reply_text(
            "🤔 Выберите диаметр кнопкой.",
            reply_markup=kb.choice_kb(ud["_diam_choices"], per_row=4, allow_skip=True))
        return SELECT_DIAMETER

    ud["data"] = {"diameter": diameter}
    return await _show_free_times(update, context)


async def select_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == kb.BTN_BACK:
        return await _ask_diameter(update, context)

    if text not in context.user_data.get("free", []):
        await update.message.reply_text("🤔 Выберите время кнопкой.",
                                        reply_markup=kb.times_kb(context.user_data["free"]))
        return SELECT_TIME

    context.user_data["time"] = text
    city: config.City = context.user_data["city"]
    context.user_data["fields"] = list(config.LAYOUTS[city.layout]["ask_fields"])
    context.user_data["field_idx"] = 0
    # data уже содержит diameter (выбран на шаге диаметра) — не сбрасываем
    return await _ask_current_field(update, context)


async def _ask_current_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    idx = ud["field_idx"]
    fields = ud["fields"]

    if idx >= len(fields):
        await update.message.reply_text(_summary_text(ud), parse_mode="HTML",
                                        reply_markup=kb.confirm_kb())
        return CONFIRM

    field = fields[idx]
    meta = config.FIELDS[field]
    optional = not meta.get("required", False)
    if meta["kind"] == "choice":
        if meta.get("in_memory"):
            values = config.FALLBACK_VALIDATION[meta["validation"]]  # МОП — из памяти
        else:
            city: config.City = ud["city"]
            values = await _run(sheets.read_validation_values, city,
                                meta["sheet_column"], meta["validation"])
        ud["_choices"] = values
        ud["_per_row"] = meta.get("per_row", 3)
        await update.message.reply_text(
            meta["prompt"],
            reply_markup=kb.choice_kb(values, per_row=ud["_per_row"], allow_skip=optional))
    else:
        await update.message.reply_text(
            meta["prompt"], reply_markup=kb.text_kb(allow_skip=optional))
    return COLLECT


async def collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    text = update.message.text.strip()

    if text == kb.BTN_BACK:
        if ud["field_idx"] == 0:
            # с первого поля — назад к выбору времени
            await update.message.reply_text("⏰ Выберите время:",
                                            reply_markup=kb.times_kb(ud["free"]))
            return SELECT_TIME
        ud["field_idx"] -= 1
        ud["data"].pop(ud["fields"][ud["field_idx"]], None)
        return await _ask_current_field(update, context)

    idx = ud["field_idx"]
    field = ud["fields"][idx]
    meta = config.FIELDS[field]
    optional = not meta.get("required", False)

    # Пропуск необязательного поля.
    if text == kb.BTN_SKIP:
        if not optional:
            await update.message.reply_text("Это поле обязательно, пропустить нельзя.")
            return COLLECT
        ud["data"].pop(field, None)
        ud["field_idx"] += 1
        return await _ask_current_field(update, context)

    if meta["kind"] == "choice":
        if text not in ud.get("_choices", []):
            await update.message.reply_text(
                "🤔 Выберите значение кнопкой.",
                reply_markup=kb.choice_kb(ud["_choices"], per_row=ud.get("_per_row", 3),
                                          allow_skip=optional))
            return COLLECT
        ud["data"][field] = text
    else:
        if not text:
            await update.message.reply_text("🤔 Введите значение.")
            return COLLECT
        ud["data"][field] = text

    ud["field_idx"] += 1
    return await _ask_current_field(update, context)


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ud = context.user_data
    if text == kb.BTN_BACK:
        ud["field_idx"] = len(ud["fields"]) - 1
        ud["data"].pop(ud["fields"][-1], None)
        return await _ask_current_field(update, context)

    if text != "✅ Подтвердить":
        await update.message.reply_text("Нажмите «✅ Подтвердить» или «⬅️ Назад».",
                                        reply_markup=kb.confirm_kb())
        return CONFIRM

    city: config.City = ud["city"]

    await update.message.reply_text("💾 Сохраняю запись…", reply_markup=kb.remove_kb())
    try:
        # На всякий случай ещё раз проверим, что слот не заняли параллельно.
        free = await _run(sheets.read_free_slots, city, ud["date"], True,
                          ud["data"].get("diameter", ""))  # свежее чтение
        if ud["time"] not in free:
            await update.message.reply_text(
                "😳 Пока вы заполняли, слот заняли. Выберите другое время:",
                reply_markup=kb.times_kb(free),
            )
            ud["free"] = free
            return SELECT_TIME

        await _run(sheets.write_booking, city, ud["date"], ud["time"], ud["data"])
    except SheetError as e:
        log.error("Ошибка записи: %s", e)
        await update.message.reply_text(f"⚠️ Не удалось сохранить запись: {e}\n"
                                        "Попробуйте ещё раз позже.")
        return ConversationHandler.END
    except Exception as e:  # noqa: BLE001
        log.exception("Непредвиденная ошибка записи")
        await update.message.reply_text(f"⚠️ Ошибка при сохранении: {e}")
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ <b>Записано!</b>\n\n" + _summary_text(ud),
        parse_mode="HTML",
    )
    await update.message.reply_text("Для новой записи нажмите /start")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. Для новой записи — /start",
                                    reply_markup=kb.remove_kb())
    return ConversationHandler.END


# ───────────────── Поиск / удаление / перенос записи ─────────────────

def _booking_label(b) -> str:
    return f"{cal.date_button(b.date)} {b.time} — {b.city.title}"


def _booking_text(b) -> str:
    lines = [
        "📋 <b>Найдена запись:</b>",
        f"🏙 Город: <b>{b.city.title}</b>",
        f"📅 Дата: <b>{cal.date_label(b.date)}</b>",
        f"⏰ Время: <b>{b.time}</b>",
    ]
    d = b.data
    for key, label in [("phone", "📞 Телефон"), ("order_number", "🧾 Заказ"),
                       ("diameter", "⭕ Диаметр"), ("car_type", "🚗 Тип авто"),
                       ("mop", "✍️ МОП Запись")]:
        if d.get(key):
            lines.append(f"{label}: <b>{d[key]}</b>")
    return "\n".join(lines)


async def _show_actions(update: Update, b) -> int:
    await update.message.reply_text(
        _booking_text(b) + "\n\nЧто сделать с записью?",
        parse_mode="HTML", reply_markup=kb.actions_kb())
    return ACTION


async def search_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == kb.BTN_MENU:
        return await _show_menu(update)

    await update.message.reply_text("🔍 Ищу запись…")
    try:
        matches = await _run(sheets.search_bookings, text)
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка поиска")
        await update.message.reply_text(f"⚠️ Ошибка поиска: {e}",
                                        reply_markup=kb.search_prompt_kb())
        return SEARCH_QUERY

    if not matches:
        await update.message.reply_text(
            f"😔 По «{text}» ничего не найдено (ищу в текущем и следующем месяце).\n"
            "Проверьте номер и попробуйте снова:", reply_markup=kb.search_prompt_kb())
        return SEARCH_QUERY

    context.user_data["matches"] = matches
    if len(matches) == 1:
        context.user_data["selected"] = matches[0]
        return await _show_actions(update, matches[0])

    labels = [_booking_label(b) for b in matches]
    context.user_data["match_labels"] = labels
    await update.message.reply_text(
        f"Найдено записей: <b>{len(matches)}</b>. Выберите нужную:",
        parse_mode="HTML", reply_markup=kb.matches_kb(labels))
    return SEARCH_PICK


async def search_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == kb.BTN_MENU:
        return await _show_menu(update)
    labels = context.user_data.get("match_labels", [])
    if text not in labels:
        await update.message.reply_text("Выберите запись кнопкой.",
                                        reply_markup=kb.matches_kb(labels))
        return SEARCH_PICK
    b = context.user_data["matches"][labels.index(text)]
    context.user_data["selected"] = b
    return await _show_actions(update, b)


async def action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == kb.BTN_MENU:
        return await _show_menu(update)
    b = context.user_data.get("selected")
    if b is None:
        return await _show_menu(update)

    if text == kb.BTN_DELETE:
        await update.message.reply_text(
            "🗑 Удалить эту запись и освободить слот?",
            reply_markup=kb.confirm_delete_kb())
        return CONFIRM_DELETE
    if text == kb.BTN_MOVE:
        await update.message.reply_text(
            "🔁 Перенос. Выберите город нового слота:", reply_markup=kb.cities_kb())
        return RS_CITY
    await update.message.reply_text("Выберите действие кнопкой.", reply_markup=kb.actions_kb())
    return ACTION


async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    b = context.user_data.get("selected")
    if text == kb.BTN_BACK or b is None:
        if b is None:
            return await _show_menu(update)
        return await _show_actions(update, b)
    if text != kb.BTN_YES_DELETE:
        await update.message.reply_text("Нажмите «✅ Да, удалить» или «⬅️ Назад».",
                                        reply_markup=kb.confirm_delete_kb())
        return CONFIRM_DELETE

    await update.message.reply_text("🗑 Удаляю…", reply_markup=kb.remove_kb())
    try:
        await _run(sheets.cancel_booking, b.city, b.date, b.time)
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка удаления")
        await update.message.reply_text(f"⚠️ Не удалось удалить: {e}")
        return await _show_menu(update)
    await update.message.reply_text(
        f"✅ Запись удалена, слот освобождён:\n{_booking_label(b)}")
    return await _show_menu(update)


# ── Перенос: выбор нового города → даты → времени ──

async def rs_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == kb.BTN_MENU:
        return await _show_menu(update)
    city = config.city_by_title(text)
    if city is None:
        await update.message.reply_text("🤔 Выберите город кнопкой.", reply_markup=kb.cities_kb())
        return RS_CITY
    context.user_data["rs_city"] = city
    dates = cal.upcoming_dates(config.DAYS_AHEAD)
    context.user_data["rs_dates"] = dates
    await update.message.reply_text(
        f"🏙 <b>{city.title}</b>\n📅 Новая дата:", parse_mode="HTML",
        reply_markup=kb.dates_kb(dates))
    return RS_DATE


async def rs_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == kb.BTN_BACK:
        await update.message.reply_text("🏙 Выберите город:", reply_markup=kb.cities_kb())
        return RS_CITY
    d = cal.parse_date_button(text, context.user_data["rs_dates"])
    if d is None:
        await update.message.reply_text("🤔 Выберите дату кнопкой.",
                                        reply_markup=kb.dates_kb(context.user_data["rs_dates"]))
        return RS_DATE
    context.user_data["rs_date"] = d
    city = context.user_data["rs_city"]
    diameter = context.user_data["selected"].data.get("diameter", "")
    await update.message.reply_text("⏳ Читаю свободные слоты…")
    try:
        free = await _run(sheets.read_free_slots, city, d, False, diameter)
    except SheetError as e:
        await update.message.reply_text(f"⚠️ {e}\nВыберите другую дату:",
                                        reply_markup=kb.dates_kb(context.user_data["rs_dates"]))
        return RS_DATE
    if not free:
        await update.message.reply_text("😔 Свободных слотов нет. Другую дату:",
                                        reply_markup=kb.dates_kb(context.user_data["rs_dates"]))
        return RS_DATE
    context.user_data["rs_free"] = free
    await update.message.reply_text(
        f"⏰ Свободное время на <b>{cal.date_label(d)}</b>:",
        parse_mode="HTML", reply_markup=kb.times_kb(free))
    return RS_TIME


async def rs_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    ud = context.user_data
    if text == kb.BTN_BACK:
        await update.message.reply_text("📅 Новая дата:",
                                        reply_markup=kb.dates_kb(ud["rs_dates"]))
        return RS_DATE
    if text not in ud.get("rs_free", []):
        await update.message.reply_text("🤔 Выберите время кнопкой.",
                                        reply_markup=kb.times_kb(ud["rs_free"]))
        return RS_TIME

    b = ud["selected"]
    new_city, new_d, new_time = ud["rs_city"], ud["rs_date"], text
    if (new_city.key, new_d, new_time) == (b.city.key, b.date, b.time):
        await update.message.reply_text("Это тот же слот — перенос не нужен.")
        return await _show_actions(update, b)

    await update.message.reply_text("🔁 Переношу…", reply_markup=kb.remove_kb())
    try:
        free = await _run(sheets.read_free_slots, new_city, new_d, True,
                          b.data.get("diameter", ""))  # свежее
        if new_time not in free:
            await update.message.reply_text("😳 Слот уже заняли. Выберите другое время:",
                                            reply_markup=kb.times_kb(free))
            ud["rs_free"] = free
            return RS_TIME
        await _run(sheets.move_booking, b.city, b.date, b.time,
                   new_city, new_d, new_time, b.data)
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка переноса")
        await update.message.reply_text(f"⚠️ Не удалось перенести: {e}")
        return await _show_menu(update)

    await update.message.reply_text(
        "✅ <b>Перенесено!</b>\n"
        f"Было: {_booking_label(b)}\n"
        f"Стало: {cal.date_button(new_d)} {new_time} — {new_city.title}",
        parse_mode="HTML")
    return await _show_menu(update)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Ошибка в обработчике", exc_info=context.error)


def build_app() -> Application:
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    # Отмена должна срабатывать на любом шаге, поэтому ставим её первым
    # обработчиком в каждом стейте (иначе общий текстовый её перехватит).
    cancel_h = MessageHandler(filters.Regex(rf"^{re.escape(kb.BTN_CANCEL)}$"), cancel)
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MENU: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, menu)],
            SELECT_CITY: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_city)],
            SELECT_DATE: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_date)],
            SELECT_DIAMETER: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_diameter)],
            SELECT_TIME: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_time)],
            COLLECT: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, collect)],
            CONFIRM: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
            SEARCH_QUERY: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, search_query)],
            SEARCH_PICK: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, search_pick)],
            ACTION: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, action)],
            CONFIRM_DELETE: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete)],
            RS_CITY: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, rs_city)],
            RS_DATE: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, rs_date)],
            RS_TIME: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, rs_time)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(rf"^{re.escape(kb.BTN_CANCEL)}$"), cancel),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_error_handler(on_error)
    return app


def main() -> None:
    global sheets
    log.info("Инициализация Google Sheets…")
    sheets = SheetsClient()
    log.info("Запуск бота (polling)…")
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
