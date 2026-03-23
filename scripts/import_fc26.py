"""
Импорт игроков из CSV-датасета FC 26 (Kaggle).
Запуск: python scripts/import_fc26.py

Загружает игроков из нужных лиг + сборных + Россия из CSV.
Не требует API-ключей.
"""
import asyncio
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.db.models import Base, Player

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./football_bot.db")
CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "FC26_20250921.csv")

MIN_RATING = 65

# Лиги без ограничений по клубам (фильтр по league_id — уникален в датасете)
TARGET_LEAGUE_IDS = {
    13,    # Premier League (England)
    14,    # Championship (England)
    53,    # La Liga (Spain)
    31,    # Serie A (Italy)
    19,    # Bundesliga (Germany)
    16,    # Ligue 1 (France)
}

# Лиги с фильтром только по определённым клубам
TARGET_CLUBS = {
    # MLS
    "LA Galaxy", "Inter Miami CF",
    # Саудовская Про Лига
    "Al Nassr", "Al Hilal", "Al Ittihad",
    # Португалия
    "FC Porto", "Sporting CP", "SL Benfica", "SC Braga",
    # Турция
    "Galatasaray", "Beşiktaş", "Fenerbahçe",
}

# Saudi Pro League называется по-разному в разных версиях FIFA датасетов
SAUDI_KEYWORDS = {"saudi", "roshn", "pro league"}

# Лиги, в которых фильтруем по клубам (MLS, Saudi, Portugal, Turkey)
FILTERED_LEAGUES = {"Major League Soccer", "Süper Lig", "Primeira Liga"}

# Маппинг позиций FIFA → наши позиции
POSITION_MAP = {
    "GK": "GK",
    "CB": "CB", "LCB": "CB", "RCB": "CB",
    "LB": "LB", "LWB": "LB",
    "RB": "RB", "RWB": "RB",
    "CDM": "CDM", "LDM": "CDM", "RDM": "CDM",
    "CM": "CM", "LCM": "CM", "RCM": "CM",
    "LM": "LM",
    "RM": "RM",
    "CAM": "CAM", "LAM": "CAM", "RAM": "CAM",
    "LW": "LW", "LF": "LW",
    "RW": "RW", "RF": "RW",
    "CF": "CF",
    "ST": "ST", "LS": "ST", "RS": "ST",
}


def parse_positions(raw: str) -> tuple[str, list[str]]:
    """Парсит строку позиций 'CAM, CM' → основная и список."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    mapped = [POSITION_MAP.get(p, "CM") for p in parts]
    primary = mapped[0] if mapped else "CM"
    unique = list(dict.fromkeys(mapped))  # порядок сохранён, дублей нет
    return primary, unique


def is_saudi(league_name: str) -> bool:
    ln = league_name.lower()
    return any(kw in ln for kw in SAUDI_KEYWORDS)


async def import_csv(session: AsyncSession) -> dict[str, int]:
    counts = {"leagues": 0, "skipped": 0}

    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            overall = int(row["overall"] or 0)
            if overall < MIN_RATING:
                counts["skipped"] += 1
                continue

            player_id = int(row["player_id"])
            league_name = row["league_name"]
            nationality = row["nationality_name"]

            primary_pos, all_positions = parse_positions(row["player_positions"])

            player = Player(
                id=player_id,
                name=row["short_name"],
                club=row["club_name"],
                nationality=nationality,
                position=primary_pos,
                positions_json=json.dumps(all_positions),
                overall_rating=overall,
                photo_url=row.get("player_face_url") or None,
                league_id=None,
                is_national_team=False,
            )

            # Определяем попадает ли игрок в выборку
            club_name = row["club_name"]
            league_id_int = int(row["league_id"]) if row["league_id"] else 0
            in_top_league = league_id_int in TARGET_LEAGUE_IDS
            in_filtered = (
                league_name in FILTERED_LEAGUES or is_saudi(league_name)
            ) and club_name in TARGET_CLUBS

            if in_top_league or in_filtered:
                await session.merge(player)
                counts["leagues"] += 1
            else:
                counts["skipped"] += 1

    await session.commit()
    return counts


async def import_russia_csv(session: AsyncSession) -> int:
    """Загружает сборную России из data/russia_players.csv.

    Если игрок уже есть в БД (по имени, из FC26 датасета) — пропускаем,
    приоритет у FC26 карточки.
    """
    from sqlalchemy import select as sa_select
    russia_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "russia_players.csv")
    if not os.path.exists(russia_path):
        print("  data/russia_players.csv не найден — пропускаем")
        return 0

    # Собираем имена уже импортированных игроков
    existing = await session.execute(sa_select(Player.name))
    existing_names = {row[0] for row in existing}

    count = 0
    skipped = 0
    with open(russia_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=1):
            rating = int(row["overall_rating"])
            if rating < MIN_RATING:
                continue
            name = row["name"]
            if name in existing_names:
                print(f"  Пропускаем {name} — уже есть из FC26")
                skipped += 1
                continue
            pos = row.get("position", "CM")
            player = Player(
                id=2_000_000 + i,
                name=name,
                club=row.get("club", ""),
                nationality="Russia",
                position=pos,
                positions_json=json.dumps([pos]),
                overall_rating=rating,
                photo_url=row.get("photo_url") or None,
                league_id=None,
                is_national_team=True,
            )
            await session.merge(player)
            existing_names.add(name)
            count += 1

    await session.commit()
    if skipped:
        print(f"  Пропущено дублей из FC26: {skipped}")
    return count


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    print(f"Читаем {CSV_PATH}...")
    async with AsyncSessionLocal() as session:
        counts = await import_csv(session)

    print(f"  Клубные игроки: {counts['leagues']}")
    print(f"  Пропущено (рейтинг <{MIN_RATING} или не та лига): {counts['skipped']}")

    print("\nЗагружаем сборную России (CSV)...")
    async with AsyncSessionLocal() as session:
        russia = await import_russia_csv(session)
    print(f"  Сборная России: {russia}")

    total = counts["leagues"] + russia
    print(f"\n✅ Итого загружено: {total} игроков")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
