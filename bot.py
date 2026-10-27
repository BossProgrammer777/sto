"""
Telegram-бот записи на шиномонтаж (помощник оператора/МОП).

Поток: /start → город → дата → свободное время → данные клиента → подтверждение.
Поле «МОП Запись» бот заполняет сам по Telegram ID оператора.

Стек: python-telegram-bot (async, ConversationHandler), gspread.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

from telegram import Update, ReplyKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters,
)

import config
import calendar_utils as cal
import keyboards as kb
from sheets import SheetsClient, SheetError
from storage import (StorageClient, StoredClient, client_details, client_label, group_message,
                     place_label, set_lines)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("shino-bot")

# Стейты диалога
(SELECT_CITY, SELECT_DATE, SELECT_TIME, COLLECT, CONFIRM,
 MENU, SEARCH_QUERY, SEARCH_PICK, ACTION, CONFIRM_DELETE,
 RS_CITY, RS_DATE, RS_TIME, SELECT_DIAMETER, STORAGE_ASK, STORAGE_PICK,
 STORAGE_SETS) = range(17)

sheets: SheetsClient  # инициализируется в main()
storage: StorageClient | None = None  # таблицы хранения (если настроены)


# ───────────────────────────── Вспомогательное ─────────────────────────────

def _deny_text(user_id: int) -> str:
    return ("⛔ У вас нет доступа к боту записи.\n"
            f"Ваш Telegram ID: {user_id} — перешлите его администратору, чтобы вас добавили.")


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
    post: config.City = ud.get("post") or ud["city"]  # пост, куда пишем
    lines = [
        "📋 <b>Проверьте запись:</b>",
        f"🏙 Город: <b>{post.full_title}</b>",
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
            lines.append(f"{label}: <b>{html.escape(str(d[key]))}</b>")
    st = ud.get("storage")
    if st:
        lines.append(f"📦 Хранение: <b>{html.escape(st.name)}</b>")
        lines.extend(f"<i>{html.escape(ln)}</i>" for ln in set_lines(st))
    return "\n".join(lines)


# ───────────────────────────── Хэндлеры ─────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not config.is_allowed(user.id):
        log.info("Отказ в доступе: id=%s username=%s", user.id, user.username)
        await update.message.reply_text(_deny_text(user.id))
        return ConversationHandler.END

    context.user_data.clear()
    # Сразу фоном прогреваем таблицу: пока оператор в меню/выбирает город,
    # месячный лист уже подгрузится и слоты покажутся мгновенно.
    asyncio.create_task(_warm_cache(cal.dates_through_month_end(config.MONTHS_AHEAD)))
    await update.message.reply_text(
        f"👋 Привет, <b>{user.first_name}</b>!\nЧто делаем?",
        parse_mode="HTML", reply_markup=kb.menu_kb(),
    )
    return MENU


async def menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка меню вне диалога (напр. после перезапуска бота) — сразу в дело."""
    if not config.is_allowed(update.effective_user.id):
        await update.message.reply_text(_deny_text(update.effective_user.id))
        return ConversationHandler.END
    return await menu(update, context)


async def lost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сообщение вне диалога. Бот хранит шаг записи в памяти и после перезапуска
    его забывает — раньше он в этом случае молчал. Теперь говорит и даёт меню."""
    if not config.is_allowed(update.effective_user.id):
        await update.message.reply_text(_deny_text(update.effective_user.id))
        return
    context.user_data.clear()
    await update.message.reply_text(
        "⚠️ Бот перезапускался или запись уже завершена — незаконченный шаг сброшен.\n"
        "Что делаем?", reply_markup=kb.menu_kb())


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
    dates = await _run(sheets.available_dates)
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
        free_posts = await _run(sheets.free_slots_by_post, city, d, False, diameter)
    except SheetError as e:
        log.warning("Ошибка чтения слотов: %s", e)
        await update.message.reply_text(f"⚠️ Не удалось прочитать расписание: {e}")
        return await _ask_diameter(update, context)
    if not free_posts:
        note = "\n(для больших шин R20+ нужны 2 свободных слота подряд)" \
            if config.needs_two_slots(city, diameter) else ""
        await update.message.reply_text(
            f"😔 Свободных слотов на эту дату нет.{note}\n"
            "Выберите другой диаметр или дату:")
        return await _ask_diameter(update, context)
    ud["free"], ud["free_posts"] = list(free_posts), free_posts
    big = " (займёт 2 слота)" if config.needs_two_slots(city, diameter) else ""
    posts = config.posts_of(city)
    hint = (f"\nПост бот выберет сам: R13–R{config.SECOND_POST_MAX_DIAMETER} — сначала "
            f"{posts[1].post}, остальное — {posts[0].post}." if len(posts) > 1 else "")
    await update.message.reply_text(
        f"⏰ Свободное время на <b>{cal.date_label(d)}</b>{big}:{hint}",
        parse_mode="HTML", reply_markup=kb.times_kb(ud["free"]))
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
    context.user_data["post"] = context.user_data["free_posts"][text]
    city: config.City = context.user_data["city"]
    context.user_data["fields"] = list(config.LAYOUTS[city.layout]["ask_fields"])
    context.user_data["field_idx"] = 0
    context.user_data["storage_asked"] = False
    context.user_data.pop("storage", None)
    # data уже содержит diameter (выбран на шаге диаметра) — не сбрасываем
    return await _ask_current_field(update, context)


async def _ask_current_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    idx = ud["field_idx"]
    fields = ud["fields"]

    if idx >= len(fields):
        if _storage_on(ud["city"]) and not ud.get("storage_asked"):
            return await _ask_storage(update, context)
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
        if _storage_on(ud["city"]):
            ud["storage_asked"] = False
            return await _ask_storage(update, context)
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
        free_posts = await _run(sheets.free_slots_by_post, city, ud["date"], True,
                                ud["data"].get("diameter", ""))  # свежее чтение
        if ud["time"] not in free_posts:
            ud["free"], ud["free_posts"] = list(free_posts), free_posts
            await update.message.reply_text(
                "😳 Пока вы заполняли, слот заняли. Выберите другое время:",
                reply_markup=kb.times_kb(ud["free"]),
            )
            return SELECT_TIME

        ud["post"] = free_posts[ud["time"]]  # пост по свежему чтению
        await _run(sheets.write_booking, ud["post"], ud["date"], ud["time"], ud["data"])
    except SheetError as e:
        log.error("Ошибка записи: %s", e)
        await update.message.reply_text(f"⚠️ Не удалось сохранить запись: {e}\n"
                                        "Попробуйте ещё раз позже.")
        return ConversationHandler.END
    except Exception as e:  # noqa: BLE001
        log.exception("Непредвиденная ошибка записи")
        await update.message.reply_text(f"⚠️ Ошибка при сохранении: {e}")
        return ConversationHandler.END

    note = await _notify_group(context, city, ud)
    await update.message.reply_text(
        "✅ <b>Записано!</b>\n\n" + _summary_text(ud) + note,
        parse_mode="HTML",
    )
    await update.message.reply_text("Для новой записи нажмите /start")
    return ConversationHandler.END


# ───────────────── Хранение шин → группа филиала ─────────────────

def _storage_on(city: config.City) -> bool:
    return storage is not None and city.key in config.STORAGE




async def _ask_storage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("storage", None)
    await update.message.reply_text(
        "📦 Клиент с хранения?\nВведите фамилию (можно часть) — найду его шины в таблице хранения.",
        reply_markup=kb.storage_ask_kb())
    return STORAGE_ASK


async def _storage_done(update: Update, context: ContextTypes.DEFAULT_TYPE, client) -> int:
    context.user_data["storage"] = client
    context.user_data["storage_asked"] = True
    return await _ask_current_field(update, context)  # → сводка и подтверждение


async def storage_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    text = update.message.text.strip()
    if text == kb.BTN_BACK:  # назад к последнему полю (МОП)
        ud["field_idx"] = len(ud["fields"]) - 1
        ud["data"].pop(ud["fields"][-1], None)
        return await _ask_current_field(update, context)
    if text == kb.BTN_NO_STORAGE:
        return await _storage_done(update, context, None)
    if len(text) < 2:
        await update.message.reply_text("Введите хотя бы 2 буквы фамилии.", reply_markup=kb.storage_ask_kb())
        return STORAGE_ASK
    try:
        hits, total = await _run(storage.search, ud["city"].key, text)
    except Exception as e:  # noqa: BLE001
        log.exception("Таблица хранения недоступна")
        await update.message.reply_text(
            f"⚠️ Не удалось прочитать таблицу хранения: {e}\nМожно продолжить «{kb.BTN_NO_STORAGE}».",
            reply_markup=kb.storage_ask_kb())
        return STORAGE_ASK
    if not hits:
        await update.message.reply_text(
            f"😔 «{text}» в хранении не нашёл. Попробуйте иначе или нажмите «{kb.BTN_NO_STORAGE}».",
            reply_markup=kb.storage_ask_kb())
        return STORAGE_ASK
    ud["_st_hits"] = hits
    ud["_st_labels"] = [client_label(c) for c in hits]
    more = f" (показаны первые {len(hits)} из {total} — уточните фамилию или имя)" if total > len(hits) else ""
    lines = [f"Найдено: {total}{more}."]
    for c in hits:  # сразу показываем, какие шины лежат у каждого
        lines += ["", f"👤 <b>{html.escape(c.name)}</b>", *(html.escape(ln) for ln in client_details(c))]
    lines += ["", "Выберите клиента:"]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    reply_markup=kb.storage_pick_kb(ud["_st_labels"]))
    return STORAGE_PICK


async def storage_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    text = update.message.text.strip()
    if text == kb.BTN_BACK:
        return await _ask_storage(update, context)
    if text == kb.BTN_NO_STORAGE:
        return await _storage_done(update, context, None)
    labels = ud.get("_st_labels", [])
    if text in labels:
        client = ud["_st_hits"][labels.index(text)]
        if len(client.sets) == 1:
            return await _storage_done(update, context, client)
        ud["_st_client"], ud["_st_sel"] = client, set()
        return await _ask_sets(update, context)
    return await storage_ask(update, context)  # ввели другую фамилию — ищем заново


def _set_labels(client, sel: set[int]) -> list[str]:
    out = []
    for i, s in enumerate(client.sets):
        where = f" · {place_label(s)}" if place_label(s) else ""
        out.append(f"{'✅' if i in sel else '⬜'} {i + 1}. {s.tires or 'без описания'}{where}")
    return out


async def _ask_sets(update: Update, context: ContextTypes.DEFAULT_TYPE, note: str = "") -> int:
    ud = context.user_data
    c = ud["_st_client"]
    text = note or (f"У клиента <b>{html.escape(c.name)}</b> на хранении комплектов: {len(c.sets)}.\n"
                    "Отметьте, какие шины привезти, и нажмите «✔️ Готово».")
    await update.message.reply_text(text, parse_mode="HTML",
                                    reply_markup=kb.storage_sets_kb(_set_labels(c, ud["_st_sel"])))
    return STORAGE_SETS


async def storage_sets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = context.user_data
    text = update.message.text.strip()
    c, sel = ud["_st_client"], ud["_st_sel"]
    if text == kb.BTN_BACK:
        await update.message.reply_text("Выберите клиента:", reply_markup=kb.storage_pick_kb(ud["_st_labels"]))
        return STORAGE_PICK
    if text == kb.BTN_NO_STORAGE:
        return await _storage_done(update, context, None)
    if text == kb.BTN_SETS_DONE:
        if not sel:
            return await _ask_sets(update, context, "Отметьте хотя бы один комплект.")
        return await _storage_done(update, context, StoredClient(c.name, tuple(c.sets[i] for i in sorted(sel))))
    m = re.match(r"^(?:✅|⬜)\s*(\d+)\.", text)
    if m and 0 < int(m.group(1)) <= len(c.sets):
        sel ^= {int(m.group(1)) - 1}  # отметить / снять отметку
        marked = ", ".join(str(i + 1) for i in sorted(sel)) or "ничего"
        return await _ask_sets(update, context, f"Отмечено: {marked}. Можно отметить ещё или нажать «✔️ Готово».")
    return await _ask_sets(update, context, "Выберите комплект кнопкой.")


async def _notify_group(context: ContextTypes.DEFAULT_TYPE, city: config.City, ud: dict) -> str:
    """Отправить запись с хранением в группу филиала. Возвращает пометку для оператора."""
    st = ud.get("storage")
    cfg = config.STORAGE.get(city.key)
    if not st or not cfg or not cfg.chat_id:
        return ""
    text = group_message(st, city=ud.get("post") or city, d=ud["date"], time_str=ud["time"],
                         data=ud["data"])
    try:
        await context.bot.send_message(cfg.chat_id, text)
        return "\n\n📨 Отправлено в группу филиала."
    except Exception as e:  # noqa: BLE001
        log.exception("Не удалось отправить в группу %s", cfg.chat_id)
        return f"\n\n⚠️ Запись сделана, но в группу не отправилось: {html.escape(str(e))}"


async def chat_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/chatid — показать ID текущего чата (чтобы подключить группу филиала)."""
    chat = update.effective_chat
    title = f"\n«{chat.title}»" if chat.title else ""
    await update.message.reply_text(f"ID этого чата: {chat.id}{title}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Отменено. Для новой записи — /start",
                                    reply_markup=kb.remove_kb())
    return ConversationHandler.END


# ───────────────── Поиск / удаление / перенос записи ─────────────────

def _booking_label(b) -> str:
    return f"{cal.date_button(b.date)} {b.time} — {b.city.full_title}"


def _booking_text(b) -> str:
    lines = [
        "📋 <b>Найдена запись:</b>",
        f"🏙 Город: <b>{b.city.full_title}</b>",
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
    dates = await _run(sheets.available_dates)
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
        free_posts = await _run(sheets.free_slots_by_post, city, d, False, diameter)
    except SheetError as e:
        await update.message.reply_text(f"⚠️ {e}\nВыберите другую дату:",
                                        reply_markup=kb.dates_kb(context.user_data["rs_dates"]))
        return RS_DATE
    if not free_posts:
        await update.message.reply_text("😔 Свободных слотов нет. Другую дату:",
                                        reply_markup=kb.dates_kb(context.user_data["rs_dates"]))
        return RS_DATE
    context.user_data["rs_free"] = list(free_posts)
    context.user_data["rs_free_posts"] = free_posts
    await update.message.reply_text(
        f"⏰ Свободное время на <b>{cal.date_label(d)}</b>:",
        parse_mode="HTML", reply_markup=kb.times_kb(context.user_data["rs_free"]))
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
    new_post = ud["rs_free_posts"][new_time]
    if (new_post.key, new_d, new_time) == (b.city.key, b.date, b.time):
        await update.message.reply_text("Это тот же слот — перенос не нужен.")
        return await _show_actions(update, b)

    await update.message.reply_text("🔁 Переношу…", reply_markup=kb.remove_kb())
    try:
        free_posts = await _run(sheets.free_slots_by_post, new_city, new_d, True,
                                b.data.get("diameter", ""))  # свежее
        if new_time not in free_posts:
            ud["rs_free"], ud["rs_free_posts"] = list(free_posts), free_posts
            await update.message.reply_text("😳 Слот уже заняли. Выберите другое время:",
                                            reply_markup=kb.times_kb(ud["rs_free"]))
            return RS_TIME
        new_post = free_posts[new_time]
        await _run(sheets.move_booking, b.city, b.date, b.time,
                   new_post, new_d, new_time, b.data)
    except Exception as e:  # noqa: BLE001
        log.exception("Ошибка переноса")
        await update.message.reply_text(f"⚠️ Не удалось перенести: {e}")
        return await _show_menu(update)

    await update.message.reply_text(
        "✅ <b>Перенесено!</b>\n"
        f"Было: {_booking_label(b)}\n"
        f"Стало: {cal.date_button(new_d)} {new_time} — {new_post.full_title}",
        parse_mode="HTML")
    return await _show_menu(update)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Ошибка в обработчике", exc_info=context.error)
    # Не молчим: в личке говорим оператору, что делать дальше.
    if (isinstance(update, Update) and update.effective_message and update.effective_chat
            and update.effective_chat.type == ChatType.PRIVATE):
        try:
            await update.effective_message.reply_text(
                "⚠️ Что-то пошло не так (возможно, Google-таблица не ответила).\n"
                "Попробуйте ещё раз или начните заново: /start")
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сообщить оператору об ошибке")


async def _post_init(app: Application) -> None:
    """Запустить API расписания для сайта в том же event loop, что и бот."""
    if not config.WEB_API_ENABLED:
        return
    try:
        from web_api import start_web_api
        app.bot_data["web_runner"] = await start_web_api(sheets)
    except Exception:  # noqa: BLE001
        log.exception("Web API не запустился — бот продолжает работать без него")


async def _post_shutdown(app: Application) -> None:
    runner = app.bot_data.get("web_runner")
    if runner is not None:
        await runner.cleanup()


def build_app() -> Application:
    app = (Application.builder().token(config.TELEGRAM_BOT_TOKEN)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    # Отмена должна срабатывать на любом шаге, поэтому ставим её первым
    # обработчиком в каждом стейте (иначе общий текстовый её перехватит).
    cancel_h = MessageHandler(filters.Regex(rf"^{re.escape(kb.BTN_CANCEL)}$"), cancel)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            # кнопки меню работают и вне диалога (после перезапуска бота)
            MessageHandler(filters.ChatType.PRIVATE & filters.Regex(
                rf"^({re.escape(kb.BTN_NEW)}|{re.escape(kb.BTN_FIND)})$"), menu_entry),
        ],
        states={
            MENU: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, menu)],
            SELECT_CITY: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_city)],
            SELECT_DATE: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_date)],
            SELECT_DIAMETER: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_diameter)],
            SELECT_TIME: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, select_time)],
            COLLECT: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, collect)],
            CONFIRM: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
            STORAGE_ASK: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, storage_ask)],
            STORAGE_PICK: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, storage_pick)],
            STORAGE_SETS: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, storage_sets)],
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
    app.add_handler(CommandHandler("chatid", chat_id_cmd))
    # Текст в личке вне диалога (бот перезапускался и забыл шаг) — не молчим.
    # Стоит после conv: во время диалога сообщения забирает он. В группах молчим.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, lost))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    global sheets
    log.info("Инициализация Google Sheets…")
    sheets = SheetsClient()
    if config.STORAGE:
        global storage
        storage = StorageClient(sheets.gc)
        log.info("Хранение шин подключено для: %s", ", ".join(config.STORAGE))
    log.info("Запуск бота (polling)…")
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
