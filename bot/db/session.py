from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings
from bot.db.models import Base

engine = create_async_engine(settings.async_database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _run_migrations()
    await _seed_players_if_empty()


async def _run_migrations() -> None:
    """Применяет накопленные миграции для уже существующих таблиц."""
    from sqlalchemy import text, inspect
    async with engine.connect() as conn:
        # Добавляем tournament_type если не существует
        def _check_and_add(sync_conn):
            insp = inspect(sync_conn)
            cols = [c["name"] for c in insp.get_columns("tournaments")]
            if "tournament_type" not in cols:
                sync_conn.execute(
                    text("ALTER TABLE tournaments ADD COLUMN tournament_type VARCHAR(20) NOT NULL DEFAULT 'regular'")
                )
        await conn.run_sync(_check_and_add)
        await conn.commit()


async def _seed_players_if_empty() -> None:
    """Импортирует игроков из CSV если таблица players пустая."""
    from sqlalchemy import text
    async with AsyncSessionLocal() as session:
        result = await session.execute(text("SELECT COUNT(*) FROM players"))
        count = result.scalar()
    if count == 0:
        import logging
        logger = logging.getLogger(__name__)
        logger.info("Таблица players пустая — запускаю импорт из CSV...")
        try:
            from scripts.import_fc26 import import_csv, import_russia_csv
            async with AsyncSessionLocal() as session:
                counts = await import_csv(session)
            async with AsyncSessionLocal() as session:
                russia = await import_russia_csv(session)
            total = counts["leagues"] + russia
            logger.info(f"Импорт завершён: {total} игроков")
        except Exception as e:
            logger.error(f"Ошибка импорта игроков: {e}")


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
