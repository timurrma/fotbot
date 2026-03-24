"""
Планировщик еженедельных задач.

Расписание (UTC+3, Moscow):
- Среда 20:00 — анонс турнира в чат
- Четверг 09:00 — авто-анонс результатов (если матчи не сыграны вручную)
- Четверг 10:00 — раздача паков
- Каждый день 09:00 — утренний пак (2 игрока)
"""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import settings


def create_scheduler(bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    # Среда 20:00 — анонс турнира
    scheduler.add_job(
        _announce_tournament,
        CronTrigger(day_of_week="wed", hour=20, minute=0),
        args=[bot],
        id="announce_tournament",
        replace_existing=True,
    )

    # Четверг 09:00 — авто-итоги если матчи не сыграны
    scheduler.add_job(
        _auto_results,
        CronTrigger(day_of_week="thu", hour=9, minute=0),
        args=[bot],
        id="auto_results",
        replace_existing=True,
    )

    # Четверг 10:00 — раздача паков
    scheduler.add_job(
        _weekly_packs,
        CronTrigger(day_of_week="thu", hour=10, minute=0),
        args=[bot],
        id="weekly_packs",
        replace_existing=True,
    )

    # Каждый день 09:00 — утренний пак
    scheduler.add_job(
        _morning_packs,
        CronTrigger(hour=9, minute=0),
        args=[bot],
        id="morning_packs",
        replace_existing=True,
    )

    return scheduler


async def _announce_tournament(bot) -> None:
    from bot.db.session import AsyncSessionLocal
    from bot.db.models import Whitelist
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Whitelist))
        players = result.scalars().all()

    names = ", ".join(f"ID{p.user_id}" for p in players)
    await bot.send_message(
        settings.group_id,
        f"⚽ Сегодня турнир!\n\n"
        f"Участники: {names}\n\n"
        f"Настрой состав в личных сообщениях с ботом командой /squad\n"
        f"Запускай матчи командой /nextmatch в чате!"
    )


async def _auto_results(bot) -> None:
    """Публикует краткие итоги несыгранных матчей без LLM."""
    from bot.services.tournament import auto_announce_results
    await auto_announce_results(bot)


async def _morning_packs(bot) -> None:
    """Выдаёт утренний пак всем игрокам из whitelist."""
    from bot.db.session import AsyncSessionLocal
    from bot.db.models import Whitelist
    from bot.services.packs import give_pending_pack
    from sqlalchemy import select
    import asyncio

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Whitelist))
        players = result.scalars().all()

    await bot.send_message(
        settings.group_id,
        "🌅 Доброе утро! Каждый получил утренний пак — открой командой /openpack в личных сообщениях.",
    )

    for player in players:
        try:
            async with AsyncSessionLocal() as session:
                await give_pending_pack(session, player.user_id, pack_type="morning")
            try:
                await bot.send_message(
                    player.user_id,
                    "🌅 Утренний пак!\n\nОткрой его командой /openpack",
                )
            except Exception:
                pass
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Ошибка выдачи утреннего пака для {player.user_id}: {e}")


async def _weekly_packs(bot) -> None:
    """Выдаёт неоткрытые паки всем игрокам из whitelist."""
    from bot.db.session import AsyncSessionLocal
    from bot.db.models import Whitelist
    from bot.services.packs import give_pending_pack
    from sqlalchemy import select
    import asyncio

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Whitelist))
        players = result.scalars().all()

    await bot.send_message(
        settings.group_id,
        "🎴 Четверг — день паков! Каждый получил пак — открой его командой /openpack в личных сообщениях.",
    )

    for player in players:
        try:
            async with AsyncSessionLocal() as session:
                await give_pending_pack(session, player.user_id, pack_type="weekly")
            try:
                await bot.send_message(
                    player.user_id,
                    "🎴 Тебе выдан еженедельный пак!\n\nОткрой его командой /openpack",
                )
            except Exception:
                pass
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Ошибка выдачи пака для {player.user_id}: {e}")
