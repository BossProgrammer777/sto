"""
Слой доступа к Google Sheets (gspread + Sheets API) с ретраями.

Реальная структура «красивой» простыни (по калибровке):
  • Каждый город — блок колонок; между блоками скрытые колонки-разделители.
      Киев A–E (simple), Софиевская R–V (simple),
      Харьков Z–AG (full), Днепр AL–AS (full), Львов дальше (full).
  • Строка 1 — название города, строка 2 — дата, строка 3 — шапка колонок.
  • Дата разбита: день недели («ВТ») в колонке «Время», сама дата
    («01.09.2026») — в объединённой ячейке рядом. Матчим по «дд.мм.гггг».
  • Блок даты ≈ 26 строк: дата → шапка → 2 строки-заглушки → 22 слота
    9:00–19:30 → следующая дата. Время без ведущего нуля («9:00»).
  • Киев Деревообробна — 2 поста: ПОСТ №1 (A–E) и ПОСТ №2 справа от него
    (Номер заказа | Диаметр | МОП Запись | ПРОЗВОН). Своей колонки «Время» у
    ПОСТ №2 может не быть — строки общие с ПОСТ №1, время берём оттуда.

Определение свободного слота (по решению заказчика):
  СВОБОДНО = ячейка «Номер заказа» БЕЛАЯ и ВСЯ строка блока пустая (все
  колонки, кроме «Время»). Не считаются: формулы и ошибки вида #N/A, а также
  скрытые колонки, которые бот не заполняет.
  Красный (занято/закрыто) и оранжевый (недоступно) — не свободны.

Запись (подход А): бот пишет данные прямо в ячейки нужного слота простыни.
Значения + цвета читаем ОДНИМ запросом на месячный лист и кешируем.
"""

from __future__ import annotations

import json
import logging
import re
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config
import calendar_utils as cal

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
_DATE_IN_CELL_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
_DOW_TOKENS = {"пн", "вт", "ср", "чт", "пт", "сб", "вс", "нд"}
_SERIAL_RE = re.compile(r"(\d{2})[,.]?(\d{3})(?:[.,]0+)?")
_SERIAL_EPOCH = date(1899, 12, 30)  # нулевой день дат Google Sheets
_ERROR_RE = re.compile(r"^#(N/A|REF!|VALUE!|DIV/0!|NAME\?|NUM!|NULL!|ERROR!)")
_logged_maps: set[str] = set()

MONTH_CACHE_TTL = 45      # сек — кеш прочитанного месячного листа (значения+цвета)
VAL_CACHE_TTL = 6 * 3600  # сек — кеш значений выпадающих списков (меняются редко)
TITLES_CACHE_TTL = 300    # сек — кеш списка листов (чтобы замечать новый месяц)


def _row_date(md: "MonthData", r: int, start_col: int, width: int) -> str | None:
    """Дата строки-заголовка дня в блоке («дд.мм.гггг») или None.
    Понимает и ячейку, у которой слетел формат даты и видно число (напр. 46300)."""
    for c in range(start_col, start_col + width):
        m = _DATE_IN_CELL_RE.search(md.val(r, c))
        if m:
            return m.group(1)
    if md.val(r, start_col).strip().lower() in _DOW_TOKENS:
        raw = md.val(r, start_col + 1).replace("\xa0", "").replace(" ", "").strip()
        m = _SERIAL_RE.fullmatch(raw)
        if m:
            n = int(m.group(1) + m.group(2))
            if 40000 <= n <= 60000:
                return (_SERIAL_EPOCH + timedelta(days=n)).strftime("%d.%m.%Y")
    return None


class SheetError(Exception):
    """Понятная ошибка уровня таблицы для показа/логов."""


def _network_retry(fn):
    return retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        retry=retry_if_exception_type((gspread.exceptions.APIError, ConnectionError, TimeoutError)),
    )(fn)


def _norm_time(s: str) -> str | None:
    m = _TIME_RE.match(s.strip())
    if not m:
        return None
    return f"{int(m.group(1))}:{m.group(2)}"


def _has_label(text: str, tokens: tuple[str, ...]) -> bool:
    """Есть ли в заголовке одно из слов (сравниваем и без пробелов/«№»)."""
    t = text.lower()
    compact = re.sub(r"[\s№#]", "", t)
    return any(tok in t or tok in compact for tok in tokens)


def _time_key(t: str) -> tuple[int, int]:
    h, m = t.split(":")
    return int(h), int(m)


def _grid_free(x: dict | None) -> bool:
    return bool(x) and x["c"] == "W" and not x["b"]


def _is_white(color: dict | None) -> bool:
    """True, если фон белый / без заливки (иначе — цветной: красный, оранжевый…)."""
    if not color:
        return True  # нет заливки = белый
    r = color.get("red", 0.0)
    g = color.get("green", 0.0)
    b = color.get("blue", 0.0)
    return r >= 0.85 and g >= 0.85 and b >= 0.85


def _is_red(color: dict | None) -> bool:
    """True, если фон красный (закрыто). Оранжевый под это НЕ подпадает."""
    if not color:
        return False
    r = color.get("red", 0.0)
    g = color.get("green", 0.0)
    b = color.get("blue", 0.0)
    return r >= 0.7 and g <= 0.5 and b <= 0.5


@dataclass
class MonthData:
    """Разобранный месячный лист: значения и цвета фона."""
    title: str
    values: list[list[str]]
    colors: list[list[dict | None]]
    hidden: set[int] = field(default_factory=set)                # скрытые колонки
    formulas: set[tuple[int, int]] = field(default_factory=set)  # ячейки с формулой

    def val(self, r: int, c: int) -> str:
        if 0 <= r < len(self.values) and 0 <= c < len(self.values[r]):
            return self.values[r][c]
        return ""

    def color(self, r: int, c: int) -> dict | None:
        if 0 <= r < len(self.colors) and 0 <= c < len(self.colors[r]):
            return self.colors[r][c]
        return None

    @property
    def nrows(self) -> int:
        return len(self.values)


@dataclass
class Booking:
    """Найденная запись в таблице."""
    month_title: str
    city: "config.City"
    date: date
    time: str
    data: dict  # order_number, phone, diameter, car_type, mop


class SheetsClient:
    def __init__(self) -> None:
        info = json.loads(config.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.ss = self.gc.open_by_key(config.SPREADSHEET_ID)
        self._month_cache: dict[str, tuple[float, MonthData]] = {}
        self._val_cache: dict[str, tuple[float, list[str]]] = {}
        self._titles_cache: tuple[float, set[str]] | None = None

    # ───────────────────────── Листы месяцев ─────────────────────────

    @_network_retry
    def _sheet_titles(self) -> set[str]:
        """Названия всех листов таблицы (кеш ~5 мин). Так бот сам «видит»
        новый месячный лист, как только его добавили."""
        if self._titles_cache and (_time.time() - self._titles_cache[0]) < TITLES_CACHE_TTL:
            return self._titles_cache[1]
        meta = self.ss.fetch_sheet_metadata({"fields": "sheets(properties(title))"})
        titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
        self._titles_cache = (_time.time(), titles)
        return titles

    def available_dates(self) -> list[date]:
        """Даты до конца месяца(ев), для которых месячный лист уже существует."""
        candidates = cal.dates_through_month_end(config.MONTHS_AHEAD)
        try:
            titles = self._sheet_titles()
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось получить список листов: %s", e)
            return candidates  # не блокируем работу
        dates = [d for d in candidates if cal.month_sheet_name(d) in titles]
        # если ни один лист не совпал — показываем хотя бы текущий месяц
        return dates or cal.dates_through_month_end(0)

    # ───────────────────────── Чтение месячного листа ─────────────────────────

    @_network_retry
    def _load_month(self, d: date, force: bool = False) -> MonthData:
        name = cal.month_sheet_name(d)
        cached = self._month_cache.get(name)
        if not force and cached and (_time.time() - cached[0]) < MONTH_CACHE_TTL:
            return cached[1]

        try:
            meta = self.ss.fetch_sheet_metadata({
                "includeGridData": True,
                "ranges": [name],
                "fields": ("sheets(properties(title),data(columnMetadata(hiddenByUser),"
                           "rowData(values(formattedValue,userEnteredValue(formulaValue),"
                           "effectiveFormat(backgroundColor)))))"),
            })
        except gspread.exceptions.APIError as e:
            raise SheetError(f"Не удалось прочитать лист «{name}»: {e}") from e

        sheets = meta.get("sheets", [])
        if not sheets:
            raise SheetError(f"Месячный лист «{name}» не найден")

        grid = sheets[0].get("data", [{}])[0]
        hidden = {i for i, m in enumerate(grid.get("columnMetadata", []) or [])
                  if m.get("hiddenByUser")}
        values: list[list[str]] = []
        colors: list[list[dict | None]] = []
        formulas: set[tuple[int, int]] = set()
        for r, row in enumerate(grid.get("rowData", [])):
            cells = row.get("values", []) or []
            vrow, crow = [], []
            for c, cell in enumerate(cells):
                vrow.append(cell.get("formattedValue", "") or "")
                fmt = cell.get("effectiveFormat") or {}
                crow.append(fmt.get("backgroundColor"))
                if (cell.get("userEnteredValue") or {}).get("formulaValue"):
                    formulas.add((r, c))
            values.append(vrow)
            colors.append(crow)

        md = MonthData(name, values, colors, hidden, formulas)
        self._month_cache[name] = (_time.time(), md)
        return md

    def _block_map(self, md: MonthData) -> tuple[int, dict[str, int]]:
        """
        (header_row, {ключ города: колонка начала блока}).
        Блоки — колонки «Время» в строке-шапке. Город блока определяем по
        заголовку над ним (City.labels, затем City.alt_labels). Если заголовок
        не прочитался, а блоков столько же, сколько городов, — по порядку.
        """
        header_row = None
        starts: list[int] = []
        for r in range(min(8, md.nrows)):
            cols = [c for c, v in enumerate(md.values[r]) if v.strip() == "Время"]
            if len(cols) >= 2:
                header_row = r
                starts = sorted(cols)
                break
        if header_row is None:
            raise SheetError("Не найдена строка-шапка с колонками «Время»")

        titles = []
        for i, s in enumerate(starts):
            end = min(s + 8, starts[i + 1]) if i + 1 < len(starts) else s + 8
            titles.append(" ".join(md.val(r, c) for r in range(header_row)
                                   for c in range(s, end)).lower())

        keys = list(config.CITIES)
        found: dict[str, int] = {}
        taken: set[int] = set()
        for attr in ("labels", "alt_labels"):
            for key in keys:
                tokens = getattr(config.CITIES[key], attr)
                if key in found or not tokens:
                    continue
                hits = [i for i, t in enumerate(titles)
                        if i not in taken and _has_label(t, tokens)]
                if len(hits) == 1:
                    found[key] = hits[0]
                    taken.add(hits[0])
        # запасной вариант — по порядку блоков (у ПОСТ №2 своей «Время» может не быть)
        main = [k for k in keys if not config.CITIES[k].parent]
        order = keys if len(starts) == len(keys) else main if len(starts) == len(main) else []
        if order:
            for i, key in enumerate(order):
                if key not in found and i not in taken:
                    found[key] = i
                    taken.add(i)

        mapping = {key: starts[i] for key, i in found.items()}
        # Доп. пост (ПОСТ №2 Деревообробной): своей колонки «Время» может не быть —
        # тогда ищем по заголовку. И проверяем, что «Номер заказа» там, где ждём.
        for key, city in config.CITIES.items():
            if not city.parent:
                continue
            col = mapping.pop(key, None)
            off = config.column_index(city, "Номер заказа")
            if col is None or md.val(header_row, col + off).strip().lower() != "номер заказа":
                col = self._titled_block(md, header_row, city, set(mapping.values()))
            if col is not None:
                mapping[key] = col
        sig = f"{md.title}:{sorted(mapping.items())}"
        if sig not in _logged_maps:
            _logged_maps.add(sig)
            log.info("Лист «%s»: блоки городов %s", md.title,
                     {k: gspread.utils.rowcol_to_a1(1, c + 1)[:-1] for k, c in mapping.items()})
        return header_row, mapping

    @staticmethod
    def _titled_block(md: MonthData, header_row: int, city: config.City,
                      used: set[int]) -> int | None:
        """Начало блока доп. поста, у которого нет своей колонки «Время»:
        ищем его заголовок (City.labels) и под ним колонку «Номер заказа».
        Начало блока — колонка левее (там у обычных блоков «Время»)."""
        hits = [c for r in range(header_row) for c, v in enumerate(md.values[r])
                if _has_label(v, city.labels)]
        if not hits or max(hits) - min(hits) > 8:
            return None
        c0 = min(hits)
        for r in range(header_row, min(header_row + 60, md.nrows)):
            for c in range(c0, c0 + 8):
                if md.val(r, c).strip().lower() == "номер заказа":
                    start = c - config.column_index(city, "Номер заказа")
                    return None if start in used else start
        return None

    def _block_start_col(self, md: MonthData, city: config.City) -> tuple[int, int]:
        """(start_col, header_row) блока города."""
        header_row, mapping = self._block_map(md)
        if city.key not in mapping:
            raise SheetError(f"Не найден блок «{city.full_title}» на листе {md.title}")
        return mapping[city.key], header_row

    def _axes(self, md: MonthData, city: config.City, start_col: int) -> tuple[int, int, int]:
        """(колонка «Время», начало и ширина блока для поиска дат). У доп. поста
        без своей колонки «Время» берём их у основного поста (строки общие)."""
        time_col = start_col + config.column_index(city, "Время")
        width = len(config.LAYOUTS[city.layout]["columns"])
        if city.parent and not any(_norm_time(md.val(r, time_col)) for r in range(md.nrows)):
            parent = config.CITIES[city.parent]
            ps, _ = self._block_start_col(md, parent)
            return (ps + config.column_index(parent, "Время"), ps,
                    len(config.LAYOUTS[parent.layout]["columns"]))
        return time_col, start_col, width

    def _date_section(self, md: MonthData, start_col: int, width: int,
                      d: date) -> tuple[int, int]:
        """Границы строк секции даты: (первая строка секции, следующая дата/конец)."""
        target = d.strftime("%d.%m.%Y")
        date_rows: list[tuple[int, str]] = []
        for r in range(md.nrows):
            ds = _row_date(md, r, start_col, width)
            if ds:
                date_rows.append((r, ds))
        start = next((r for r, ds in date_rows if ds == target), None)
        if start is None:
            raise SheetError(f"Дата «{target}» не найдена на листе {md.title}")
        after = [r for r, _ in date_rows if r > start]
        end = after[0] if after else md.nrows
        return start, end

    def _section_slots(self, md: MonthData, city: config.City,
                       d: date) -> tuple[int, list[tuple[int, str]]]:
        """(start_col, [(строка, время)...]) — слоты времени секции даты по порядку."""
        start_col, _ = self._block_start_col(md, city)
        time_col, date_col, width = self._axes(md, city, start_col)
        sec_start, sec_end = self._date_section(md, date_col, width, d)
        slots: list[tuple[int, str]] = []
        for r in range(sec_start, sec_end):
            t = _norm_time(md.val(r, time_col))
            if t:
                slots.append((r, t))
        return start_col, slots

    def _slot_row(self, md: MonthData, city: config.City, d: date,
                  time: str) -> tuple[int, int]:
        """Вернуть (row, start_col) конкретного слота времени в блоке города."""
        start_col, slots = self._section_slots(md, city, d)
        want = _norm_time(time)
        for r, t in slots:
            if t == want:
                return r, start_col
        raise SheetError(f"Слот {time} не найден на {d.isoformat()} ({city.full_title})")

    # ───────────────────────── Свободные слоты ─────────────────────────

    def prefetch(self, dates: list[date]) -> None:
        """Прогреть кеш месячных листов для набора дат (фоном, ошибки глушим)."""
        seen: set[tuple[int, int]] = set()
        for d in dates:
            key = (d.year, d.month)
            if key in seen:
                continue
            seen.add(key)
            try:
                self._load_month(d)
            except Exception as e:  # noqa: BLE001
                log.info("prefetch %s не удался: %s", cal.month_sheet_name(d), e)

    def _free_in_block(self, md: MonthData, city: config.City, d: date,
                       diameter: str) -> list[str]:
        """
        Свободные слоты одного блока (поста) на дату.
        Обычно: ячейка «Номер заказа» белая и пустая.
        Большие шины (R20+) в Киеве занимают 2 подряд слота: слот подходит,
        если он пуст (цвет неважен) И следующий слот пуст ИЛИ это последний
        свободный слот дня. Пустой = пуста вся строка блока (см. _row_busy).
        """
        start_col, slots = self._section_slots(md, city, d)
        order_col = start_col + config.column_index(city, "Номер заказа")

        big = config.needs_two_slots(city, diameter)
        empties = [not self._row_busy(md, city, start_col, r) for r, _ in slots]
        whites = [_is_white(md.color(r, order_col)) for r, _ in slots]

        free: list[str] = []
        n = len(slots)
        for i, (r, t) in enumerate(slots):
            # СТАРТ записи всегда только на белом пустом слоте (оранжевый/красный
            # стартом быть не может).
            if not (empties[i] and whites[i]):
                continue
            if big:
                # R20+: второй слот — любой ПУСТОЙ (белый/оранжевый/красный);
                # либо это последний белый слот дня — тогда 1 слот.
                has_second = (i + 1 < n) and empties[i + 1]
                is_last_white = not any(empties[j] and whites[j] for j in range(i + 1, n))
                if has_second or is_last_white:
                    free.append(t)
            else:
                free.append(t)
        return free

    @_network_retry
    def free_slots_by_post(self, city: config.City, d: date, force: bool = False,
                           diameter: str = "") -> dict[str, config.City]:
        """
        Свободное время на город+дату → пост, куда записывать (по порядку времени).
        Если у филиала несколько постов, на каждое время берём предпочтительный
        для диаметра пост (config.posts_for), а если там занято — другой.
        """
        md = self._load_month(d, force=force)
        found: dict[str, config.City] = {}
        errors: list[SheetError] = []
        for post in config.posts_for(city, diameter):
            try:
                times = self._free_in_block(md, post, d, diameter)
            except SheetError as e:
                # доп. пост может отсутствовать на старом листе — считаем по остальным
                log.info("%s: %s", post.full_title, e)
                errors.append(e)
                continue
            for t in times:
                found.setdefault(t, post)
        if len(errors) == len(config.posts_of(city)):
            raise errors[0]

        free = sorted(found, key=_time_key)
        # На сегодня не показывать прошедшее время
        if d == cal.today():
            now = cal.now()
            free = [t for t in free if _time_key(t) >= (now.hour, now.minute)]
        return {t: found[t] for t in free}

    def read_free_slots(self, city: config.City, d: date, force: bool = False,
                        diameter: str = "") -> list[str]:
        """Свободное время на город+дату (без разбивки по постам)."""
        return list(self.free_slots_by_post(city, d, force, diameter))

    # ───────────────────────── Расписание для сайта (только чтение) ─────────────────────────

    def day_grid(self, city: config.City, d: date) -> list[dict]:
        """Сетка дня без персональных данных: [{t, c: W|O|R, b: занято?}].
        У филиала с несколькими постами слот свободен, если свободен хоть на одном."""
        md = self._load_month(d)
        grid = self._block_grid(md, city, d)
        for post in config.posts_of(city)[1:]:
            try:
                other = {x["t"]: x for x in self._block_grid(md, post, d)}
            except SheetError:
                continue
            grid = [other[x["t"]] if not _grid_free(x) and _grid_free(other.get(x["t"])) else x
                    for x in grid]
        return grid

    def _block_grid(self, md: MonthData, city: config.City, d: date) -> list[dict]:
        start_col, slots = self._section_slots(md, city, d)
        order_col = start_col + config.column_index(city, "Номер заказа")
        out = []
        for r, t in slots:
            color = md.color(r, order_col)
            c = "W" if _is_white(color) else ("R" if _is_red(color) else "O")
            out.append({"t": t, "c": c, "b": self._row_busy(md, city, start_col, r)})
        return out

    def days_for_city(self, city: config.City) -> dict[str, list[dict]]:
        """Сетки всех доступных дат города (до конца следующего месяца)."""
        out: dict[str, list[dict]] = {}
        for d in self.available_dates():
            try:
                out[d.isoformat()] = self.day_grid(city, d)
            except SheetError:
                continue  # дата не найдена на листе — пропускаем
        return out

    # ───────────────────────── Запись в грид (подход А) ─────────────────────────

    @_network_retry
    def write_booking(self, city: config.City, d: date, time: str, data: dict) -> None:
        """Вписать данные записи в ячейки слота. Большие шины (R20+) в Киеве —
        в 2 подряд слота (или в 1, если это последний слот дня)."""
        md = self._load_month(d)
        start_col, slots = self._section_slots(md, city, d)
        want = _norm_time(time)
        idx = next((i for i, (r, t) in enumerate(slots) if t == want), None)
        if idx is None:
            raise SheetError(f"Слот {time} не найден на {d.isoformat()} ({city.full_title})")

        columns = config.LAYOUTS[city.layout]["columns"]
        target_rows = [slots[idx][0]]
        if config.needs_two_slots(city, data.get("diameter", "")) and idx + 1 < len(slots):
            nr = slots[idx + 1][0]
            # второй слот занимаем, если он просто пустой (цвет любой)
            if not self._row_busy(md, city, start_col, nr):
                target_rows.append(nr)

        value_map = {
            "Номер тел.": data.get("phone", ""),
            "Номер заказа": data.get("order_number", ""),
            "Диаметр": data.get("diameter", ""),
            "ТИП Авто": data.get("car_type", ""),
            "МОП Запись": data.get("mop", ""),
        }
        ws = self.ss.worksheet(md.title)
        updates = []
        for row in target_rows:
            for name, val in value_map.items():
                if name in columns and val:
                    c = start_col + columns.index(name)
                    a1 = gspread.utils.rowcol_to_a1(row + 1, c + 1)
                    updates.append({"range": a1, "values": [[val]]})
        if not updates:
            raise SheetError("Нет данных для записи")
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        self._month_cache.clear()  # слот занят — сбросить кеш доступности

    # ───────────────────────── Поиск / удаление / перенос ─────────────────────────

    def _search_month_dates(self) -> list[date]:
        """Даты-представители месяцев для поиска: текущий + следующий."""
        today = cal.today()
        if today.month == 12:
            nxt = date(today.year + 1, 1, 1)
        else:
            nxt = date(today.year, today.month + 1, 1)
        return [today, nxt]

    def _block_date_rows(self, md: MonthData, start_col: int, width: int) -> list[tuple[int, date]]:
        """Список (строка_даты, дата) для блока города, сверху вниз."""
        out: list[tuple[int, date]] = []
        for r in range(md.nrows):
            ds = _row_date(md, r, start_col, width)
            if ds:
                try:
                    out.append((r, datetime.strptime(ds, "%d.%m.%Y").date()))
                except ValueError:
                    pass
        return out

    @staticmethod
    def _match(cell: str, q: str) -> bool:
        cell = cell.strip()
        return bool(cell) and (cell == q or (len(q) >= 4 and q in cell))

    def _bot_cols(self, cols: list[str]) -> list[str]:
        """Колонки, которые бот заполняет/чистит (по порядку)."""
        return [c for c in ["Номер тел.", "Номер заказа", "Диаметр", "ТИП Авто", "МОП Запись"]
                if c in cols]

    def _row_cols(self, md: MonthData, city: config.City, start_col: int) -> list[int]:
        """Колонки строки записи: весь блок, кроме «Время». Скрытые колонки —
        только если их заполняет бот (в скрытых может лежать что угодно)."""
        cols = config.LAYOUTS[city.layout]["columns"]
        bot = self._bot_cols(cols)
        return [start_col + i for i, name in enumerate(cols)
                if name != "Время" and (name in bot or start_col + i not in md.hidden)]

    def _row_busy(self, md: MonthData, city: config.City, start_col: int, r: int) -> bool:
        """Слот занят, если в строке заполнена хоть одна ячейка (кроме «Время»).
        Формулы и ошибки вида #N/A (напр. в «Ориент. стоимость») не считаются."""
        for c in self._row_cols(md, city, start_col):
            v = md.val(r, c).strip()
            if v and (r, c) not in md.formulas and not _ERROR_RE.match(v):
                return True
        return False

    @_network_retry
    def search_bookings(self, query: str) -> list[Booking]:
        """Найти записи по номеру заказа или телефону (текущий + след. месяц)."""
        q = query.strip()
        if not q:
            return []
        results: list[Booking] = []
        seen: set[str] = set()
        for d in self._search_month_dates():
            name = cal.month_sheet_name(d)
            if name in seen:
                continue
            seen.add(name)
            try:
                md = self._load_month(d)
            except SheetError:
                continue
            for city in config.CITIES.values():
                try:
                    start_col, _ = self._block_start_col(md, city)
                    time_col, date_col, date_width = self._axes(md, city, start_col)
                except SheetError:
                    continue
                cols = config.LAYOUTS[city.layout]["columns"]
                order_col = start_col + cols.index("Номер заказа")
                phone_col = start_col + cols.index("Номер тел.") if "Номер тел." in cols else None
                diam_col = start_col + cols.index("Диаметр") if "Диаметр" in cols else None
                car_col = start_col + cols.index("ТИП Авто") if "ТИП Авто" in cols else None
                mop_col = start_col + cols.index("МОП Запись") if "МОП Запись" in cols else None
                date_rows = self._block_date_rows(md, date_col, date_width)

                for r in range(md.nrows):
                    order = md.val(r, order_col)
                    phone = md.val(r, phone_col) if phone_col is not None else ""
                    if not (self._match(order, q) or self._match(phone, q)):
                        continue
                    # 2-слотовая запись (R20+) = 2 подряд строки с тем же заказом;
                    # показываем только первую (пропускаем строку-продолжение).
                    prev_order = md.val(r - 1, order_col).strip() if r > 0 else ""
                    if order.strip() and prev_order == order.strip():
                        continue
                    tm = _norm_time(md.val(r, time_col))
                    do = next((dt for (dr, dt) in reversed(date_rows) if dr <= r), None)
                    if tm is None or do is None:
                        continue
                    results.append(Booking(md.title, city, do, tm, {
                        "order_number": order.strip(),
                        "phone": phone.strip(),
                        "diameter": md.val(r, diam_col).strip() if diam_col is not None else "",
                        "car_type": md.val(r, car_col).strip() if car_col is not None else "",
                        "mop": md.val(r, mop_col).strip() if mop_col is not None else "",
                    }))
        return results

    @_network_retry
    def cancel_booking(self, city: config.City, d: date, time: str) -> None:
        """Освободить слот: очистить всю строку записи, кроме «Время» и формул
        (цвет фона не трогаем, чтобы не менять смысл «красное = закрыто»).
        Для R20+ чистим обе строки."""
        md = self._load_month(d, force=True)
        start_col, slots = self._section_slots(md, city, d)
        order_col = start_col + config.column_index(city, "Номер заказа")
        want = _norm_time(time)
        idx = next((i for i, (r, t) in enumerate(slots) if t == want), None)
        if idx is None:
            raise SheetError(f"Слот {time} не найден на {d.isoformat()} ({city.full_title})")

        row0 = slots[idx][0]
        order_val = md.val(row0, order_col).strip()
        rows = [row0]
        for j in (idx - 1, idx + 1):  # соседние строки той же 2-слотовой записи
            if 0 <= j < len(slots) and order_val and md.val(slots[j][0], order_col).strip() == order_val:
                rows.append(slots[j][0])

        # чистим всю строку (и «Прозвон», и «Стоимость»), иначе слот останется
        # занятым; формулы не трогаем
        ranges = [gspread.utils.rowcol_to_a1(r + 1, c + 1) for r in rows
                  for c in self._row_cols(md, city, start_col) if (r, c) not in md.formulas]
        if not ranges:
            return
        ws = self.ss.worksheet(md.title)
        ws.batch_clear(ranges)
        self._month_cache.clear()

    def move_booking(self, city: config.City, d: date, time: str,
                     new_city: config.City, new_d: date, new_time: str, data: dict) -> None:
        """Перенести запись: записать в новый слот, затем очистить старый."""
        self.write_booking(new_city, new_d, new_time, data)
        self.cancel_booking(city, d, time)

    # ───────────────────────── Выпадающие списки ─────────────────────────

    def _expand_condition(self, cond: dict) -> list[str]:
        """Развернуть условие data validation в список значений.
        Поддерживает и явный перечень (ONE_OF_LIST), и ссылку на диапазон
        (ONE_OF_RANGE) — во втором случае дочитываем значения диапазона.
        """
        ctype = cond.get("type")
        vals = cond.get("values", []) or []
        if ctype == "ONE_OF_LIST":
            return [v["userEnteredValue"] for v in vals if v.get("userEnteredValue")]
        if ctype == "ONE_OF_RANGE" and vals:
            ref = vals[0].get("userEnteredValue", "").lstrip("=")
            if not ref:
                return []
            resp = self.ss.values_get(ref)
            out: list[str] = []
            for row in resp.get("values", []):
                if row and str(row[0]).strip():
                    out.append(str(row[0]).strip())
            return out
        return []

    @_network_retry
    def read_validation_values(self, city: config.City, column_name: str,
                               validation_key: str) -> list[str]:
        """Значения выпадающего списка из data validation (кешируются надолго);
        при неудаче — fallback из config."""
        cached = self._val_cache.get(validation_key)
        if cached and (_time.time() - cached[0]) < VAL_CACHE_TTL:
            return cached[1]
        try:
            md = self._load_month(cal.today())
            start_col, _ = self._block_start_col(md, city)
            time_col, date_col, width = self._axes(md, city, start_col)
            sec_start, sec_end = self._date_section(md, date_col, width, cal.today())
            col = start_col + config.column_index(city, column_name)

            data_row = next((r for r in range(sec_start, sec_end)
                             if _norm_time(md.val(r, time_col))), sec_start + 1)
            a1 = gspread.utils.rowcol_to_a1(data_row + 1, col + 1)
            meta = self.ss.fetch_sheet_metadata({
                "includeGridData": True,
                "ranges": [f"{md.title}!{a1}"],
                "fields": "sheets(data(rowData(values(dataValidation))))",
            })
            cond = (meta["sheets"][0]["data"][0]["rowData"][0]["values"][0]
                    ["dataValidation"]["condition"])
            out = self._expand_condition(cond)
            if out:
                self._val_cache[validation_key] = (_time.time(), out)
                return out
        except Exception as e:  # noqa: BLE001
            log.warning("data validation (%s) не прочитан: %s; беру fallback", validation_key, e)
        return config.FALLBACK_VALIDATION[validation_key]
