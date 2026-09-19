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
from datetime import date

import gspread
from google.oauth2.service_account import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config
import calendar_utils as cal

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
_DATE_IN_CELL_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")

MONTH_CACHE_TTL = 45  # сек — кеш прочитанного месячного листа (значения+цвета)


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


class SheetsClient:
    def __init__(self) -> None:
        info = json.loads(config.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.ss = self.gc.open_by_key(config.SPREADSHEET_ID)
        self._month_cache: dict[str, tuple[float, MonthData]] = {}

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

    def _slot_row(self, md: MonthData, city: config.City, d: date,
                  time: str) -> tuple[int, int]:
        """Вернуть (row, start_col) конкретного слота времени в блоке города."""
        width = len(config.LAYOUTS[city.layout]["columns"])
        start_col, _ = self._block_start_col(md, city)
        sec_start, sec_end = self._date_section(md, start_col, width, d)
        time_col = start_col + config.column_index(city, "Время")
        want = _norm_time(time)
        for r in range(sec_start, sec_end):
            if _norm_time(md.val(r, time_col)) == want:
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
    def read_free_slots(self, city: config.City, d: date, force: bool = False) -> list[str]:
        md = self._load_month(d, force=force)
        width = len(config.LAYOUTS[city.layout]["columns"])
        start_col, _ = self._block_start_col(md, city)
        sec_start, sec_end = self._date_section(md, start_col, width, d)

        time_col = start_col + config.column_index(city, "Время")
        order_col = start_col + config.column_index(city, "Номер заказа")

        free: list[str] = []
        for r in range(sec_start, sec_end):
            t = _norm_time(md.val(r, time_col))
            if not t:
                continue
            order_text = md.val(r, order_col).strip()
            white = _is_white(md.color(r, order_col))
            if order_text or not white:
                continue  # занято (текст), закрыто (красный) или недоступно (оранжевый)
            free.append(t)

        # На сегодня не показывать прошедшее время
        if d == date.today():
            from datetime import datetime
            now = datetime.now()
            free = [t for t in free
                    if (int(t.split(":")[0]), int(t.split(":")[1])) >= (now.hour, now.minute)]
        return free

    # ───────────────────────── Запись в грид (подход А) ─────────────────────────

    @_network_retry
    def write_booking(self, city: config.City, d: date, time: str, data: dict) -> None:
        """Вписать данные записи прямо в ячейки нужного слота простыни."""
        md = self._load_month(d)
        target_row, start_col = self._slot_row(md, city, d, time)

        columns = config.LAYOUTS[city.layout]["columns"]
        value_map = {
            "Номер тел.": data.get("phone", ""),
            "Номер заказа": data.get("order_number", ""),
            "Диаметр": data.get("diameter", ""),
            "ТИП Авто": data.get("car_type", ""),
            "МОП Запись": data.get("mop", ""),
            "Ориент. стоимость (4шт)": data.get("price", ""),
        }
        ws = self.ss.worksheet(md.title)
        updates = []
        for name, val in value_map.items():
            if name in columns and val:
                c = start_col + columns.index(name)
                a1 = gspread.utils.rowcol_to_a1(target_row + 1, c + 1)
                updates.append({"range": a1, "values": [[val]]})
        if not updates:
            raise SheetError("Нет данных для записи")
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        self._month_cache.clear()  # слот занят — сбросить кеш доступности

    # ───────────────────────── Выпадающие списки ─────────────────────────

    @_network_retry
    def read_validation_values(self, city: config.City, column_name: str,
                               validation_key: str) -> list[str]:
        """Значения выпадающего списка из data validation; при неудаче — fallback."""
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
            vals = (meta["sheets"][0]["data"][0]["rowData"][0]["values"][0]
                    ["dataValidation"]["condition"]["values"])
            out = [v["userEnteredValue"] for v in vals if "userEnteredValue" in v]
            if out:
                return out
        except Exception as e:  # noqa: BLE001
            log.warning("data validation (%s) не прочитан: %s; беру fallback", validation_key, e)
        return config.FALLBACK_VALIDATION[validation_key]
