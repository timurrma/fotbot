"""
Простой HTTP API для Telegram Mini App (настройка состава).

Эндпоинты:
  GET  /api/cards?user_id=XXX          — коллекция карточек пользователя
  GET  /api/squad?user_id=XXX          — текущий состав
  POST /api/squad                       — сохранить состав {user_id, formation, slots}
"""
from __future__ import annotations

import hmac
import hashlib
import json
import logging
from typing import Optional
from urllib.parse import parse_qsl

from aiohttp import web
from sqlalchemy import select

from bot.config import settings
from bot.db.models import UserCard, UserSquad
from bot.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


# ─── Проверка initData от Telegram ────────────────────────────────────────────

def verify_telegram_init_data(init_data: str) -> Optional[dict]:
    """
    Верифицирует initData из Telegram Mini App.
    Возвращает распарсенный dict или None если подпись неверна.
    """
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = params.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(params.items())
    )
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        return None

    user_data = params.get("user")
    if user_data:
        try:
            return json.loads(user_data)
        except Exception:
            return None
    return params


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def get_cards(request: web.Request) -> web.Response:
    """GET /api/cards?user_id=XXX — коллекция карточек."""
    user_id_str = request.rel_url.query.get("user_id")
    if not user_id_str:
        return web.json_response({"error": "user_id required"}, status=400)
    try:
        user_id = int(user_id_str)
    except ValueError:
        return web.json_response({"error": "invalid user_id"}, status=400)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserCard).where(UserCard.user_id == user_id)
        )
        cards = result.scalars().all()

    data = [
        {
            "id": c.id,
            "player_id": c.player_id,
            "name": c.player.name,
            "club": c.player.club,
            "position": c.player.position,
            "positions": c.player.positions,
            "rating": c.player.overall_rating,
            "photo": c.player.photo_url,
            "national": c.player.is_national_team,
        }
        for c in cards
    ]
    return web.json_response(data)


async def get_squad(request: web.Request) -> web.Response:
    """GET /api/squad?user_id=XXX — текущий состав."""
    user_id_str = request.rel_url.query.get("user_id")
    if not user_id_str:
        return web.json_response({"error": "user_id required"}, status=400)
    try:
        user_id = int(user_id_str)
    except ValueError:
        return web.json_response({"error": "invalid user_id"}, status=400)

    async with AsyncSessionLocal() as session:
        squad = await session.get(UserSquad, user_id)

    if not squad:
        return web.json_response({"formation": "4-4-2", "slots": {}})

    return web.json_response({
        "formation": squad.formation,
        "slots": squad.slot_assignments,
    })


async def save_squad(request: web.Request) -> web.Response:
    """POST /api/squad — сохранить состав."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    # Верификация через initData или простой user_id (для разработки)
    init_data = body.get("initData", "")
    user_id = None

    if init_data:
        user_info = verify_telegram_init_data(init_data)
        if user_info:
            user_id = user_info.get("id")

    # Fallback: доверяем user_id из тела (для локальной разработки)
    if not user_id:
        user_id = body.get("user_id")

    if not user_id:
        return web.json_response({"error": "unauthorized"}, status=401)

    formation = body.get("formation", "4-4-2")
    slots = body.get("slots", {})

    if not isinstance(slots, dict):
        return web.json_response({"error": "slots must be object"}, status=400)

    async with AsyncSessionLocal() as session:
        squad = await session.get(UserSquad, int(user_id))
        if squad:
            squad.formation = formation
            squad.slot_assignments = {k: int(v) for k, v in slots.items()}
        else:
            squad = UserSquad(
                user_id=int(user_id),
                formation=formation,
            )
            squad.slot_assignments = {k: int(v) for k, v in slots.items()}
            session.add(squad)
        await session.commit()

    return web.json_response({"ok": True})


# ─── App factory ──────────────────────────────────────────────────────────────

def create_api_app() -> web.Application:
    app = web.Application()

    # CORS для Mini App (Vercel / GitHub Pages)
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                }
            )
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    app.middlewares.append(cors_middleware)
    app.router.add_get("/api/cards", get_cards)
    app.router.add_get("/api/squad", get_squad)
    app.router.add_post("/api/squad", save_squad)
    app.router.add_options("/api/squad", lambda r: web.Response())

    # Раздаём Mini App (index.html) по корневому пути
    import os
    miniapp_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "miniapp", "index.html")

    async def serve_miniapp(request: web.Request) -> web.Response:
        return web.FileResponse(miniapp_path)

    app.router.add_get("/", serve_miniapp)
    app.router.add_get("/miniapp", serve_miniapp)

    return app


async def start_api_server(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Запускает API сервер в фоне (вызывается из main.py)."""
    if port is None:
        port = settings.api_port
    app = create_api_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"API сервер запущен на {host}:{port}")
