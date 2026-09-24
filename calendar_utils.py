"""Помощники по датам/времени: имена месячных листов, метки дат, слоты времени."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import config


def month_sheet_name(d: date) -> str:
    """date -> имя листа, напр. «ШМ Сентябрь 26»."""
    return f"ШМ {config.MONTHS_RU[d.month]} {d:%y}"


def date_label(d: date) -> str:
    """date -> метка в таблице, напр. «ЧТ 03.09.2026»."""
    return f"{config.DOW_RU[d.weekday()]} {d:%d.%m.%Y}"


def date_button(d: date) -> str:
    """Короткая подпись кнопки даты для оператора, напр. «ЧТ 03.09»."""
    return f"{config.DOW_RU[d.weekday()]} {d:%d.%m}"


def slot_times() -> list[str]:
    """Список слотов времени 9:00..19:30 строками в формате таблицы."""
    out: list[str] = []
    cur = datetime(2000, 1, 1, config.WORK_START_HOUR, 0)
    end = datetime(2000, 1, 1, config.WORK_END_HOUR, config.WORK_END_MINUTE)
    while cur <= end:
        if config.TIME_FORMAT_LEADING_ZERO:
            out.append(cur.strftime("%H:%M"))
        else:
            out.append(f"{cur.hour}:{cur.minute:02d}")
        cur += timedelta(minutes=config.SLOT_STEP_MIN)
    return out


def upcoming_dates(days_ahead: int, start: date | None = None) -> list[date]:
    """Ближайшие даты (включая сегодня) на N дней вперёд."""
    start = start or date.today()
    return [start + timedelta(days=i) for i in range(days_ahead)]


def dates_through_month_end(months_ahead: int = 1, start: date | None = None) -> list[date]:
    """Даты от сегодня до конца месяца (текущего при 0, следующего при 1 и т.д.)."""
    start = start or date.today()
    total = (start.month - 1) + months_ahead
    y = start.year + total // 12
    m = total % 12 + 1
    after = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    end = after - timedelta(days=1)
    n = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(max(n, 0))]


def parse_date_button(label: str, candidates: list[date]) -> date | None:
    """Вернуть дату из candidates, соответствующую нажатой кнопке."""
    for d in candidates:
        if date_button(d) == label:
            return d
    return None
