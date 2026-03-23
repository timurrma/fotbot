"""Команды в личных сообщениях."""
import asyncio
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select

from bot.config import settings
from bot.db.models import UserCard, Whitelist
from bot.db.session import AsyncSessionLocal
from bot.services.packs import has_starter_pack, open_pack, send_pack_with_photos
from bot.services.simulation import FORMATIONS_SLOTS
from bot.services.stats import format_scorers, get_tournament_record, get_user_stats
from bot.services.transfers import (
    accept_transfer,
    create_transfer_offer,
    decline_transfer,
    get_outgoing_offers,
    get_pending_offers,
    get_remaining_transfers,
)

router = Router()


# ─── /start ───────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        wl = await session.get(Whitelist, user_id)
        if not wl and user_id != settings.admin_id:
            await message.answer(
                "⛔ У тебя нет доступа к боту.\n"
                "Попроси администратора добавить тебя командой /adduser."
            )
            return

        already_has = await has_starter_pack(session, user_id)

        # Если pack_history есть но карточек нет — выдаём заново
        if already_has:
            cards_result = await session.execute(
                select(UserCard).where(UserCard.user_id == user_id).limit(1)
            )
            if not cards_result.scalar_one_or_none():
                already_has = False

        if not already_has:
            await message.answer("👋 Добро пожаловать! Открываю твой стартовый пак...")
            await asyncio.sleep(1)
            players = await open_pack(session, user_id, pack_type="starter")

    if not already_has:
        username = message.from_user.username or message.from_user.full_name
        await send_pack_with_photos(message.bot, message.chat.id, username, players, "starter")
        await send_pack_with_photos(message.bot, settings.group_id, username, players, "starter")
    else:
        await message.answer(
            "👋 С возвращением!\n\n"
            "📋 Команды:\n"
            "/mystats — моя статистика"
        )


# ─── /mystats ─────────────────────────────────────────────────────────────────

@router.message(Command("mystats"))
async def cmd_mystats(message: Message) -> None:
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        rows = await get_user_stats(session, user_id, limit=10)
        record = await get_tournament_record(session, user_id)

    record_text = (
        f"🏆 Рекорд: {record['wins']}В {record['draws']}Н {record['losses']}П "
        f"({record['played']} матчей)\n\n"
    )
    await message.answer(record_text + format_scorers(rows, "⚽ Твои бомбардиры"), parse_mode="Markdown")


# ─── /transfers ───────────────────────────────────────────────────────────────

@router.message(Command("transfers"))
async def cmd_transfers(message: Message) -> None:
    user_id = message.from_user.id
    async with AsyncSessionLocal() as session:
        incoming = await get_pending_offers(session, user_id)
        outgoing = await get_outgoing_offers(session, user_id)
        remaining = await get_remaining_transfers(session, user_id)

    lines = [f"🔄 Трансферы (осталось: {remaining}/3)\n"]

    if incoming:
        lines.append("📨 Входящие предложения:")
        for offer in incoming:
            lines.append(
                f"  #{offer.id} от ID{offer.from_user_id}: "
                f"{offer.offer_card.player.name} (#{offer.offer_card_id}) "
                f"→ {offer.want_card.player.name} (#{offer.want_card_id})"
            )
            lines.append(f"  /accept_{offer.id}  |  /decline_{offer.id}")

    if outgoing:
        lines.append("\n📤 Исходящие предложения:")
        for offer in outgoing:
            lines.append(
                f"  #{offer.id} для ID{offer.to_user_id}: "
                f"{offer.offer_card.player.name} (#{offer.offer_card_id}) "
                f"→ {offer.want_card.player.name} (#{offer.want_card_id})"
            )

    if not incoming and not outgoing:
        lines.append(
            "Нет активных предложений.\n\n"
            "Чтобы предложить обмен:\n"
            "/maketransfer <user_id> <своя_card_id> <его_card_id>\n\n"
            "ID карточек видны в Mini App (кнопка Menu) или в /transfers."
        )

    await message.answer("\n".join(lines))


@router.message(F.text.regexp(r"^/accept_(\d+)$"))
async def cmd_accept(message: Message) -> None:
    offer_id = int(message.text.split("_")[1])
    async with AsyncSessionLocal() as session:
        ok, msg = await accept_transfer(session, offer_id, message.from_user.id)
    await message.answer(f"{'✅' if ok else '❌'} {msg}")


@router.message(F.text.regexp(r"^/decline_(\d+)$"))
async def cmd_decline(message: Message) -> None:
    offer_id = int(message.text.split("_")[1])
    async with AsyncSessionLocal() as session:
        ok, msg = await decline_transfer(session, offer_id, message.from_user.id)
    await message.answer(f"{'✅' if ok else '❌'} {msg}")


@router.message(Command("transfer"))
async def cmd_transfer(message: Message) -> None:
    await message.answer(
        "Чтобы предложить обмен:\n\n"
        "/maketransfer <to_user_id> <своя_card_id> <его_card_id>\n\n"
        "ID карточек видны в Mini App (кнопка Menu).\n"
        "Пример: /maketransfer 123456789 42 87"
    )


@router.message(Command("maketransfer"))
async def cmd_maketransfer(message: Message) -> None:
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("Использование: /maketransfer <to_user_id> <своя_card_id> <его_card_id>")
        return

    try:
        to_user_id = int(parts[1])
        offer_card_id = int(parts[2])
        want_card_id = int(parts[3])
    except ValueError:
        await message.answer("Все параметры должны быть числами.")
        return

    async with AsyncSessionLocal() as session:
        ok, result = await create_transfer_offer(
            session,
            from_user_id=message.from_user.id,
            to_user_id=to_user_id,
            offer_card_id=offer_card_id,
            want_card_id=want_card_id,
        )

    if ok:
        await message.answer(f"✅ Предложение #{result} отправлено!")
        try:
            await message.bot.send_message(
                to_user_id,
                f"📨 Новое предложение обмена от ID{message.from_user.id}!\n"
                f"Используй /transfers чтобы принять или отклонить."
            )
        except Exception:
            pass
    else:
        await message.answer(f"❌ {result}")
