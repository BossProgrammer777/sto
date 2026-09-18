"""
Конфигурация бота: города, раскладки колонок, поля для сбора,
значения выпадающих списков (fallback), разбор переменных окружения.

Значения выпадающих списков (диаметр, тип авто, имена МОП) бот старается
читать прямо из data validation ячеек таблицы (см. sheets.py). Константы
ниже — это резервные значения на случай, если API-чтение недоступно.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

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
FLAT_SHEET_NAME = _get("FLAT_SHEET_NAME", "Записи_Бот")
DAYS_AHEAD = int(_get("DAYS_AHEAD", "14"))
WRITE_TO_GRID = _get("WRITE_TO_GRID", "false").lower() in ("1", "true", "yes", "да")


def _load_operators() -> dict[int, str]:
    """Маппинг Telegram ID -> имя МОП. Ключи = whitelist доступа."""
    raw = _get("OPERATORS_JSON", "{}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"OPERATORS_JSON — некорректный JSON: {e}") from e
    return {int(k): str(v).strip() for k, v in data.items()}


OPERATORS: dict[int, str] = _load_operators()


def _load_allowed_ids() -> set[int]:
    ids = set(OPERATORS.keys())
    extra = _get("ALLOWED_OPERATOR_IDS", "")
    for part in extra.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(int(part))
    return ids


ALLOWED_OPERATOR_IDS: set[int] = _load_allowed_ids()


def operator_name(telegram_id: int) -> str | None:
    """Имя МОП по Telegram ID для авто-заполнения колонки «МОП Запись»."""
    return OPERATORS.get(telegram_id)


def is_allowed(telegram_id: int) -> bool:
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

LAYOUTS: dict[str, dict] = {
    "simple": {
        # Колонки блока по порядку (индекс = смещение от начала блока).
        "columns": ["Время", "Номер заказа", "Диаметр", "МОП Запись", "ПРОЗВОН МОП ШМ"],
        # Какие поля бот спрашивает у оператора и в каком порядке.
        "ask_fields": ["order_number", "diameter"],
    },
    "full": {
        "columns": [
            "Время", "Номер тел.", "Номер заказа", "Диаметр", "ТИП Авто",
            "МОП Запись", "Ориент. стоимость (4шт)", "ПРОЗВОН МОП ШМ",
        ],
        "ask_fields": ["phone", "order_number", "diameter", "car_type", "price"],
    },
}


@dataclass(frozen=True)
class City:
    key: str            # внутренний ключ
    title: str          # как показываем оператору
    layout: str         # simple | full
    sheet_label: str    # как город подписан в самой таблице (для поиска блока)


# Порядок = порядок кнопок в боте.
CITIES: dict[str, City] = {
    "kyiv": City("kyiv", "Киев (ПОСТ №1)", "simple", "Киев"),
    "sofiyivska": City("sofiyivska", "Софиевская Борщагивка", "simple", "Софиевская Борщагивка"),
    "kharkiv": City("kharkiv", "Харьков", "full", "Харьков"),
    "dnipro": City("dnipro", "Днепр", "full", "Днепр"),
    "lviv": City("lviv", "Львов", "full", "Львов"),
}


def city_by_title(title: str) -> City | None:
    for c in CITIES.values():
        if c.title == title:
            return c
    return None


def layout_of(city: City) -> dict:
    return LAYOUTS[city.layout]


def column_index(city: City, column_name: str) -> int:
    """Смещение колонки внутри блока города (0 = колонка «Время»)."""
    return LAYOUTS[city.layout]["columns"].index(column_name)


# ─────────────────────────── Поля сбора данных ───────────────────────────
# Метаданные полей, которые бот спрашивает у оператора.
# kind: text — свободный ввод; choice — выбор из выпадающего списка (кнопки).

FIELDS: dict[str, dict] = {
    "phone":        {"prompt": "📞 Введите номер телефона клиента:", "kind": "text",
                     "sheet_column": "Номер тел."},
    "order_number": {"prompt": "🧾 Введите номер заказа:", "kind": "text",
                     "sheet_column": "Номер заказа"},
    "diameter":     {"prompt": "⭕ Выберите диаметр:", "kind": "choice",
                     "sheet_column": "Диаметр", "validation": "diameter"},
    "car_type":     {"prompt": "🚗 Выберите тип авто:", "kind": "choice",
                     "sheet_column": "ТИП Авто", "validation": "car_type"},
    "price":        {"prompt": "💵 Введите ориент. стоимость (4 шт):", "kind": "text",
                     "sheet_column": "Ориент. стоимость (4шт)"},
}


# ─────────────────────── Выпадающие списки (fallback) ───────────────────────
# Пытаемся читать из data validation; это резерв, если чтение не удалось.

FALLBACK_VALIDATION: dict[str, list[str]] = {
    "diameter": ["R13", "R14", "R15", "R16", "R17", "R18", "R19", "R20",
                 "R21", "R22", "РОЗВАЛ", "СТО"],
    "car_type": ["Легковой", "Кроссовер/Паркетник", "Микроавтобус", "Большой Бус"],
    "mop": [
        "Симоненко Сергей", "Александров Егор", "Рябцев Александр", "Ольховский Олег",
        "Шабатько Вячеслав", "Борисов Дмитрий", "Широколава Екатерина", "Симоненко Алекс",
        "Давыдов Александр", "Скороход Артём", "Кучерявый Александр", "Майданец Сергей",
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

# Формат времени в таблице. ВНИМАНИЕ: уточняется на калибровке ("9:00" vs "09:00").
TIME_FORMAT_LEADING_ZERO = False
