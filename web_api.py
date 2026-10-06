"""
Публичный API расписания для сайта (только чтение).

GET /api/days?city=<key>  → {"city", "updated", "days": {"YYYY-MM-DD": [{t, c, b}, ...]}}
    c — цвет слота (W белый / O оранжевый / R красный), b — занят ли слот.
    Номера заказов, телефоны и имена МОП наружу не отдаются.
GET /api/health           → {"ok": true}

Запускается в том же процессе и event loop, что и бот (см. bot.py, post_init).
Порт — из переменной PORT (Railway), по умолчанию 8080.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from aiohttp import web

import calendar_utils as cal
import config

log = logging.getLogger("web-api")

DAYS_CACHE_TTL = 30  # сек — ответ по городу кешируется, чтобы не дёргать таблицу на каждый заход


@web.middleware
async def _cors(request: web.Request, handler):
    resp = web.Response() if request.method == "OPTIONS" else await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def build_web_app(sheets) -> web.Application:
    app = web.Application(middlewares=[_cors])
    cache: dict[str, tuple[float, dict]] = {}
    locks: dict[str, asyncio.Lock] = {}

    async def days(request: web.Request) -> web.Response:
        key = request.query.get("city", "")
        city = config.CITIES.get(key)
        if city is None:
            return web.json_response({"error": "unknown city"}, status=400)
        lock = locks.setdefault(key, asyncio.Lock())
        async with lock:  # одновременные запросы по одному городу читают таблицу один раз
            hit = cache.get(key)
            if not hit or time.time() - hit[0] >= DAYS_CACHE_TTL:
                try:
                    grid = await asyncio.to_thread(sheets.days_for_city, city)
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось прочитать расписание для %s", key)
                    return web.json_response({"error": "schedule unavailable"}, status=503)
                hit = (time.time(), {
                    "city": key,
                    "updated": cal.now().isoformat(timespec="seconds"),
                    "days": grid,
                })
                cache[key] = hit
        return web.json_response(hit[1])

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/api/days", days)
    app.router.add_get("/api/health", health)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", health)
    return app


async def start_web_api(sheets) -> web.AppRunner:
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(build_web_app(sheets))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Web API расписания слушает порт %s (GET /api/days?city=...)", port)
    return runner
