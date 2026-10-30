"""
Таблица хранения шин филиала: поиск клиента по ФИО, комплекты и сообщение в группу.

Ожидаемые колонки (ищутся по заголовку, порядок любой):
    ФИО | Ряд | Место | Шины | Комментарий
Комплекты клиента: каждая строка в ячейке «Шины» — отдельный комплект; если клиент
записан в таблице несколькими строками, его комплекты собираются вместе.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import config

log = logging.getLogger(__name__)

CACHE_TTL = 120  # сек — таблица хранения перечитывается не чаще раза в 2 минуты


@dataclass(frozen=True)
class TireSet:
    tires: str     # описание комплекта (одна строка из ячейки «Шины»)
    row: str
    place: str
    comment: str


@dataclass(frozen=True)
class StoredClient:
    name: str
    sets: tuple[TireSet, ...]


# Начало следующей шины внутри одной строки ячейки: «… 98H Зимняя шина …».
_NEXT_TIRE_RE = re.compile(r"(?<=\S)[\s,;+]+(?=(?:зимн|летн|літн|всесезон)\w*\s+шин)", re.I)


def split_tires(cell: str) -> list[str]:
    """Комплекты из ячейки «Шины»: каждая строка — отдельно; если в одной строке
    подряд несколько шин («Зимняя шина … Летняя шина …») — тоже делим."""
    out = []
    for ln in cell.splitlines():
        out.extend(p.strip() for p in _NEXT_TIRE_RE.split(ln) if p.strip())
    return out


def _norm(s: str) -> str:
    s = s.replace("#", " ").replace("ё", "е").replace("Ё", "Е").replace("’", "'").replace("ʼ", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def parse_rows(values: list[list[str]]) -> list[StoredClient]:
    """Разобрать значения листа хранения в список клиентов с их комплектами."""
    header, cols = None, {}
    for i, row in enumerate(values[:10]):
        low = [c.strip().lower() for c in row]
        if any(c.startswith(("фио", "піб")) for c in low) and any("шин" in c for c in low):
            header = i
            for j, c in enumerate(low):
                if c.startswith(("фио", "піб")):
                    cols.setdefault("name", j)
                elif c.startswith("ряд"):
                    cols.setdefault("row", j)
                elif c.startswith(("мест", "місц")):
                    cols.setdefault("place", j)
                elif "шин" in c:
                    cols.setdefault("tires", j)
                elif c.startswith(("коммент", "комент")):
                    cols.setdefault("comment", j)
            break
    if header is None:
        raise ValueError("В таблице хранения не найдена шапка с колонками «ФИО» и «Шины»")

    def cell(r: list[str], key: str) -> str:
        j = cols.get(key)
        return r[j].strip() if j is not None and j < len(r) else ""

    by_name: dict[str, tuple[str, list[TireSet]]] = {}
    for r in values[header + 1:]:
        name = cell(r, "name")
        if not name:
            continue
        lines = split_tires(cell(r, "tires")) or [""]
        sets = [TireSet(ln, cell(r, "row"), cell(r, "place"), cell(r, "comment")) for ln in lines]
        by_name.setdefault(_norm(name), (name, []))[1].extend(sets)
    return [StoredClient(name, tuple(sets)) for name, sets in by_name.values()]


def search(clients: list[StoredClient], query: str, limit: int = 8) -> tuple[list[StoredClient], int]:
    """Клиенты, в ФИО которых есть все слова запроса. Возвращает (первые limit, всего)."""
    words = _norm(query).split()
    if not words:
        return [], 0
    hits = [c for c in clients if all(w in _norm(c.name) for w in words)]
    return hits[:limit], len(hits)


def place_label(s: TireSet) -> str:
    parts = [p for p in (f"ряд {s.row}" if s.row else "", f"место {s.place}" if s.place else "") if p]
    return ", ".join(parts)


def client_label(c: StoredClient) -> str:
    """Подпись клиента в результатах поиска."""
    if len(c.sets) > 1:
        return f"{c.name} · {len(c.sets)} компл."
    where = place_label(c.sets[0])
    return f"{c.name} · {where}" if where else c.name


def client_details(c: StoredClient) -> list[str]:
    """Что лежит у клиента — для показа оператору при выборе клиента."""
    places = {place_label(s) for s in c.sets}
    if len(places) == 1:
        place = places.pop()
        lines = ([f"📍 {place}"] if place else []) + [f"• {s.tires}" for s in c.sets if s.tires]
    else:
        lines = [f"• {s.tires or 'без описания'}" + (f" — {place_label(s)}" if place_label(s) else "")
                 for s in c.sets]
    comments = list(dict.fromkeys(s.comment for s in c.sets if s.comment))
    if comments:
        lines.append("💬 " + "; ".join(comments))
    return lines


def set_lines(c: StoredClient) -> list[str]:
    """Комплекты клиента строками; место — общей строкой, если оно у всех одно."""
    places = {place_label(s) for s in c.sets}
    if len(places) == 1:
        place = places.pop()
        return ([place] if place else []) + [s.tires for s in c.sets if s.tires]
    return [f"{s.tires} — {place_label(s)}" if place_label(s) else s.tires for s in c.sets if s.tires or place_label(s)]


def group_message(c: StoredClient, *, city: config.City, d, time_str: str, data: dict) -> str:
    """Сообщение в группу филиала — в том же стиле, что пишут операторы."""
    lines = [f"покупатель ({c.name})", *set_lines(c)]
    comments = list(dict.fromkeys(s.comment for s in c.sets if s.comment))
    if comments:
        lines.append("Комментарий: " + "; ".join(comments))
    lines.append("")
    lines.append(f"шм+хр {d:%d.%m} {time_str}")
    tail = [city.full_title]
    if data.get("order_number"):
        tail.append(f"заказ {data['order_number']}")
    if data.get("mop"):
        tail.append(f"МОП: {data['mop']}")
    lines.append(" · ".join(tail))
    return "\n".join(lines)


class StorageClient:
    """Читает таблицы хранения (через тот же сервисный аккаунт) с кешем."""

    def __init__(self, gc) -> None:
        self.gc = gc
        self._cache: dict[str, tuple[float, list[StoredClient]]] = {}

    def clients(self, city_key: str) -> list[StoredClient]:
        cfg = config.STORAGE[city_key]
        hit = self._cache.get(city_key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        ss = self.gc.open_by_key(cfg.sheet_id)
        ws = ss.worksheet(cfg.tab) if cfg.tab else ss.get_worksheet(0)
        data = parse_rows(ws.get_all_values())
        self._cache[city_key] = (time.time(), data)
        log.info("Таблица хранения %s: %d клиентов", city_key, len(data))
        return data

    def search(self, city_key: str, query: str) -> tuple[list[StoredClient], int]:
        return search(self.clients(city_key), query)
