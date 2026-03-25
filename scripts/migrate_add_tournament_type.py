"""
Миграция: добавляет колонку tournament_type в таблицу tournaments.
Запустить один раз: python -m scripts.migrate_add_tournament_type
"""
import asyncio
from sqlalchemy import text
from bot.db.session import engine


async def migrate():
    async with engine.begin() as conn:
        # SQLite: проверяем колонки через PRAGMA
        result = await conn.execute(text("PRAGMA table_info(tournaments)"))
        cols = [row[1] for row in result.fetchall()]
        if "tournament_type" in cols:
            print("Колонка tournament_type уже существует.")
            return
        await conn.execute(text(
            "ALTER TABLE tournaments ADD COLUMN tournament_type VARCHAR(20) NOT NULL DEFAULT 'regular'"
        ))
        print("✅ Колонка tournament_type добавлена.")


if __name__ == "__main__":
    asyncio.run(migrate())
