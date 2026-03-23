from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Whitelist


class WhitelistMiddleware(BaseMiddleware):
    """Пропускает только пользователей из whitelist + admin."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        user_id = event.from_user.id if event.from_user else None
        if user_id is None:
            return

        # Всегда пропускаем admin
        if user_id == settings.admin_id:
            return await handler(event, data)

        session: AsyncSession = data.get("session")
        if session is None:
            return

        result = await session.execute(
            select(Whitelist).where(Whitelist.user_id == user_id)
        )
        if result.scalar_one_or_none() is None:
            await event.answer("⛔ У тебя нет доступа к боту.")
            return

        return await handler(event, data)
