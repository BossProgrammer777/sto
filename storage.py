"""
Таблица хранения шин филиала: поиск клиента по ФИО и текст сообщения в группу.

Ожидаемые колонки (ищутся по заголовку, порядок любой):
    ФИО | Ряд | Место | Шины | Комментарий
В «Шины» может быть несколько строк (несколько комплектов).
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
class StoredClient:
    name: str
    row: str
    place: str
    tires: str
    comment: str


def _norm(s: str) -> str:
    s = s.replace("#", " ").replace("ё", "е").replace("Ё", "Е").replace("’", "'").replace("ʼ", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def parse_rows(values: list[list[str]]) -> list[StoredClient]:
    """Разобрать значения листа хранения в список клиентов."""
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

    out = []
    for r in values[header + 1:]:
        name = cell(r, "name")
        if name:
            out.append(StoredClient(name, cell(r, "row"), cell(r, "place"), cell(r, "tires"), cell(r, "comment")))
    return out


def search(clients: list[StoredClient], query: str, limit: int = 8) -> tuple[list[StoredClient], int]:
    """Клиенты, в ФИО которых есть все слова запроса. Возвращает (первые limit, всего)."""
    words = _norm(query).split()
    if not words:
        return [], 0
    hits = [c for c in clients if all(w in _norm(c.name) for w in words)]
    return hits[:limit], len(hits)


def place_label(c: StoredClient) -> str:
    parts = [p for p in (f"ряд {c.row}" if c.row else "", f"место {c.place}" if c.place else "") if p]
    return ", ".join(parts)


def group_message(c: StoredClient, *, city: config.City, d, time_str: str, data: dict) -> str:
    """Сообщение в группу филиала — в том же стиле, что пишут операторы."""
    lines = [f"покупатель ({c.name})"]
    if place_label(c):
        lines.append(place_label(c))
    if c.tires:
        lines.append(c.tires)
    if c.comment:
        lines.append(f"Комментарий: {c.comment}")
    lines.append("")
    lines.append(f"шм+хр {d:%d.%m} {time_str}")
    tail = [city.title]
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
