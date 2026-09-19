"""
Конфигурация бота: города, раскладки колонок, поля для сбора,
значения выпадающих списков (fallback), разбор переменных окружения.

Значения выпадающих списков (диаметр, тип авто, имена МОП) бот старается
читать прямо из data validation ячеек таблицы (см. sheets.py). Константы
ниже — это резервные значения на случай, если API-чтение недоступно.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # локально подтягивает .env; на Railway переменные уже в окружении


# ─────────────────────────── Переменные окружения ───────────────────────────

def _get(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return val or ""


TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", required=True)
SPREADSHEET_ID = _get("SPREADSHEET_ID", required=True)
GOOGLE_CREDENTIALS_JSON = _get("GOOGLE_CREDENTIALS_JSON", required=True)
DAYS_AHEAD = int(_get("DAYS_AHEAD", "14"))


def _load_allowed_ids() -> set[int]:
    """Whitelist Telegram ID операторов с доступом к боту (через запятую)."""
    raw = _get("ALLOWED_OPERATOR_IDS", "")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(int(part))
    return ids


ALLOWED_OPERATOR_IDS: set[int] = _load_allowed_ids()


def is_allowed(telegram_id: int) -> bool:
    # Пустой whitelist = доступ открыт всем (временный режим; впишите ID —
    # и доступ станет ограниченным).
    if not ALLOWED_OPERATOR_IDS:
        return True
    return telegram_id in ALLOWED_OPERATOR_IDS


# ─────────────────────────── Раскладки колонок ───────────────────────────
#
# На месячном листе каждый город — блок колонок. Порядок колонок внутри блока:
#
#  simple (Киев, Софиевская):
#     Время | Номер заказа | Диаметр | МОП Запись | ПРОЗВОН МОП ШМ
#  full (Харьков, Днепр, Львов):
#     Время | Номер тел. | Номер заказа | Диаметр | ТИП Авто | МОП Запись |
#     Ориент. стоимость (4шт) | ПРОЗВОН МОП ШМ
#
# ask_fields — какие поля бот спрашивает у оператора и в каком порядке.
# «МОП Запись» оператор выбирает из списка (поле mop).

# ВНИМАНИЕ: список columns описывает ВСЕ колонки блока по порядку (для точной
# адресации ячеек), даже те, что бот не заполняет («Ориент. стоимость», «ПРОЗВОН»).
# ask_fields — только то, что бот реально спрашивает у оператора.
# Диаметр спрашивается ОТДЕЛЬНЫМ шагом ДО выбора времени (от него зависит,
# сколько слотов занимает запись), поэтому его нет в ask_fields.
LAYOUTS: dict[str, dict] = {
    "simple": {
        "columns": ["Время", "Номер заказа", "Диаметр", "МОП Запись", "ПРОЗВОН МОП ШМ"],
        "ask_fields": ["order_number", "mop"],
    },
    "full": {
        "columns": [
            "Время", "Номер тел.", "Номер заказа", "Диаметр", "ТИП Авто",
            "МОП Запись", "Ориент. стоимость (4шт)", "ПРОЗВОН МОП ШМ",
        ],
        "ask_fields": ["phone", "order_number", "car_type", "mop"],
    },
}

# Города, где большие шины (R20+) занимают 2 подряд слота («2 поста»).
# Пока только Киев; Харьков добавится позже — просто впишите ключ сюда.
TWO_POST_CITIES: set[str] = {"kyiv"}
BIG_DIAMETERS: set[str] = {"R20", "R21", "R22"}


def needs_two_slots(city: "City", diameter: str) -> bool:
    return city.key in TWO_POST_CITIES and (diameter or "").strip() in BIG_DIAMETERS


@dataclass(frozen=True)
class City:
    key: str            # внутренний ключ
    title: str          # как показываем оператору
    layout: str         # simple | full
    sheet_label: str    # как город подписан в самой таблице


# Порядок = порядок блоков в таблице (слева направо) и кнопок в боте.
CITIES: dict[str, City] = {
    "kyiv": City("kyiv", "Киев (ПОСТ №1)", "simple", "Киев"),
    "sofiyivska": City("sofiyivska", "Софиевская Борщагивка", "simple", "Софиївська"),
    "kharkiv": City("kharkiv", "Харьков", "full", "Харьков"),
    "dnipro": City("dnipro", "Днепр", "full", "Днепр"),
    "lviv": City("lviv", "Львов", "full", "Львов"),
}


def city_by_title(title: str) -> City | None:
    for c in CITIES.values():
        if c.title == title:
            return c
    return None


def column_index(city: City, column_name: str) -> int:
    """Смещение колонки внутри блока города (0 = колонка «Время»)."""
    return LAYOUTS[city.layout]["columns"].index(column_name)


# ─────────────────────────── Поля сбора данных ───────────────────────────
# kind: text — свободный ввод; choice — выбор из выпадающего списка (кнопки).
# required: обязательное поле (нельзя пропустить). Обязательны только
#   номер заказа, телефон и МОП; остальные можно пропустить кнопкой.
# in_memory: значения берём из константы в памяти, не читая таблицу (быстрее).

FIELDS: dict[str, dict] = {
    "phone":        {"prompt": "📞 Введите номер телефона клиента:", "kind": "text",
                     "sheet_column": "Номер тел.", "required": True},
    "order_number": {"prompt": "🧾 Введите номер заказа:", "kind": "text",
                     "sheet_column": "Номер заказа", "required": True},
    "diameter":     {"prompt": "⭕ Выберите диаметр:", "kind": "choice",
                     "sheet_column": "Диаметр", "validation": "diameter", "required": False},
    "car_type":     {"prompt": "🚗 Выберите тип авто:", "kind": "choice",
                     "sheet_column": "ТИП Авто", "validation": "car_type", "required": False},
    "mop":          {"prompt": "✍️ Кто делает запись (МОП)?", "kind": "choice",
                     "sheet_column": "МОП Запись", "validation": "mop", "required": True,
                     "per_row": 5, "in_memory": True},
}


# ─────────────────────── Выпадающие списки (fallback) ───────────────────────
# Пытаемся читать из data validation; это резерв, если чтение не удалось.

FALLBACK_VALIDATION: dict[str, list[str]] = {
    "diameter": ["R13", "R14", "R15", "R16", "R17", "R18", "R19", "R20",
                 "R21", "R22", "РОЗВАЛ", "СТО"],
    "car_type": ["Легковой", "Кроссовер/Паркетник", "Микроавтобус", "Большой Бус"],
    # Актуальный список операторов (задан заказчиком). Написание — как в
    # таблице, чтобы значение проходило проверку данных при записи.
    "mop": [
        "Симоненко Сергей", "Александров Егор", "Ольховский Олег", "Шабатько Вячеслав",
        "Широколава Екатерина", "Симоненко Алекс", "Давыдов Александр", "Скороход Артем",
        "Кучерявый Александр", "Майданец Сергей", "Татара Денис", "Садурский Виталий",
        "Кваша Татьяна", "Сюта Виталий", "Нестеренко Вячеслав", "Белоглазов Дмитрий",
        "Бугайченко Артем",
    ],
}


# ─────────────────────── Календарь / расписание ───────────────────────

# Названия месячных листов: «ШМ Сентябрь 26», «ШМ Август 26» ...
MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

# Сокращения дней недели для меток дат: «ЧТ 03.09.2026» (Пн=0).
DOW_RU = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]

# Слоты времени: 9:00 .. 19:30 с шагом 30 минут.
WORK_START_HOUR = 9
WORK_END_HOUR = 19
WORK_END_MINUTE = 30
SLOT_STEP_MIN = 30

# Формат времени в таблице: «9:00» (без ведущего нуля) — подтверждено калибровкой.
TIME_FORMAT_LEADING_ZERO = False
