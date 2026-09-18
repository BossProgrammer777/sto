"""
Слой доступа к Google Sheets (gspread) с ретраями.

Отвечает за:
  • чтение свободных слотов на город+дату из «красивой» простыни (грид);
  • запись записи в служебный «плоский» лист (подход Б);
  • (опционально, WRITE_TO_GRID) запись прямо в ячейку простыни (подход А);
  • чтение допустимых значений выпадающих списков (data validation).

⚠️ КАЛИБРОВКА: точная геометрия грида (где начинается блок города, как
идут строки дат/времени) финализируется по реальной таблице — см.
inspect_sheet.py и функции _locate_* ниже. Пока используются эвристики
по заголовкам колонок и меткам дат.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime

import gspread
from google.oauth2.service_account import Credentials
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config
import calendar_utils as cal

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Заголовки плоского служебного листа (подход Б).
FLAT_HEADERS = [
    "timestamp", "city", "date", "time", "phone", "order_number",
    "diameter", "car_type", "price", "mop", "telegram_id", "status",
]

_DATE_LABEL_RE = re.compile(r"^(ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s+\d{2}\.\d{2}\.\d{4}$")


class SheetError(Exception):
    """Понятная ошибка уровня таблицы для показа/логов."""


def _network_retry(fn):
    """Ретраи при сетевых сбоях/времянных ошибках API (2s,4s,8s,16s)."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        retry=retry_if_exception_type((gspread.exceptions.APIError, ConnectionError, TimeoutError)),
    )(fn)


class SheetsClient:
    def __init__(self) -> None:
        info = json.loads(config.GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.ss = self.gc.open_by_key(config.SPREADSHEET_ID)
        self._flat_ws = None

    # ───────────────────────── Плоский лист (подход Б) ─────────────────────────

    @_network_retry
    def _flat(self):
        if self._flat_ws is not None:
            return self._flat_ws
        try:
            ws = self.ss.worksheet(config.FLAT_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            ws = self.ss.add_worksheet(config.FLAT_SHEET_NAME, rows=2000, cols=len(FLAT_HEADERS))
            ws.update("A1", [FLAT_HEADERS])
        self._flat_ws = ws
        return ws

    @_network_retry
    def append_booking(self, *, city: config.City, d: date, time: str,
                       data: dict, mop: str, telegram_id: int) -> None:
        """Дописать запись в плоский лист. status=active."""
        ws = self._flat()
        row = [
            datetime.now().isoformat(timespec="seconds"),
            city.title,
            d.isoformat(),
            time,
            data.get("phone", ""),
            data.get("order_number", ""),
            data.get("diameter", ""),
            data.get("car_type", ""),
            data.get("price", ""),
            mop,
            str(telegram_id),
            "active",
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")

    @_network_retry
    def _flat_booked_times(self, city: config.City, d: date) -> set[str]:
        """Времена, уже занятые записями бота (плоский лист), на город+дату."""
        ws = self._flat()
        records = ws.get_all_values()[1:]  # без заголовка
        booked: set[str] = set()
        di = d.isoformat()
        for r in records:
            r = r + [""] * (len(FLAT_HEADERS) - len(r))
            _, c_city, c_date, c_time, *_rest = r
            status = r[FLAT_HEADERS.index("status")]
            if c_city == city.title and c_date == di and status == "active":
                booked.add(c_time.strip())
        return booked

    # ───────────────────────── Красивая простыня (грид) ─────────────────────────

    @_network_retry
    def _month_ws(self, d: date):
        name = cal.month_sheet_name(d)
        try:
            return self.ss.worksheet(name)
        except gspread.exceptions.WorksheetNotFound as e:
            raise SheetError(f"Не найден месячный лист «{name}»") from e

    def _locate_city_block(self, grid: list[list[str]], city: config.City) -> dict:
        """
        Найти блок колонок города: ищем в верхних строках подпись города,
        затем строку с заголовком «Время», от которой начинается блок.
        Возвращает {start_col, header_row, columns}.

        ⚠️ КАЛИБРОВКА: эвристика по подписи города и заголовку «Время».
        """
        columns = config.LAYOUTS[city.layout]["columns"]
        label = city.sheet_label.lower()

        # 1) колонка, где встречается подпись города
        city_col = None
        for r, row in enumerate(grid[:8]):
            for c, val in enumerate(row):
                if val and label in val.strip().lower():
                    city_col = c
                    break
            if city_col is not None:
                break

        # 2) от найденной колонки ищем ближайшую «Время» (начало блока)
        for r, row in enumerate(grid[:12]):
            for c, val in enumerate(row):
                if val.strip() == "Время":
                    if city_col is None or abs(c - city_col) <= len(columns):
                        return {"start_col": c, "header_row": r, "columns": columns}

        raise SheetError(f"Не удалось найти блок города «{city.title}» на листе")

    def _locate_date_section(self, grid: list[list[str]], block: dict, d: date) -> tuple[int, int]:
        """
        Границы строк секции даты внутри блока: от строки метки даты до
        следующей метки даты (или конца). Возвращает (start_row, end_row).

        ⚠️ КАЛИБРОВКА: ищем метку даты в колонке «Время» блока.
        """
        col = block["start_col"]
        target = cal.date_label(d)
        start = None
        for r in range(len(grid)):
            cell = grid[r][col].strip() if col < len(grid[r]) else ""
            if cell == target:
                start = r
                break
        if start is None:
            raise SheetError(f"Дата «{target}» не найдена в блоке (лист {cal.month_sheet_name(d)})")

        end = len(grid)
        for r in range(start + 1, len(grid)):
            cell = grid[r][col].strip() if col < len(grid[r]) else ""
            if _DATE_LABEL_RE.match(cell):
                end = r
                break
        return start, end

    @_network_retry
    def read_free_slots(self, city: config.City, d: date) -> list[str]:
        """
        Свободные слоты времени на город+дату.
        Свободно = ячейка «Номер заказа» пустая в простыне И нет активной
        записи бота в плоском листе на этот слот.
        """
        ws = self._month_ws(d)
        grid = ws.get_all_values()
        block = self._locate_city_block(grid, city)
        start, end = self._locate_date_section(grid, block, d)

        time_col = block["start_col"] + config.column_index(city, "Время")
        order_col = block["start_col"] + config.column_index(city, "Номер заказа")

        occupied: set[str] = set()
        times_in_grid: list[str] = []
        for r in range(start, end):
            row = grid[r]
            t = row[time_col].strip() if time_col < len(row) else ""
            if not t or not re.match(r"^\d{1,2}:\d{2}$", t):
                continue
            times_in_grid.append(t)
            order = row[order_col].strip() if order_col < len(row) else ""
            if order:
                occupied.add(t)

        occupied |= self._flat_booked_times(city, d)

        # Если в гриде удалось прочитать список времён — берём его, иначе дефолт.
        all_times = times_in_grid or cal.slot_times()
        return [t for t in all_times if t not in occupied]

    @_network_retry
    def mark_slot_in_grid(self, city: config.City, d: date, time: str,
                          data: dict, mop: str) -> None:
        """Подход А (опционально): вписать данные в ячейки простыни."""
        if not config.WRITE_TO_GRID:
            return
        ws = self._month_ws(d)
        grid = ws.get_all_values()
        block = self._locate_city_block(grid, city)
        start, end = self._locate_date_section(grid, block, d)
        time_col = block["start_col"] + config.column_index(city, "Время")

        target_row = None
        for r in range(start, end):
            row = grid[r]
            if time_col < len(row) and row[time_col].strip() == time:
                target_row = r
                break
        if target_row is None:
            raise SheetError(f"Слот {time} не найден для записи в грид")

        updates = []
        columns = config.LAYOUTS[city.layout]["columns"]
        value_map = {
            "Номер тел.": data.get("phone", ""),
            "Номер заказа": data.get("order_number", ""),
            "Диаметр": data.get("diameter", ""),
            "ТИП Авто": data.get("car_type", ""),
            "МОП Запись": mop,
            "Ориент. стоимость (4шт)": data.get("price", ""),
        }
        for name, val in value_map.items():
            if name in columns and val:
                c = block["start_col"] + columns.index(name)
                a1 = gspread.utils.rowcol_to_a1(target_row + 1, c + 1)
                updates.append({"range": a1, "values": [[val]]})
        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")

    # ───────────────────────── Выпадающие списки ─────────────────────────

    @_network_retry
    def read_validation_values(self, city: config.City, column_name: str,
                               validation_key: str) -> list[str]:
        """
        Прочитать допустимые значения выпадающего списка из data validation
        первой строки данных нужной колонки. При неудаче — fallback из config.
        """
        try:
            ws = self._month_ws(date.today())
            grid = ws.get_all_values()
            block = self._locate_city_block(grid, city)
            col = block["start_col"] + config.column_index(city, column_name)
            row = block["header_row"] + 2  # ориентировочно первая ячейка данных
            a1 = gspread.utils.rowcol_to_a1(row, col + 1)
            meta = self.ss.fetch_sheet_metadata({
                "includeGridData": True,
                "ranges": [f"{ws.title}!{a1}"],
                "fields": "sheets(data(rowData(values(dataValidation))))",
            })
            values = (meta["sheets"][0]["data"][0]["rowData"][0]["values"][0]
                      ["dataValidation"]["condition"]["values"])
            out = [v["userEnteredValue"] for v in values if "userEnteredValue" in v]
            if out:
                return out
        except Exception as e:  # noqa: BLE001 — чтение validation не критично
            log.warning("Не удалось прочитать data validation (%s): %s; беру fallback",
                        validation_key, e)
        return config.FALLBACK_VALIDATION[validation_key]
