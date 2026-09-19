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

Определение свободного слота (по решению заказчика):
  СВОБОДНО = ячейка «Номер заказа» БЕЛАЯ и ПУСТАЯ.
  Красный (занято/закрыто) и оранжевый (недоступно) — не свободны.

Запись (подход А): бот пишет данные прямо в ячейки нужного слота простыни.
Значения + цвета читаем ОДНИМ запросом на месячный лист и кешируем.
"""

from __future__ import annotations

import json
import logging
import re
import time as _time
from dataclasses import dataclass
from datetime import date, datetime

import gspread
from google.oauth2.service_account import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config
import calendar_utils as cal

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
_DATE_IN_CELL_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")

MONTH_CACHE_TTL = 45      # сек — кеш прочитанного месячного листа (значения+цвета)
VAL_CACHE_TTL = 6 * 3600  # сек — кеш значений выпадающих списков (меняются редко)


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
                "fields": ("sheets(properties(title),data(rowData(values("
                           "formattedValue,effectiveFormat(backgroundColor)))))"),
            })
        except gspread.exceptions.APIError as e:
            raise SheetError(f"Не удалось прочитать лист «{name}»: {e}") from e

        sheets = meta.get("sheets", [])
        if not sheets:
            raise SheetError(f"Месячный лист «{name}» не найден")

        row_data = sheets[0].get("data", [{}])[0].get("rowData", [])
        values: list[list[str]] = []
        colors: list[list[dict | None]] = []
        for row in row_data:
            cells = row.get("values", []) or []
            vrow, crow = [], []
            for cell in cells:
                vrow.append(cell.get("formattedValue", "") or "")
                fmt = cell.get("effectiveFormat") or {}
                crow.append(fmt.get("backgroundColor"))
            values.append(vrow)
            colors.append(crow)

        md = MonthData(name, values, colors)
        self._month_cache[name] = (_time.time(), md)
        return md

    def _block_start_col(self, md: MonthData, city: config.City) -> tuple[int, int]:
        """
        (start_col, header_row) блока города. Блоки определяем по строке-шапке,
        где несколько раз встречается «Время»; сопоставляем слева направо
        порядку городов в config.CITIES.
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

        order = list(config.CITIES.keys())
        if len(starts) < len(order):
            log.warning("Найдено блоков «Время»: %d, городов: %d", len(starts), len(order))
        try:
            idx = order.index(city.key)
            return starts[idx], header_row
        except (ValueError, IndexError) as e:
            raise SheetError(f"Не удалось сопоставить блок города «{city.title}»") from e

    def _date_section(self, md: MonthData, start_col: int, width: int,
                      d: date) -> tuple[int, int]:
        """Границы строк секции даты: (первая строка секции, следующая дата/конец)."""
        target = d.strftime("%d.%m.%Y")
        date_rows: list[tuple[int, str]] = []
        for r in range(md.nrows):
            for c in range(start_col, start_col + width):
                m = _DATE_IN_CELL_RE.search(md.val(r, c))
                if m:
                    date_rows.append((r, m.group(1)))
                    break
        start = next((r for r, ds in date_rows if ds == target), None)
        if start is None:
            raise SheetError(f"Дата «{target}» не найдена на листе {md.title}")
        after = [r for r, _ in date_rows if r > start]
        end = after[0] if after else md.nrows
        return start, end

    def _section_slots(self, md: MonthData, city: config.City,
                       d: date) -> tuple[int, list[tuple[int, str]]]:
        """(start_col, [(строка, время)...]) — слоты времени секции даты по порядку."""
        width = len(config.LAYOUTS[city.layout]["columns"])
        start_col, _ = self._block_start_col(md, city)
        sec_start, sec_end = self._date_section(md, start_col, width, d)
        time_col = start_col + config.column_index(city, "Время")
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
        raise SheetError(f"Слот {time} не найден на {d.isoformat()} ({city.title})")

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

    @_network_retry
    def read_free_slots(self, city: config.City, d: date, force: bool = False,
                        diameter: str = "") -> list[str]:
        """
        Свободные слоты на город+дату.
        Обычно: ячейка «Номер заказа» белая и пустая.
        Большие шины (R20+) в Киеве занимают 2 подряд слота: слот подходит,
        если он пуст (цвет неважен) И следующий слот пуст ИЛИ это последний
        свободный слот дня. Свободность считается по пустоте «Номер заказа».
        """
        md = self._load_month(d, force=force)
        start_col, slots = self._section_slots(md, city, d)
        order_col = start_col + config.column_index(city, "Номер заказа")

        big = config.needs_two_slots(city, diameter)
        empties = [not md.val(r, order_col).strip() for r, _ in slots]   # пусто?
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

        # На сегодня не показывать прошедшее время
        if d == date.today():
            now = datetime.now()
            free = [t for t in free
                    if (int(t.split(":")[0]), int(t.split(":")[1])) >= (now.hour, now.minute)]
        return free

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
            raise SheetError(f"Слот {time} не найден на {d.isoformat()} ({city.title})")

        columns = config.LAYOUTS[city.layout]["columns"]
        order_col = start_col + columns.index("Номер заказа")
        target_rows = [slots[idx][0]]
        if config.needs_two_slots(city, data.get("diameter", "")) and idx + 1 < len(slots):
            nr = slots[idx + 1][0]
            # второй слот занимаем, если он просто пустой (цвет любой)
            if not md.val(nr, order_col).strip():
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
        today = date.today()
        if today.month == 12:
            nxt = date(today.year + 1, 1, 1)
        else:
            nxt = date(today.year, today.month + 1, 1)
        return [today, nxt]

    def _block_date_rows(self, md: MonthData, start_col: int, width: int) -> list[tuple[int, date]]:
        """Список (строка_даты, дата) для блока города, сверху вниз."""
        out: list[tuple[int, date]] = []
        for r in range(md.nrows):
            for c in range(start_col, start_col + width):
                m = _DATE_IN_CELL_RE.search(md.val(r, c))
                if m:
                    try:
                        out.append((r, datetime.strptime(m.group(1), "%d.%m.%Y").date()))
                    except ValueError:
                        pass
                    break
        return out

    @staticmethod
    def _match(cell: str, q: str) -> bool:
        cell = cell.strip()
        return bool(cell) and (cell == q or (len(q) >= 4 and q in cell))

    def _bot_cols(self, cols: list[str]) -> list[str]:
        """Колонки, которые бот заполняет/чистит (по порядку)."""
        return [c for c in ["Номер тел.", "Номер заказа", "Диаметр", "ТИП Авто", "МОП Запись"]
                if c in cols]

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
                except SheetError:
                    continue
                cols = config.LAYOUTS[city.layout]["columns"]
                width = len(cols)
                order_col = start_col + cols.index("Номер заказа")
                time_col = start_col + cols.index("Время")
                phone_col = start_col + cols.index("Номер тел.") if "Номер тел." in cols else None
                diam_col = start_col + cols.index("Диаметр") if "Диаметр" in cols else None
                car_col = start_col + cols.index("ТИП Авто") if "ТИП Авто" in cols else None
                mop_col = start_col + cols.index("МОП Запись") if "МОП Запись" in cols else None
                date_rows = self._block_date_rows(md, start_col, width)

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
        """Освободить слот: очистить данные записи (цвет фона не трогаем, чтобы
        не менять смысл «красное = закрыто»). Для R20+ чистим обе строки."""
        md = self._load_month(d, force=True)
        start_col, slots = self._section_slots(md, city, d)
        order_col = start_col + config.column_index(city, "Номер заказа")
        want = _norm_time(time)
        idx = next((i for i, (r, t) in enumerate(slots) if t == want), None)
        if idx is None:
            raise SheetError(f"Слот {time} не найден на {d.isoformat()} ({city.title})")

        row0 = slots[idx][0]
        order_val = md.val(row0, order_col).strip()
        rows = [row0]
        for j in (idx - 1, idx + 1):  # соседние строки той же 2-слотовой записи
            if 0 <= j < len(slots) and order_val and md.val(slots[j][0], order_col).strip() == order_val:
                rows.append(slots[j][0])

        cols = config.LAYOUTS[city.layout]["columns"]
        bot_cols = self._bot_cols(cols)
        if not bot_cols:
            return
        first = start_col + cols.index(bot_cols[0])
        last = start_col + cols.index(bot_cols[-1])
        ranges = [f"{gspread.utils.rowcol_to_a1(r + 1, first + 1)}:"
                  f"{gspread.utils.rowcol_to_a1(r + 1, last + 1)}" for r in rows]
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
            md = self._load_month(date.today())
            width = len(config.LAYOUTS[city.layout]["columns"])
            start_col, _ = self._block_start_col(md, city)
            sec_start, sec_end = self._date_section(md, start_col, width, date.today())
            time_col = start_col + config.column_index(city, "Время")
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
