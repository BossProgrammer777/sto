"""
Прайс шиномонтажа (комплект, 4 колеса) — как на сайте, по городам.
Бот показывает цену в сводке записи: по диаметру и типу авто (если тип не
указан — цены и для легкового, и для кроссовера), плюс надбавки за низкий
профиль (45% и меньше) и Run-Flat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import config


@dataclass(frozen=True)
class PriceList:
    car: tuple[tuple[int, int, int], ...]  # легковые: (от R, до R, грн)
    suv: tuple[tuple[int, int, int], ...]  # кроссоверы, внедорожники, минивэны, фургоны, малые микроавтобусы
    low_profile: tuple[int, str]           # профиль 45% и меньше: (грн, «шину» | «комплект»)
    run_flat: int                          # Run-Flat: грн за комплект
    bus_extra: tuple[int, str]             # Газель/грузовые микроавтобусы: надбавка к цене кроссовера


PRICES: dict[str, PriceList] = {
    # Киев; у Софиевской Борщагивки на сайте отдельная таблица, но цифры те же.
    "kyiv": PriceList(
        car=((12, 14, 1360), (15, 16, 1500), (17, 18, 1600), (19, 20, 1860), (21, 22, 2100)),
        suv=((13, 14, 1540), (15, 16, 1660), (17, 18, 1800), (19, 20, 2100), (21, 22, 2300)),
        low_profile=(75, "шину"), run_flat=400, bus_extra=(400, "комплект")),
    "kharkiv": PriceList(
        car=((12, 14, 950), (15, 16, 1050), (17, 18, 1350), (19, 20, 1550), (21, 22, 1800)),
        suv=((15, 16, 1300), (17, 18, 1450), (19, 20, 1800), (21, 22, 2200), (23, 24, 2400)),
        low_profile=(60, "шину"), run_flat=200, bus_extra=(100, "шину")),
    "dnipro": PriceList(
        car=((12, 14, 1200), (15, 16, 1400), (17, 18, 1600), (19, 19, 1800), (20, 20, 2000),
             (21, 22, 2200)),
        suv=((15, 16, 1500), (17, 18, 1700), (19, 20, 2100), (21, 22, 2400)),
        low_profile=(250, "комплект"), run_flat=300, bus_extra=(150, "комплект")),
}

# Пост → прайс. Львова пока нет — для него цена не показывается.
PRICE_OF: dict[str, str] = {
    "kyiv": "kyiv", "kyiv_p2": "kyiv", "kyiv_terekhina": "kyiv", "sofiyivska": "kyiv",
    "kharkiv": "kharkiv", "kharkiv_maidan": "kharkiv",
    "dnipro": "dnipro",
}


def _grn(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " грн"


def _find(rows: tuple[tuple[int, int, int], ...], r: int) -> int | None:
    return next((p for lo, hi, p in rows if lo <= r <= hi), None)


def _kind(car_type: str) -> str:
    """Тип авто из таблицы → car | suv | bus | «» (не указан)."""
    t = (car_type or "").lower()
    if any(k in t for k in ("большой", "газел", "груз")):
        return "bus"
    if any(k in t for k in ("кросс", "паркет", "внедор", "минив", "микроавт", "фургон")):
        return "suv"
    return "car" if "легк" in t else ""


def _per_set(extra: tuple[int, str]) -> int:
    grn, per = extra
    return grn * 4 if per == "шину" else grn


def price_lines(city: config.City, diameter: str, car_type: str = "") -> list[str]:
    """Строки с ценой для сводки записи (пусто, если прайса/диаметра нет)."""
    p = PRICES.get(PRICE_OF.get(city.key, ""))
    m = re.fullmatch(r"[RrРр]\s*(\d{2})", (diameter or "").strip())
    if not p or not m:
        return []
    r = int(m.group(1))
    car, suv = _find(p.car, r), _find(p.suv, r)
    kind = _kind(car_type)
    head = f"💰 Шиномонтаж {diameter} (4 колеса)"
    if kind == "car" and car:
        lines = [f"{head}, легковой: {_grn(car)}"]
    elif kind == "suv" and suv:
        lines = [f"{head}, {car_type.strip().lower()}: {_grn(suv)}"]
    elif kind == "bus" and suv:
        lines = [f"{head}, {car_type.strip().lower()}: {_grn(suv + _per_set(p.bus_extra))}"]
    elif car or suv:  # тип авто не указан (или на него нет цены) — обе цены
        lines = [f"{head}:"]
        if car:
            lines.append(f"• легковой — {_grn(car)}")
        if suv:
            lines.append(f"• кроссовер / минивэн / микроавтобус — {_grn(suv)}")
    else:
        return []
    grn, per = p.low_profile
    low = f"+{_grn(grn)} за шину (+{_grn(grn * 4)} за комплект)" if per == "шину" else f"+{_grn(grn)} за комплект"
    lines.append(f"➕ Профиль 45% и меньше: {low}")
    lines.append(f"➕ Run-Flat: +{_grn(p.run_flat)} за комплект")
    return lines
