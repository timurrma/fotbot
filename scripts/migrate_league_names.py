"""
Миграция league_name из FC26_20250921.csv в таблицу players.
Запуск: python scripts/migrate_league_names.py
"""
import asyncio
import csv
import os

import asyncpg

DATABASE_URL = os.environ.get(
    "DATABASE_URL_SYNC",
    "postgresql://postgres:xDzWGvMEdLELWzecMPnzxpbSJNptUhZE@caboose.proxy.rlwy.net:44501/railway",
)

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "FC26_20250921.csv")


async def main() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        updated = 0
        skipped = 0
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = [(int(row["player_id"]), row["league_name"]) for row in reader if row.get("league_name")]

        for player_id, league_name in rows:
            result = await conn.execute(
                "UPDATE players SET league_name = $1 WHERE id = $2 AND league_name IS NULL",
                league_name, player_id,
            )
            if result == "UPDATE 1":
                updated += 1
            else:
                skipped += 1

        print(f"Обновлено из CSV: {updated}, пропущено (уже есть или нет в БД): {skipped}")

        # Российские кастомные игроки (id >= 2000000, кроме легенд)
        result = await conn.execute(
            """
            UPDATE players
            SET league_name = 'Российская Премьер-лига'
            WHERE id >= 2000000
              AND id NOT IN (2000032, 2000033)
              AND league_name IS NULL
            """
        )
        print(f"Российские игроки: {result}")

        # Легенды
        await conn.execute(
            "UPDATE players SET league_name = 'Легенды' WHERE id IN (2000032, 2000033)"
        )
        print("Легенды (Аршавин, Туран) — league_name = 'Легенды'")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
