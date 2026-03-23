from __future__ import annotations

"""Функции для получения статистики голов/ассистов и турнирных результатов."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Match, MatchStat, Player, Tournament


async def get_top_scorers(
    session: AsyncSession,
    limit: int = 10,
    tournament_id: int | None = None,
) -> list[dict]:
    """Лучшие бомбардиры за всё время или конкретный турнир."""
    query = (
        select(
            MatchStat.user_id,
            MatchStat.player_id,
            Player.name,
            func.sum(MatchStat.goals).label("total_goals"),
            func.sum(MatchStat.assists).label("total_assists"),
            func.sum(MatchStat.appearances).label("appearances"),
        )
        .join(Player, MatchStat.player_id == Player.id)
        .group_by(MatchStat.user_id, MatchStat.player_id, Player.name)
        .order_by(func.sum(MatchStat.goals).desc())
        .limit(limit)
    )
    if tournament_id is not None:
        query = query.join(Match, MatchStat.match_id == Match.id).where(
            Match.tournament_id == tournament_id
        )
    result = await session.execute(query)
    return [
        {
            "user_id": row.user_id,
            "player_id": row.player_id,
            "player_name": row.name,
            "goals": row.total_goals,
            "assists": row.total_assists,
            "appearances": row.appearances,
        }
        for row in result.all()
    ]


async def get_top_assisters(
    session: AsyncSession,
    limit: int = 10,
    tournament_id: int | None = None,
) -> list[dict]:
    """Лучшие ассистенты."""
    query = (
        select(
            MatchStat.user_id,
            MatchStat.player_id,
            Player.name,
            func.sum(MatchStat.goals).label("total_goals"),
            func.sum(MatchStat.assists).label("total_assists"),
            func.sum(MatchStat.appearances).label("appearances"),
        )
        .join(Player, MatchStat.player_id == Player.id)
        .group_by(MatchStat.user_id, MatchStat.player_id, Player.name)
        .order_by(func.sum(MatchStat.assists).desc())
        .limit(limit)
    )
    if tournament_id is not None:
        query = query.join(Match, MatchStat.match_id == Match.id).where(
            Match.tournament_id == tournament_id
        )
    result = await session.execute(query)
    return [
        {
            "user_id": row.user_id,
            "player_id": row.player_id,
            "player_name": row.name,
            "goals": row.total_goals,
            "assists": row.total_assists,
            "appearances": row.appearances,
        }
        for row in result.all()
    ]


async def get_top_combined(
    session: AsyncSession,
    limit: int = 5,
    tournament_id: int | None = None,
) -> list[dict]:
    """Топ по голам+ассистам."""
    query = (
        select(
            MatchStat.user_id,
            MatchStat.player_id,
            Player.name,
            func.sum(MatchStat.goals).label("total_goals"),
            func.sum(MatchStat.assists).label("total_assists"),
            func.sum(MatchStat.appearances).label("appearances"),
        )
        .join(Player, MatchStat.player_id == Player.id)
        .group_by(MatchStat.user_id, MatchStat.player_id, Player.name)
        .order_by((func.sum(MatchStat.goals) + func.sum(MatchStat.assists)).desc())
        .limit(limit)
    )
    if tournament_id is not None:
        query = query.join(Match, MatchStat.match_id == Match.id).where(
            Match.tournament_id == tournament_id
        )
    result = await session.execute(query)
    return [
        {
            "user_id": row.user_id,
            "player_id": row.player_id,
            "player_name": row.name,
            "goals": row.total_goals,
            "assists": row.total_assists,
            "appearances": row.appearances,
        }
        for row in result.all()
    ]


async def get_user_stats(
    session: AsyncSession,
    user_id: int,
    limit: int = 10,
) -> list[dict]:
    """Личная статистика пользователя — его игроки с голами/ассистами."""
    query = (
        select(
            MatchStat.player_id,
            Player.name,
            func.sum(MatchStat.goals).label("total_goals"),
            func.sum(MatchStat.assists).label("total_assists"),
            func.sum(MatchStat.appearances).label("appearances"),
        )
        .join(Player, MatchStat.player_id == Player.id)
        .where(MatchStat.user_id == user_id)
        .group_by(MatchStat.player_id, Player.name)
        .order_by(
            (func.sum(MatchStat.goals) + func.sum(MatchStat.assists)).desc()
        )
        .limit(limit)
    )
    result = await session.execute(query)
    return [
        {
            "player_name": row.name,
            "goals": row.total_goals,
            "assists": row.total_assists,
            "appearances": row.appearances,
        }
        for row in result.all()
    ]


async def get_tournament_record(
    session: AsyncSession,
    user_id: int,
) -> dict:
    """Итоговый рекорд игрока: побед/ничьих/поражений/участий."""
    result = await session.execute(
        select(Match).where(
            (Match.home_user_id == user_id) | (Match.away_user_id == user_id),
            Match.home_goals.isnot(None),
        )
    )
    matches = result.scalars().all()
    w = d = l = 0
    for m in matches:
        is_home = m.home_user_id == user_id
        gf = m.home_goals if is_home else m.away_goals
        ga = m.away_goals if is_home else m.home_goals
        if gf > ga:
            w += 1
        elif gf == ga:
            d += 1
        else:
            l += 1
    return {"wins": w, "draws": d, "losses": l, "played": w + d + l}


async def get_top_mvp(
    session: AsyncSession,
    limit: int = 5,
    tournament_id: int | None = None,
) -> list[dict]:
    """Топ по количеству MVP-наград."""
    query = (
        select(
            MatchStat.user_id,
            MatchStat.player_id,
            Player.name,
            func.sum(MatchStat.mvp_count).label("total_mvp"),
            func.sum(MatchStat.appearances).label("appearances"),
        )
        .join(Player, MatchStat.player_id == Player.id)
        .group_by(MatchStat.user_id, MatchStat.player_id, Player.name)
        .having(func.sum(MatchStat.mvp_count) > 0)
        .order_by(func.sum(MatchStat.mvp_count).desc())
        .limit(limit)
    )
    if tournament_id is not None:
        query = query.join(Match, MatchStat.match_id == Match.id).where(
            Match.tournament_id == tournament_id
        )
    result = await session.execute(query)
    return [
        {
            "user_id": row.user_id,
            "player_id": row.player_id,
            "player_name": row.name,
            "mvp_count": row.total_mvp,
            "appearances": row.appearances,
        }
        for row in result.all()
    ]


def format_scorers(rows: list[dict], title: str) -> str:
    lines = [f"*{title}*\n"]
    for i, row in enumerate(rows, 1):
        apps = row.get('appearances', 0)
        lines.append(
            f"{i}. {row['player_name']} — ⚽{row['goals']} 🎯{row['assists']} ({apps} матч.)"
        )
    return "\n".join(lines) if rows else f"{title}\nПока нет данных."
