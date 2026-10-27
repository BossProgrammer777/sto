"""
Конфигурация бота: города, раскладки колонок, поля для сбора,
значения выпадающих списков (fallback), разбор переменных окружения.

Значения выпадающих списков (диаметр, тип авто, имена МОП) бот старается
читать прямо из data validation ячеек таблицы (см. sheets.py). Константы
ниже — это резервные значения на случай, если API-чтение недоступно.
"""

from __future__ import annotations

import os
import re
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
# На сколько месяцев вперёд показывать даты (0 = только текущий месяц,
# 1 = до конца следующего месяца). Реально показываются лишь даты,
# для которых уже существует месячный лист.
MONTHS_AHEAD = int(_get("MONTHS_AHEAD", "1"))

# Публичный API расписания для сайта (только чтение). Выключить: WEB_API=false.
WEB_API_ENABLED = _get("WEB_API", "true").lower() in ("1", "true", "yes", "да")


# Кто может пользоваться ботом (Telegram ID → имя). Остальным бот отвечает
# «нет доступа» и показывает их ID — его присылают админу, чтобы добавить сюда.
# Свой ID человек узнаёт, написав боту /chatid в личку.
OPERATORS: dict[int, str] = {
    6401842735: "Виталий Садурский (админ)",
    7492065202: "Сергей Майданец",
    7120671397: "Егор Александров",
    7859856603: "Сергей Симоненко",
    721843220: "Александр Симоненко",
    7737627021: "Артем Скороход",
    7662530066: "Вячеслав Шабатько",
    8029297178: "Олег Ольховский",
    5043367181: "Дмитрий Белоглазов",
    7273727635: "Вячеслав Нестеренко",
    8180858231: "Виталий Сюта",
    8261905684: "Сергей Дубина",
    7303853985: "Татьяна Кваша",
    8311096812: "Артем Бугайченко",
    993304667: "Вячеслав Разьва",
    7783840143: "Денис Татара",
}


def _load_allowed_ids() -> set[int]:
    """Доп. Telegram ID из окружения (через запятую) — добавить человека без правки кода."""
    raw = _get("ALLOWED_OPERATOR_IDS", "")
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(int(part))
    return ids


ALLOWED_OPERATOR_IDS: set[int] = set(OPERATORS) | _load_allowed_ids()


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
# Пока только Киев Деревообробна (оба поста); Харьков добавится позже —
# просто впишите ключ сюда.
TWO_POST_CITIES: set[str] = {"kyiv"}
BIG_DIAMETERS: set[str] = {"R20", "R21", "R22"}


def needs_two_slots(city: "City", diameter: str) -> bool:
    return branch_of(city).key in TWO_POST_CITIES and (diameter or "").strip() in BIG_DIAMETERS


@dataclass(frozen=True)
class City:
    key: str            # внутренний ключ
    title: str          # как показываем оператору
    layout: str         # simple | full
    # Как найти блок в таблице: слова из заголовка над блоком (в нижнем регистре).
    labels: tuple[str, ...]
    # Запасные слова — если по основным блок не нашёлся (напр. старый лист,
    # где над блоком было просто «Харьков»).
    alt_labels: tuple[str, ...] = ()
    # Подпись поста, если у филиала их несколько («ПОСТ №1»).
    post: str = ""
    # Дополнительный пост филиала: ключ основного поста. Своей кнопки нет —
    # бот сам выбирает пост; строки (даты/время) общие с основным постом.
    parent: str = ""

    @property
    def full_title(self) -> str:
        return f"{self.title} ({self.post})" if self.post else self.title


# Порядок = порядок блоков в таблице (слева направо) и кнопок в боте.
# Блок ищется по заголовку (labels); порядок — запасной вариант, если
# заголовки не читаются.
CITIES: dict[str, City] = {
    "kyiv": City("kyiv", "Киев Деревообробна", "simple",
                 ("деревообр",), alt_labels=("киев", "київ"), post="ПОСТ №1"),
    # Заголовок блока сравнивается и без пробелов/«№»: «ПОСТ №2» → «пост2».
    "kyiv_p2": City("kyiv_p2", "Киев Деревообробна", "simple",
                    ("пост2", "постn2", "постno2"), post="ПОСТ №2", parent="kyiv"),
    "kyiv_terekhina": City("kyiv_terekhina", "Киев О. Терьохіна 5/7", "simple",
                           ("терьох", "терех", "терёх")),
    "sofiyivska": City("sofiyivska", "Софиевская Борщагивка", "simple",
                       ("борщаг", "софиев", "софіїв", "софиїв")),
    "kharkiv": City("kharkiv", "Харьков Шевченко", "full", ("шевченк",),
                    alt_labels=("харьков", "харків")),
    "kharkiv_maidan": City("kharkiv_maidan", "Харьков Майдан Захисників", "full",
                           ("майдан",)),
    "dnipro": City("dnipro", "Днепр", "full", ("днепр", "дніпр")),
    "lviv": City("lviv", "Львов", "full", ("львов", "львів")),
}


def button_cities() -> list[City]:
    """Города для кнопок: доп. посты отдельной кнопки не имеют."""
    return [c for c in CITIES.values() if not c.parent]


def city_by_title(title: str) -> City | None:
    for c in button_cities():
        if title in (c.title, c.full_title):
            return c
    return None


def branch_of(city: City) -> City:
    """Основной пост филиала (для доп. поста — его parent)."""
    return CITIES[city.parent] if city.parent else city


def posts_of(city: City) -> list[City]:
    """Все посты филиала: основной + дополнительные."""
    return [city] + [c for c in CITIES.values() if c.parent == city.key]


# Филиал с двумя постами (Киев Деревообробна): R19 и больше — сначала ПОСТ №1,
# R13–R18 — сначала ПОСТ №2. Если на «своём» посту это время занято — бот
# берёт другой пост. РОЗВАЛ/СТО/без диаметра — как большие (сначала ПОСТ №1).
SECOND_POST_MAX_DIAMETER = 18


def posts_for(city: City, diameter: str) -> list[City]:
    """Посты филиала в порядке предпочтения для этого диаметра."""
    posts = posts_of(city)
    m = re.fullmatch(r"[RrРр]?\s*(\d{2})", (diameter or "").strip())
    if len(posts) > 1 and m and int(m.group(1)) <= SECOND_POST_MAX_DIAMETER:
        posts = posts[1:] + posts[:1]
    return posts


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
        "Бугайченко Артем", "Дубина Сергей", "Разьва Вячеслав",
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


# ─────────────────────── Хранение шин → группа филиала ───────────────────────
# Для поста можно подключить таблицу хранения шин и группу филиала в Telegram.
# Тогда при записи бот спрашивает «Клиент с хранения?», ищет его по ФИО и после
# записи пишет в группу: клиент, ряд/место, шины, дата и время.
# Переменные окружения (KEY — ключ поста в верхнем регистре, напр. KYIV):
#   STORAGE_<KEY>_SHEET — ID или ссылка на Google-таблицу хранения
#   STORAGE_<KEY>_TAB   — имя листа (необязательно, по умолчанию первый)
#   STORAGE_<KEY>_CHAT  — ID группы филиала (узнать: команда /chatid в группе)

@dataclass(frozen=True)
class StorageCfg:
    sheet_id: str
    tab: str
    chat_id: int | None


def _load_storage() -> dict[str, StorageCfg]:
    out: dict[str, StorageCfg] = {}
    for key in CITIES:
        prefix = f"STORAGE_{key.upper()}_"
        sheet = _get(prefix + "SHEET", "").strip()
        if not sheet:
            continue
        m = re.search(r"/d/([A-Za-z0-9_-]+)", sheet)  # можно вставить полную ссылку
        chat = _get(prefix + "CHAT", "").strip()
        out[key] = StorageCfg(m.group(1) if m else sheet, _get(prefix + "TAB", "").strip(),
                              int(chat) if chat else None)
    return out


STORAGE: dict[str, StorageCfg] = _load_storage()
