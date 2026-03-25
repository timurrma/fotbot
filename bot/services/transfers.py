"""Логика трансферов: обмен карточками между игроками."""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import TransferCount, TransferOffer, UserCard

MAX_TRANSFERS_PER_WEEK = 3


async def _get_current_week() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    return now.isocalendar()[1], now.year


async def get_remaining_transfers(session: AsyncSession, user_id: int) -> int:
    week, year = await _get_current_week()
    row = await session.get(TransferCount, user_id)
    if not row or row.week_number != week or row.year != year:
        return MAX_TRANSFERS_PER_WEEK
    return max(0, MAX_TRANSFERS_PER_WEEK - row.count)


async def _increment_transfer_count(session: AsyncSession, user_id: int) -> None:
    week, year = await _get_current_week()
    row = await session.get(TransferCount, user_id)
    if not row or row.week_number != week or row.year != year:
        if row:
            row.week_number = week
            row.year = year
            row.count = 1
        else:
            row = TransferCount(user_id=user_id, week_number=week, year=year, count=1)
            session.add(row)
    else:
        row.count += 1


async def create_transfer_offer(
    session: AsyncSession,
    from_user_id: int,
    to_user_id: int,
    offer_card_id: int,
    want_card_id: int,
) -> tuple[bool, str]:
    """
    Создаёт предложение обмена.
    Возвращает (success, message).
    """
    # Проверяем принадлежность карточек
    offer_card = await session.get(UserCard, offer_card_id)
    want_card = await session.get(UserCard, want_card_id)

    if not offer_card or offer_card.user_id != from_user_id:
        return False, "Карточка для обмена не найдена в твоей коллекции."
    if not want_card or want_card.user_id != to_user_id:
        return False, "Запрашиваемая карточка не найдена у этого игрока."

    # Проверяем нет ли уже активного предложения
    existing = await session.execute(
        select(TransferOffer).where(
            TransferOffer.from_user_id == from_user_id,
            TransferOffer.offer_card_id == offer_card_id,
            TransferOffer.status == "pending",
        )
    )
    if existing.scalar_one_or_none():
        return False, "У тебя уже есть активное предложение с этой карточкой."

    offer = TransferOffer(
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        offer_card_id=offer_card_id,
        want_card_id=want_card_id,
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    session.add(offer)
    await session.commit()
    await session.refresh(offer)
    return True, str(offer.id)


async def accept_transfer(
    session: AsyncSession,
    offer_id: int,
    accepting_user_id: int,
) -> tuple[bool, str]:
    """Принимает предложение обмена, меняет карточки местами."""
    offer = await session.get(TransferOffer, offer_id)
    if not offer:
        return False, "Предложение не найдено."
    if offer.to_user_id != accepting_user_id:
        return False, "Это предложение не для тебя."
    if offer.status != "pending":
        return False, "Предложение уже неактивно."

    offer_card = await session.get(UserCard, offer.offer_card_id)
    want_card = await session.get(UserCard, offer.want_card_id)

    if not offer_card or not want_card:
        offer.status = "cancelled"
        await session.commit()
        return False, "Одна из карточек больше не существует."

    # Меняем владельцев
    offer_card.user_id = accepting_user_id
    want_card.user_id = offer.from_user_id

    offer.status = "accepted"

    # Списываем трансфер у инициатора
    await _increment_transfer_count(session, offer.from_user_id)

    await session.commit()
    return True, "Обмен выполнен!"


async def decline_transfer(
    session: AsyncSession,
    offer_id: int,
    declining_user_id: int,
) -> tuple[bool, str]:
    """Отклоняет предложение."""
    offer = await session.get(TransferOffer, offer_id)
    if not offer:
        return False, "Предложение не найдено."
    if offer.to_user_id != declining_user_id and offer.from_user_id != declining_user_id:
        return False, "Это не твоё предложение."
    if offer.status != "pending":
        return False, "Предложение уже неактивно."

    offer.status = "declined"
    await session.commit()
    return True, "Предложение отклонено."


async def get_pending_offers(
    session: AsyncSession,
    user_id: int,
) -> list[TransferOffer]:
    """Активные предложения для пользователя (входящие)."""
    result = await session.execute(
        select(TransferOffer).where(
            TransferOffer.to_user_id == user_id,
            TransferOffer.status == "pending",
        )
    )
    return list(result.scalars().all())


async def get_outgoing_offers(
    session: AsyncSession,
    user_id: int,
) -> list[TransferOffer]:
    """Исходящие активные предложения пользователя."""
    result = await session.execute(
        select(TransferOffer).where(
            TransferOffer.from_user_id == user_id,
            TransferOffer.status == "pending",
        )
    )
    return list(result.scalars().all())
