"""Точка входа бота."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.api import start_api_server
from bot.config import settings
from bot.db.session import AsyncSessionLocal, init_db
from bot.handlers import admin, group, private
from bot.middleware import WhitelistMiddleware
from bot.scheduler import create_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    await init_db()
    logger.info("БД инициализирована")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()

    # Middleware — инжектим сессию в каждый апдейт
    from aiogram import BaseMiddleware
    from aiogram.types import TelegramObject
    from typing import Any, Awaitable, Callable

    class SessionMiddleware(BaseMiddleware):
        async def __call__(
            self,
            handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: dict[str, Any],
        ) -> Any:
            async with AsyncSessionLocal() as session:
                data["session"] = session
                return await handler(event, data)

    dp.update.middleware(SessionMiddleware())
    dp.message.middleware(WhitelistMiddleware())

    # Роутеры
    dp.include_router(admin.router)
    dp.include_router(group.router)
    dp.include_router(private.router)

    # Планировщик
    scheduler = create_scheduler(bot)
    scheduler.start()
    logger.info("Планировщик запущен")

    # API сервер для Mini App
    await start_api_server()

    logger.info("Бот запускается...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
