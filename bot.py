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
SELECT_CITY, SELECT_DATE, SELECT_TIME, COLLECT, CONFIRM = range(5)

sheets: SheetsClient  # инициализируется в main()


# ───────────────────────────── Вспомогательное ─────────────────────────────

def _deny_text() -> str:
    return ("⛔ У вас нет доступа к боту записи.\n"
            "Обратитесь к администратору, чтобы добавить ваш Telegram ID.")


async def _run(fn, *args):
    """Выполнить блокирующий вызов gspread в отдельном потоке."""
    return await asyncio.to_thread(fn, *args)


async def _warm_cache(dates) -> None:
    """Фоново прогреть кеш таблицы, чтобы слоты показывались мгновенно."""
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
    if "phone" in d:
        lines.append(f"📞 Телефон: <b>{d['phone']}</b>")
    lines.append(f"🧾 Заказ: <b>{d.get('order_number', '')}</b>")
    lines.append(f"⭕ Диаметр: <b>{d.get('diameter', '')}</b>")
    if "car_type" in d:
        lines.append(f"🚗 Тип авто: <b>{d['car_type']}</b>")
    if "price" in d:
        lines.append(f"💵 Стоимость: <b>{d['price']}</b>")
    lines.append(f"✍️ МОП Запись: <b>{d.get('mop', '')}</b>")
    return "\n".join(lines)


# ───────────────────────────── Хэндлеры ─────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not config.is_allowed(user.id):
        log.info("Отказ в доступе: id=%s username=%s", user.id, user.username)
        await update.message.reply_text(_deny_text())
        return ConversationHandler.END

    context.user_data.clear()
    # Сразу фоном прогреваем таблицу: пока оператор жмёт город/дату,
    # месячный лист уже подгрузится и слоты покажутся мгновенно.
    asyncio.create_task(_warm_cache(cal.upcoming_dates(config.DAYS_AHEAD)))
    await update.message.reply_text(
        f"👋 Привет, <b>{user.first_name}</b>!\n"
        "Запишем клиента. Выберите город:",
        parse_mode="HTML", reply_markup=kb.cities_kb(),
    )
    return SELECT_CITY


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
    city: config.City = context.user_data["city"]
    await update.message.reply_text("⏳ Читаю свободные слоты…")
    try:
        free = await _run(sheets.read_free_slots, city, d)
    except SheetError as e:
        log.warning("Ошибка чтения слотов: %s", e)
        await update.message.reply_text(
            f"⚠️ Не удалось прочитать расписание: {e}\nПопробуйте другую дату или позже.",
            reply_markup=kb.dates_kb(context.user_data["dates"]),
        )
        return SELECT_DATE

    if not free:
        await update.message.reply_text(
            "😔 На эту дату свободных слотов нет. Выберите другую:",
            reply_markup=kb.dates_kb(context.user_data["dates"]),
        )
        return SELECT_DATE

    context.user_data["free"] = free
    await update.message.reply_text(
        f"⏰ Свободное время на <b>{cal.date_label(d)}</b>:",
        parse_mode="HTML", reply_markup=kb.times_kb(free),
    )
    return SELECT_TIME


async def select_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == kb.BTN_BACK:
        await update.message.reply_text("📅 Выберите дату:",
                                        reply_markup=kb.dates_kb(context.user_data["dates"]))
        return SELECT_DATE

    if text not in context.user_data.get("free", []):
        await update.message.reply_text("🤔 Выберите время кнопкой.",
                                        reply_markup=kb.times_kb(context.user_data["free"]))
        return SELECT_TIME

    context.user_data["time"] = text
    city: config.City = context.user_data["city"]
    context.user_data["fields"] = list(config.LAYOUTS[city.layout]["ask_fields"])
    context.user_data["field_idx"] = 0
    context.user_data["data"] = {}
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
    if meta["kind"] == "choice":
        city: config.City = ud["city"]
        values = await _run(sheets.read_validation_values, city,
                            meta["sheet_column"], meta["validation"])
        ud["_choices"] = values
        await update.message.reply_text(meta["prompt"], reply_markup=kb.choice_kb(values))
    else:
        await update.message.reply_text(meta["prompt"], reply_markup=_text_kb())
    return COLLECT


def _text_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[kb.BTN_BACK, kb.BTN_CANCEL]], resize_keyboard=True)


async def collect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    text = update.message.text.strip()

    if text == kb.BTN_BACK:
        ud["field_idx"] = max(0, ud["field_idx"] - 1)
        # если ушли до первого поля — вернёмся к выбору времени
        if ud["field_idx"] == 0 and update.message.text == kb.BTN_BACK and not ud["data"]:
            await update.message.reply_text("⏰ Выберите время:",
                                            reply_markup=kb.times_kb(ud["free"]))
            return SELECT_TIME
        # снять последнее сохранённое значение
        prev_field = ud["fields"][ud["field_idx"]]
        ud["data"].pop(prev_field, None)
        return await _ask_current_field(update, context)

    idx = ud["field_idx"]
    field = ud["fields"][idx]
    meta = config.FIELDS[field]

    if meta["kind"] == "choice":
        if text not in ud.get("_choices", []):
            await update.message.reply_text("🤔 Выберите значение кнопкой.",
                                            reply_markup=kb.choice_kb(ud["_choices"]))
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
        free = await _run(sheets.read_free_slots, city, ud["date"], True)  # свежее чтение
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
            SELECT_CITY: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_city)],
            SELECT_DATE: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_date)],
            SELECT_TIME: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_time)],
            COLLECT: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, collect)],
            CONFIRM: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
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
