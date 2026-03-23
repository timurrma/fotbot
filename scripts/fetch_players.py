"""
Скрипт для одноразового сбора игроков через API-Football (RapidAPI).
Запуск: python scripts/fetch_players.py

Требуется .env с RAPIDAPI_KEY и DATABASE_URL.
Бесплатный тир: 100 запросов/день — хватает для первичного заполнения.
"""

import asyncio
import csv
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

from bot.db.models import Base, Player

RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./football_bot.db")

API_BASE = "https://api-football-v1.p.rapidapi.com/v3"
HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com",
}
SEASON = 2024

# Лиги: (league_id, div_name, is_national=False)
LEAGUES = [
    (39, "АПЛ 1"),
    (40, "АПЛ 2"),
    (140, "Ла Лига 1"),
    (141, "Ла Лига 2"),
    (135, "Серия А 1"),
    (136, "Серия А 2"),
    (78, "Бундеслига 1"),
    (79, "Бундеслига 2"),
    (61, "Лига 1"),
    (62, "Лига 2"),
    (203, "Турция"),
    (94, "Португалия"),
    (307, "Саудовская Аравия"),
    (253, "MLS"),
]

# Топ-10 сборных (team_id в API-Football)
NATIONAL_TEAMS = [
    (2, "Франция"),
    (6, "Бразилия"),
    (26, "Аргентина"),
    (1, "Бельгия"),
    (10, "Англия"),
    (27, "Португалия"),
    (9, "Испания"),
    (1118, "Нидерланды"),
    (768, "Италия"),
    (3, "Хорватия"),
]

MIN_RATING = 65.0


def api_rating_to_overall(raw_rating: float | None) -> int | None:
    """Конвертирует рейтинг API (0–10) в шкалу 65–99."""
    if raw_rating is None:
        return None
    # Нормируем: 6.5 → 65, 10.0 → 99
    scaled = (raw_rating - 5.0) / (10.0 - 5.0) * (99 - 65) + 65
    return max(65, min(99, round(scaled)))


def extract_position(stats: list[dict]) -> str:
    """Определяет основную позицию из статистики."""
    pos_map = {
        "Goalkeeper": "GK",
        "Defender": "CB",
        "Midfielder": "CM",
        "Attacker": "ST",
    }
    if stats and stats[0].get("games", {}).get("position"):
        raw = stats[0]["games"]["position"]
        return pos_map.get(raw, raw[:2].upper())
    return "CM"


async def fetch_players_for_league(
    client: httpx.AsyncClient,
    league_id: int,
    is_national: bool = False,
) -> list[dict]:
    """Загружает всех игроков лиги постранично."""
    players = []
    page = 1
    while True:
        params = {"season": SEASON, "page": page}
        if is_national:
            params["team"] = league_id
        else:
            params["league"] = league_id

        resp = await client.get(f"{API_BASE}/players", params=params, headers=HEADERS)
        if resp.status_code == 429:
            print("Rate limit — ждём 60 сек...")
            await asyncio.sleep(60)
            continue
        resp.raise_for_status()
        data = resp.json()
        results = data.get("response", [])
        players.extend(results)
        total_pages = data.get("paging", {}).get("total", 1)
        print(f"  страница {page}/{total_pages}, игроков: {len(results)}")
        if page >= total_pages:
            break
        page += 1
        await asyncio.sleep(1.2)  # ~50 req/min — не превышаем лимит
    return players


def parse_player(item: dict, league_id: int, is_national: bool) -> Player | None:
    """Парсит объект игрока из API в модель Player."""
    info = item.get("player", {})
    stats = item.get("statistics", [])

    if not stats:
        return None

    raw_rating = stats[0].get("games", {}).get("rating")
    if raw_rating:
        try:
            raw_rating = float(raw_rating)
        except (ValueError, TypeError):
            raw_rating = None

    overall = api_rating_to_overall(raw_rating)
    if overall is None or overall < MIN_RATING:
        return None

    position = extract_position(stats)
    club = stats[0].get("team", {}).get("name") if stats else None

    player = Player(
        id=info["id"],
        name=info.get("name") or info.get("firstname", "") + " " + info.get("lastname", ""),
        club=club,
        nationality=info.get("nationality"),
        position=position,
        positions_json=json.dumps([position]),
        overall_rating=overall,
        photo_url=info.get("photo"),
        league_id=league_id if not is_national else None,
        is_national_team=is_national,
    )
    return player


async def load_russia_csv(session: AsyncSession) -> int:
    """Загружает сборную России из CSV-файла data/russia_players.csv."""
    csv_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "russia_players.csv")
    if not os.path.exists(csv_path):
        print("data/russia_players.csv не найден — пропускаем сборную России")
        return 0

    count = 0
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=1):
            rating = int(row["overall_rating"])
            if rating < MIN_RATING:
                continue
            player = Player(
                id=2000000 + i,  # synthetic ID
                name=row["name"],
                club=row.get("club", ""),
                nationality="Russia",
                position=row.get("position", "CM"),
                positions_json=json.dumps([row.get("position", "CM")]),
                overall_rating=rating,
                photo_url=row.get("photo_url"),
                league_id=None,
                is_national_team=True,
            )
            await session.merge(player)
            count += 1
    await session.commit()
    return count


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    total = 0

    async with httpx.AsyncClient(timeout=30) as client:
        async with AsyncSessionLocal() as session:
            # Лиги
            for league_id, name in LEAGUES:
                print(f"\n=== {name} (league {league_id}) ===")
                try:
                    items = await fetch_players_for_league(client, league_id)
                    saved = 0
                    for item in items:
                        player = parse_player(item, league_id, is_national=False)
                        if player:
                            await session.merge(player)
                            saved += 1
                    await session.commit()
                    print(f"  Сохранено: {saved}")
                    total += saved
                except Exception as e:
                    print(f"  Ошибка: {e}")
                await asyncio.sleep(2)

            # Сборные
            for team_id, name in NATIONAL_TEAMS:
                print(f"\n=== Сборная {name} (team {team_id}) ===")
                try:
                    items = await fetch_players_for_league(client, team_id, is_national=True)
                    saved = 0
                    for item in items:
                        player = parse_player(item, team_id, is_national=True)
                        if player:
                            await session.merge(player)
                            saved += 1
                    await session.commit()
                    print(f"  Сохранено: {saved}")
                    total += saved
                except Exception as e:
                    print(f"  Ошибка: {e}")
                await asyncio.sleep(2)

            # Россия CSV
            print("\n=== Сборная России (CSV) ===")
            russia_count = await load_russia_csv(session)
            print(f"  Сохранено: {russia_count}")
            total += russia_count

    print(f"\n✅ Всего игроков загружено: {total}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
