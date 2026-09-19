"""
Калибровочный скрипт: печатает структуру реальной таблицы, чтобы точно
настроить геометрию грида (блоки городов, строки дат/времени) и убедиться,
что значения выпадающих списков читаются.

Запуск (когда GOOGLE_CREDENTIALS_JSON и SPREADSHEET_ID в окружении):
    python inspect_sheet.py
    python inspect_sheet.py --month "ШМ Сентябрь 26"
"""

from __future__ import annotations

import argparse
import json
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

import config
import calendar_utils as cal

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def client() -> gspread.Client:
    info = json.loads(config.GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="Имя месячного листа (по умолчанию — текущий месяц)")
    ap.add_argument("--rows", type=int, default=15, help="Сколько верхних строк показать")
    ap.add_argument("--cols", type=int, default=30, help="Сколько левых колонок показать")
    args = ap.parse_args()

    gc = client()
    ss = gc.open_by_key(config.SPREADSHEET_ID)

    print("=" * 70)
    print(f"Таблица: {ss.title}")
    print("Листы:")
    for ws in ss.worksheets():
        print(f"  • {ws.title}  ({ws.row_count}×{ws.col_count})")
    print("=" * 70)

    month_name = args.month or cal.month_sheet_name(date.today())
    try:
        ws = ss.worksheet(month_name)
    except gspread.exceptions.WorksheetNotFound:
        print(f"⚠️ Лист «{month_name}» не найден. Укажите --month из списка выше.")
        return

    grid = ws.get_all_values()
    print(f"\nЛист «{month_name}»: {len(grid)} строк\n")

    print(f"── Верхние {args.rows} строк × {args.cols} колонок ──")
    for r, row in enumerate(grid[:args.rows]):
        cells = [c[:12] for c in row[:args.cols]]
        print(f"{r:>3}: " + " | ".join(cells))

    # Кандидаты меток дат (дата «дд.мм.гггг» в отдельной ячейке блока)
    print("\n── Найденные даты (row, col, текст) ──")
    import re
    date_re = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")
    found = 0
    for r, row in enumerate(grid):
        for c, val in enumerate(row):
            if date_re.search(val.strip()):
                print(f"  ({r}, {c})  {val.strip()}")
                found += 1
                if found >= 20:
                    break
        if found >= 20:
            break

    # Кандидаты заголовков «Время»
    print("\n── Заголовки «Время» (начало блоков городов) ──")
    for r, row in enumerate(grid[:15]):
        for c, val in enumerate(row):
            if val.strip() == "Время":
                nxt = [x.strip()[:14] for x in row[c:c + 8]]
                print(f"  строка {r}, колонка {c}: {nxt}")

    print("\nГотово. Пришли этот вывод разработчику для точной калибровки.")


if __name__ == "__main__":
    main()
