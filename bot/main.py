"""Точка входа бота."""
import asyncio
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject
from aiohttp import web

from bot.api import create_api_app
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

    dp.include_router(admin.router)
    dp.include_router(group.router)
    dp.include_router(private.router)

    # Webhook — нет конфликтов при редеплое
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    webhook_url = f"{settings.miniapp_url}/webhook"

    app = create_api_app()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.api_port)
    await site.start()
    logger.info(f"Сервер запущен на 0.0.0.0:{settings.api_port}")

    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    logger.info(f"Webhook установлен: {webhook_url}")

    scheduler = create_scheduler(bot)
    scheduler.start()
    logger.info("Планировщик запущен")

    try:
        await asyncio.Event().wait()
    finally:
        await bot.delete_webhook()
        scheduler.shutdown()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
