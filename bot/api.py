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
from sqlalchemy import func, select

from bot.config import settings
from bot.db.models import UserCard, UserSquad, PackHistory, Player, TransferListing, TransferOffer, Whitelist
from bot.db.session import AsyncSessionLocal
from bot.services.simulation import compute_penalty

_SLOT_TO_POS = {
    "GK": "GK",
    "CB1": "CB", "CB2": "CB", "CB3": "CB",
    "LB": "LB", "RB": "RB",
    "CDM1": "CDM", "CDM2": "CDM",
    "CM": "CM", "CM1": "CM", "CM2": "CM", "CM3": "CM",
    "LM": "LM", "RM": "RM", "CAM": "CAM",
    "LW": "LW", "RW": "RW",
    "ST": "ST", "ST1": "ST", "ST2": "ST",
}


def _card_dict_with_penalty(card: UserCard, slot_name: str) -> dict:
    p = card.player
    slot_pos = _SLOT_TO_POS.get(slot_name, "CM")
    penalty = compute_penalty(p, slot_pos)
    effective = max(40, p.overall_rating + penalty)
    return {
        "card_id": card.id,
        "player_id": p.id,
        "name": p.name,
        "club": p.club,
        "position": p.position,
        "rating": p.overall_rating,
        "effective_rating": effective,
        "penalty": penalty,
        "photo": p.photo_url,
    }

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


# ─── Photo proxy ──────────────────────────────────────────────────────────────

async def proxy_photo(request: web.Request) -> web.Response:
    """GET /api/photo?url=... — проксирует фото с sofifa CDN."""
    import aiohttp as aio
    url = request.rel_url.query.get("url", "")
    if not url.startswith("https://cdn.sofifa.net/"):
        return web.Response(status=400)
    try:
        async with aio.ClientSession() as session:
            async with session.get(url, headers={"Referer": "https://sofifa.com/"}, timeout=aio.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return web.Response(status=404)
                data = await resp.read()
                return web.Response(body=data, content_type="image/png",
                                    headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return web.Response(status=502)


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
            "nationality": c.player.nationality,
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

    # Проверка на дублирующихся игроков
    card_ids = [int(v) for v in slots.values() if v is not None]
    if len(card_ids) != len(set(card_ids)):
        return web.json_response({"error": "duplicate players in lineup"}, status=400)

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


async def get_last_pack(request: web.Request) -> web.Response:
    """GET /api/lastpack?user_id=XXX — игроки из последнего открытого пака."""
    user_id_str = request.rel_url.query.get("user_id")
    if not user_id_str:
        return web.json_response({"error": "user_id required"}, status=400)
    try:
        user_id = int(user_id_str)
    except ValueError:
        return web.json_response({"error": "invalid user_id"}, status=400)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PackHistory)
            .where(PackHistory.user_id == user_id)
            .order_by(PackHistory.opened_at.desc())
            .limit(1)
        )
        pack = result.scalar_one_or_none()

    if not pack or not pack.player_ids:
        return web.json_response([])

    async with AsyncSessionLocal() as session:
        players = []
        for pid in pack.player_ids:
            p = await session.get(Player, pid)
            if p:
                players.append({
                    "id": p.id,
                    "name": p.name,
                    "club": p.club,
                    "position": p.position,
                    "rating": p.overall_rating,
                    "photo": p.photo_url,
                    "pack_type": pack.pack_type,
                    "opened_at": pack.opened_at.isoformat(),
                })

    return web.json_response(players)


def _card_dict(card: UserCard) -> dict:
    p = card.player
    return {
        "card_id": card.id,
        "player_id": p.id,
        "name": p.name,
        "club": p.club,
        "position": p.position,
        "rating": p.overall_rating,
        "photo": p.photo_url,
    }


async def get_market(request: web.Request) -> web.Response:
    """GET /api/market?user_id=XXX — все активные листинги кроме своих."""
    user_id_str = request.rel_url.query.get("user_id")
    user_id = int(user_id_str) if user_id_str else None

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TransferListing).where(TransferListing.status == "active")
        )
        listings = result.scalars().all()

        # Загружаем юзернеймы
        wl_result = await session.execute(select(Whitelist))
        wl_map = {w.user_id: w.username for w in wl_result.scalars().all()}

    data = []
    for l in listings:
        data.append({
            "listing_id": l.id,
            "owner_id": l.user_id,
            "owner": wl_map.get(l.user_id, f"ID{l.user_id}"),
            "is_mine": l.user_id == user_id,
            "card": _card_dict(l.card),
            "created_at": l.created_at.isoformat(),
        })
    return web.json_response(data)


async def post_list_card(request: web.Request) -> web.Response:
    """POST /api/market/list — выставить карточку на рынок. {user_id, card_id}"""
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    card_id = int(body.get("card_id", 0))
    if not user_id or not card_id:
        return web.json_response({"error": "user_id and card_id required"}, status=400)

    async with AsyncSessionLocal() as session:
        card = await session.get(UserCard, card_id)
        if not card or card.user_id != user_id:
            return web.json_response({"error": "Карточка не найдена"}, status=400)

        # Проверяем нет ли уже активного листинга этой карточки
        existing = await session.execute(
            select(TransferListing).where(
                TransferListing.card_id == card_id,
                TransferListing.status == "active",
            )
        )
        if existing.scalar_one_or_none():
            return web.json_response({"error": "Карточка уже на рынке"}, status=400)

        # Проверяем лимит 3 активных листинга
        count_result = await session.execute(
            select(func.count()).select_from(TransferListing).where(
                TransferListing.user_id == user_id,
                TransferListing.status == "active",
            )
        )
        if count_result.scalar() >= 3:
            return web.json_response({"error": "Максимум 3 карточки на рынке одновременно"}, status=400)

        # Получаем username для объявления
        wl = await session.get(Whitelist, user_id)
        username = wl.username if wl and wl.username else f"ID{user_id}"

        listing = TransferListing(user_id=user_id, card_id=card_id, status="active")
        session.add(listing)
        await session.commit()
        await session.refresh(listing)

        player = card.player
        pos = player.position
        rating = player.overall_rating

    # Объявление в беседу
    try:
        bot = request.app["bot"]
        text = (
            f"🔄 <b>{username}</b> выставил карточку на трансферный рынок!\n"
            f"├ <b>{player.name}</b> — {rating} ⭐\n"
            f"└ Позиция: {pos}\n\n"
            f"Открой мини-приложение, чтобы предложить обмен."
        )
        await bot.send_message(settings.group_id, text)
    except Exception:
        pass

    return web.json_response({"ok": True, "listing_id": listing.id})


async def post_cancel_listing(request: web.Request) -> web.Response:
    """POST /api/market/cancel — снять карточку с рынка. {user_id, listing_id}"""
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    listing_id = int(body.get("listing_id", 0))

    async with AsyncSessionLocal() as session:
        listing = await session.get(TransferListing, listing_id)
        if not listing or listing.user_id != user_id:
            return web.json_response({"error": "Листинг не найден"}, status=400)
        if listing.status != "active":
            return web.json_response({"error": "Листинг уже неактивен"}, status=400)

        # Отменяем все pending офферы на этот листинг
        offers = await session.execute(
            select(TransferOffer).where(
                TransferOffer.want_card_id == listing.card_id,
                TransferOffer.to_user_id == user_id,
                TransferOffer.status == "pending",
            )
        )
        for offer in offers.scalars().all():
            offer.status = "cancelled"

        listing.status = "cancelled"
        await session.commit()

    return web.json_response({"ok": True})


async def post_make_offer(request: web.Request) -> web.Response:
    """POST /api/market/offer — предложить свою карточку в обмен. {user_id, listing_id, offer_card_id}"""
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    listing_id = int(body.get("listing_id", 0))
    offer_card_id = int(body.get("offer_card_id", 0))

    async with AsyncSessionLocal() as session:
        listing = await session.get(TransferListing, listing_id)
        if not listing or listing.status != "active":
            return web.json_response({"error": "Листинг не найден или неактивен"}, status=400)
        if listing.user_id == user_id:
            return web.json_response({"error": "Нельзя делать оффер на свой листинг"}, status=400)

        offer_card = await session.get(UserCard, offer_card_id)
        if not offer_card or offer_card.user_id != user_id:
            return web.json_response({"error": "Карточка не найдена в твоей коллекции"}, status=400)

        # Проверяем нет ли уже оффера от этого пользователя на этот листинг
        existing = await session.execute(
            select(TransferOffer).where(
                TransferOffer.from_user_id == user_id,
                TransferOffer.want_card_id == listing.card_id,
                TransferOffer.status == "pending",
            )
        )
        if existing.scalar_one_or_none():
            return web.json_response({"error": "Ты уже сделал оффер на эту карточку"}, status=400)

        offer = TransferOffer(
            from_user_id=user_id,
            to_user_id=listing.user_id,
            offer_card_id=offer_card_id,
            want_card_id=listing.card_id,
            status="pending",
        )
        session.add(offer)
        await session.commit()
        await session.refresh(offer)

    return web.json_response({"ok": True, "offer_id": offer.id})


async def post_cancel_offer(request: web.Request) -> web.Response:
    """POST /api/market/cancel-offer — отменить свой оффер. {user_id, offer_id}"""
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    offer_id = int(body.get("offer_id", 0))

    async with AsyncSessionLocal() as session:
        offer = await session.get(TransferOffer, offer_id)
        if not offer or offer.from_user_id != user_id:
            return web.json_response({"error": "Оффер не найден"}, status=400)
        if offer.status != "pending":
            return web.json_response({"error": "Оффер уже неактивен"}, status=400)
        offer.status = "cancelled"
        await session.commit()

    return web.json_response({"ok": True})


async def get_listing_offers(request: web.Request) -> web.Response:
    """GET /api/market/offers?user_id=XXX — офферы на мои листинги."""
    user_id_str = request.rel_url.query.get("user_id")
    user_id = int(user_id_str) if user_id_str else None
    if not user_id:
        return web.json_response({"error": "user_id required"}, status=400)

    async with AsyncSessionLocal() as session:
        # Мои активные листинги
        listings_result = await session.execute(
            select(TransferListing).where(
                TransferListing.user_id == user_id,
                TransferListing.status == "active",
            )
        )
        listings = {l.card_id: l for l in listings_result.scalars().all()}

        if not listings:
            return web.json_response([])

        # Офферы на мои карточки
        offers_result = await session.execute(
            select(TransferOffer).where(
                TransferOffer.to_user_id == user_id,
                TransferOffer.want_card_id.in_(list(listings.keys())),
                TransferOffer.status == "pending",
            )
        )
        offers = offers_result.scalars().all()

        wl_result = await session.execute(select(Whitelist))
        wl_map = {w.user_id: w.username for w in wl_result.scalars().all()}

    data = []
    for o in offers:
        listing = listings.get(o.want_card_id)
        data.append({
            "offer_id": o.id,
            "listing_id": listing.id if listing else None,
            "from_user": wl_map.get(o.from_user_id, f"ID{o.from_user_id}"),
            "offer_card": _card_dict(o.offer_card),
            "want_card": _card_dict(o.want_card),
        })
    return web.json_response(data)


async def post_accept_offer(request: web.Request) -> web.Response:
    """POST /api/market/accept — принять оффер. {user_id, offer_id}"""
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    offer_id = int(body.get("offer_id", 0))

    from bot.services.transfers import accept_transfer
    announce_data = None
    async with AsyncSessionLocal() as session:
        # Собираем данные для объявления до accept (пока offer ещё pending)
        offer_pre = await session.get(TransferOffer, offer_id)
        if offer_pre and offer_pre.to_user_id == user_id:
            wl_from = await session.get(Whitelist, offer_pre.from_user_id)
            wl_to = await session.get(Whitelist, user_id)
            from_name = wl_from.username if wl_from and wl_from.username else f"ID{offer_pre.from_user_id}"
            to_name = wl_to.username if wl_to and wl_to.username else f"ID{user_id}"
            offer_card = await session.get(UserCard, offer_pre.offer_card_id)
            want_card = await session.get(UserCard, offer_pre.want_card_id)
            if offer_card and want_card:
                announce_data = (from_name, to_name, offer_card.player, want_card.player)

        ok, msg = await accept_transfer(session, offer_id, user_id)
        if not ok:
            return web.json_response({"error": msg}, status=400)

        # Помечаем листинг как taken
        offer = await session.get(TransferOffer, offer_id)
        if offer:
            listing_result = await session.execute(
                select(TransferListing).where(
                    TransferListing.card_id == offer.want_card_id,
                    TransferListing.status == "active",
                )
            )
            listing = listing_result.scalar_one_or_none()
            if listing:
                listing.status = "taken"
            # Отменяем остальные офферы на этот листинг
            other_offers = await session.execute(
                select(TransferOffer).where(
                    TransferOffer.want_card_id == offer.want_card_id,
                    TransferOffer.status == "pending",
                    TransferOffer.id != offer_id,
                )
            )
            for o in other_offers.scalars().all():
                o.status = "cancelled"
            await session.commit()

    # Объявление об обмене в беседу
    if announce_data:
        try:
            from_name, to_name, offer_player, want_player = announce_data
            bot = request.app["bot"]
            text = (
                f"🤝 Трансфер состоялся!\n"
                f"├ <b>{from_name}</b> отдаёт: <b>{offer_player.name}</b> — {offer_player.overall_rating} ⭐\n"
                f"└ <b>{to_name}</b> отдаёт: <b>{want_player.name}</b> — {want_player.overall_rating} ⭐"
            )
            await bot.send_message(settings.group_id, text)
        except Exception:
            pass

    return web.json_response({"ok": True})


async def get_squad_full(request: web.Request) -> web.Response:
    """GET /api/squad_full?user_id=XXX — состав с штрафами и effective_rating."""
    user_id_str = request.rel_url.query.get("user_id")
    if not user_id_str:
        return web.json_response({"error": "user_id required"}, status=400)
    try:
        user_id = int(user_id_str)
    except ValueError:
        return web.json_response({"error": "invalid user_id"}, status=400)

    from sqlalchemy.orm import joinedload as jl
    async with AsyncSessionLocal() as session:
        squad = await session.get(UserSquad, user_id)
        if not squad:
            return web.json_response({"formation": "4-4-2", "slots": []})
        assignments = squad.slot_assignments
        slots_out = []
        for slot_name, card_id in assignments.items():
            card = await session.get(UserCard, card_id, options=[jl(UserCard.player)])
            if card:
                slots_out.append({
                    "slot": slot_name,
                    "card_id": card_id,
                    "player": _card_dict_with_penalty(card, slot_name),
                })
    return web.json_response({"formation": squad.formation, "slots": slots_out})


async def get_users(request: web.Request) -> web.Response:
    """GET /api/users — список игроков вайтлиста."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Whitelist))
        users = result.scalars().all()
    return web.json_response([
        {"user_id": u.user_id, "username": u.username or f"ID{u.user_id}"}
        for u in users
    ])


async def get_opponent_squad(request: web.Request) -> web.Response:
    """GET /api/opponent_squad?user_id=XXX — полный состав соперника с именами игроков."""
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
            return web.json_response({"formation": "4-4-2", "slots": []})

        assignments = squad.slot_assignments  # {slot: user_card_id}
        slots_out = []
        for slot_name, card_id in assignments.items():
            card = await session.get(UserCard, card_id, options=[__import__('sqlalchemy.orm', fromlist=['joinedload']).joinedload(UserCard.player)])
            if card:
                slots_out.append({
                    "slot": slot_name,
                    "card_id": card_id,
                    "player": _card_dict_with_penalty(card, slot_name),
                })

    return web.json_response({"formation": squad.formation, "slots": slots_out})


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
    app.router.add_get("/api/photo", proxy_photo)
    app.router.add_get("/api/lastpack", get_last_pack)
    app.router.add_get("/api/users", get_users)
    app.router.add_get("/api/opponent_squad", get_opponent_squad)
    app.router.add_get("/api/squad_full", get_squad_full)
    app.router.add_get("/api/market", get_market)
    app.router.add_post("/api/market/list", post_list_card)
    app.router.add_post("/api/market/cancel", post_cancel_listing)
    app.router.add_post("/api/market/offer", post_make_offer)
    app.router.add_post("/api/market/cancel-offer", post_cancel_offer)
    app.router.add_get("/api/market/offers", get_listing_offers)
    app.router.add_post("/api/market/accept", post_accept_offer)
    for path in ["/api/market/list", "/api/market/cancel", "/api/market/offer",
                 "/api/market/cancel-offer", "/api/market/accept"]:
        app.router.add_options(path, lambda r: web.Response())

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
