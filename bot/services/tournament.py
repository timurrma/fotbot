from __future__ import annotations

"""
Управление турниром: создание, запуск, round-robin, сохранение результатов.

Режимы:
- Ручной: /nextmatch в чате — запускает следующий несыгранный матч с LLM-комментарием
- Авто: если за день никто не вызвал матч — публикует только результат (без симуляции текста)
"""
import asyncio
from datetime import datetime, timezone
from itertools import combinations

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Match, MatchStat, Tournament, UserCard, UserSquad, Whitelist
from bot.db.session import AsyncSessionLocal
from bot.services.llm_commentator import commentate_match, format_match_summary
from bot.services.simulation import events_to_dict, simulate_match


async def _get_squad_cards(
    session: AsyncSession,
    user_id: int,
) -> tuple[str, list[tuple[int, object]]]:
    """
    Возвращает (formation, [(user_card_id, Player), ...]) для игрока.
    Если состав не настроен — берёт топ-11 по рейтингу, схема 4-4-2.
    """
    from sqlalchemy.orm import joinedload
    from bot.db.models import Player

    squad_row = await session.get(UserSquad, user_id)

    if squad_row and squad_row.slot_assignments:
        formation = squad_row.formation
        assignments = squad_row.slot_assignments
        cards = []
        for _, card_id in sorted(assignments.items()):
            card = await session.get(UserCard, card_id, options=[joinedload(UserCard.player)])
            if card:
                cards.append((card_id, card.player))
        if len(cards) >= 11:
            return formation, cards[:11]

    # Фоллбэк: топ-11 по рейтингу
    from sqlalchemy import desc
    result = await session.execute(
        select(UserCard)
        .where(UserCard.user_id == user_id)
        .join(UserCard.player)
        .order_by(desc("overall_rating"))
        .limit(11)
        .options(joinedload(UserCard.player))
    )
    cards_raw = result.scalars().all()
    return "4-4-2", [(c.id, c.player) for c in cards_raw]


async def get_or_create_tournament(session: AsyncSession) -> Tournament:
    now = datetime.now(timezone.utc)
    week = now.isocalendar()[1]
    year = now.year

    result = await session.execute(
        select(Tournament).where(
            Tournament.week_number == week,
            Tournament.year == year,
        )
    )
    t = result.scalar_one_or_none()
    if not t:
        t = Tournament(week_number=week, year=year, status="pending")
        session.add(t)
        await session.commit()
        await session.refresh(t)
    return t


async def get_active_tournament(session: AsyncSession) -> Tournament | None:
    """Возвращает турнир со статусом running, или None если такого нет."""
    result = await session.execute(
        select(Tournament).where(Tournament.status == "running").limit(1)
    )
    return result.scalar_one_or_none()


async def get_next_unplayed_match(
    session: AsyncSession,
    tournament: Tournament,
) -> Match | None:
    """Возвращает следующий несыгранный матч турнира."""
    result = await session.execute(
        select(Match).where(
            Match.tournament_id == tournament.id,
            Match.home_goals.is_(None),
        ).limit(1)
    )
    return result.scalar_one_or_none()


async def ensure_matches_created(
    session: AsyncSession,
    tournament: Tournament,
) -> None:
    """Создаёт все матчи round-robin если ещё не созданы."""
    result = await session.execute(
        select(Match).where(Match.tournament_id == tournament.id)
    )
    existing = result.scalars().all()
    if existing:
        return

    wl_result = await session.execute(select(Whitelist))
    players = wl_result.scalars().all()
    user_ids = [p.user_id for p in players]

    for home_id, away_id in combinations(user_ids, 2):
        match = Match(
            tournament_id=tournament.id,
            home_user_id=home_id,
            away_user_id=away_id,
        )
        session.add(match)
    await session.commit()


def _format_lineups(
    home_name: str,
    home_formation: str,
    home_cards: list,
    away_name: str,
    away_formation: str,
    away_cards: list,
) -> str:
    """Форматирует составы двух команд перед матчем."""
    def lineup_lines(name: str, formation: str, cards: list) -> list[str]:
        lines = [f"<b>{name}</b> ({formation})"]
        for _, player in cards[:11]:
            r = player.overall_rating
            icon = "👑" if r >= 90 else "🌟" if r >= 85 else "⭐"
            lines.append(f"  {player.position} {player.name} {r}{icon}")
        return lines

    home_lines = lineup_lines(home_name, home_formation, home_cards)
    away_lines = lineup_lines(away_name, away_formation, away_cards)

    return (
        "📋 <b>Составы</b>\n\n"
        + "\n".join(home_lines)
        + "\n\n"
        + "\n".join(away_lines)
    )


async def play_next_match(bot: Bot, with_commentary: bool = True) -> bool:
    """
    Играет следующий несыгранный матч турнира.
    with_commentary=True — публикует LLM-комментарий (для ручного запуска /nextmatch)
    with_commentary=False — только краткий итог (для авто-анонса)
    Возвращает True если матч был сыгран, False если матчей больше нет.
    """
    async with AsyncSessionLocal() as session:
        tournament = await get_active_tournament(session)
        if not tournament:
            return False

        await ensure_matches_created(session, tournament)

        match = await get_next_unplayed_match(session, tournament)
        if not match:
            return False

        home_formation, home_cards = await _get_squad_cards(session, match.home_user_id)
        away_formation, away_cards = await _get_squad_cards(session, match.away_user_id)

        if not home_cards or not away_cards:
            return False

        # Получаем имена заранее для анонса
        try:
            home_chat = await bot.get_chat(match.home_user_id)
            away_chat = await bot.get_chat(match.away_user_id)
            _home_name = home_chat.username or home_chat.full_name or f"ID{match.home_user_id}"
            _away_name = away_chat.username or away_chat.full_name or f"ID{match.away_user_id}"
        except Exception:
            _home_name = f"ID{match.home_user_id}"
            _away_name = f"ID{match.away_user_id}"

        await bot.send_message(
            settings.group_id,
            f"⚽ <b>Матч:</b> @{_home_name} vs @{_away_name}",
        )
        await asyncio.sleep(1)

        result = simulate_match(home_formation, home_cards, away_formation, away_cards)
        events_data = events_to_dict(result.events)

        match.home_goals = result.home_goals
        match.away_goals = result.away_goals
        match.events = events_data
        match.played_at = datetime.utcnow()

        # Статистика
        for card_id, stat in result.home_stats.items():
            s = MatchStat(
                match_id=match.id,
                user_id=match.home_user_id,
                user_card_id=card_id,
                player_id=stat["player_id"],
                goals=stat["goals"],
                assists=stat["assists"],
            )
            session.add(s)
        for card_id, stat in result.away_stats.items():
            s = MatchStat(
                match_id=match.id,
                user_id=match.away_user_id,
                user_card_id=card_id,
                player_id=stat["player_id"],
                goals=stat["goals"],
                assists=stat["assists"],
            )
            session.add(s)

        await session.commit()

        # Если больше нет несыгранных матчей — помечаем турнир завершённым
        remaining = await get_next_unplayed_match(session, tournament)
        if not remaining:
            tournament.status = "finished"
            await session.commit()

        home_name, away_name = _home_name, _away_name

        # Составы перед матчем
        lineup_text = _format_lineups(
            home_name, home_formation, home_cards,
            away_name, away_formation, away_cards,
        )

        if with_commentary:
            try:
                messages = await commentate_match(
                    home_name, away_name,
                    home_formation, away_formation,
                    result, events_data,
                )
            except Exception:
                messages = [format_match_summary(home_name, away_name, result, events_data)]

            # Вставляем составы первым сообщением
            all_messages = [lineup_text] + messages
            for msg in all_messages:
                try:
                    await bot.send_message(settings.group_id, msg)
                    await asyncio.sleep(2)
                except Exception:
                    pass
        else:
            await bot.send_message(settings.group_id, lineup_text)
            await asyncio.sleep(1)
            summary = format_match_summary(home_name, away_name, result, events_data)
            await bot.send_message(settings.group_id, summary)

        return True


async def auto_announce_results(bot: Bot) -> None:
    """
    Авто-анонс: публикует краткие результаты всех несыгранных матчей без LLM.
    Вызывается по расписанию если за день никто не вызвал /nextmatch.
    """
    async with AsyncSessionLocal() as session:
        tournament = await get_active_tournament(session)
        if not tournament:
            return
        await ensure_matches_created(session, tournament)

        result = await session.execute(
            select(Match).where(
                Match.tournament_id == tournament.id,
                Match.home_goals.is_(None),
            )
        )
        unplayed = result.scalars().all()

        if not unplayed:
            return

        await bot.send_message(
            settings.group_id,
            "📊 Авто-итоги матчей этой недели:"
        )
        await asyncio.sleep(1)

        for match in unplayed:
            home_formation, home_cards = await _get_squad_cards(session, match.home_user_id)
            away_formation, away_cards = await _get_squad_cards(session, match.away_user_id)

            if not home_cards or not away_cards:
                continue

            sim_result = simulate_match(home_formation, home_cards, away_formation, away_cards)
            events_data = events_to_dict(sim_result.events)

            match.home_goals = sim_result.home_goals
            match.away_goals = sim_result.away_goals
            match.events = events_data
            match.played_at = datetime.utcnow()

            for card_id, stat in sim_result.home_stats.items():
                s = MatchStat(
                    match_id=match.id,
                    user_id=match.home_user_id,
                    user_card_id=card_id,
                    player_id=stat["player_id"],
                    goals=stat["goals"],
                    assists=stat["assists"],
                )
                session.add(s)
            for card_id, stat in sim_result.away_stats.items():
                s = MatchStat(
                    match_id=match.id,
                    user_id=match.away_user_id,
                    user_card_id=card_id,
                    player_id=stat["player_id"],
                    goals=stat["goals"],
                    assists=stat["assists"],
                )
                session.add(s)

            await session.commit()

            try:
                home_chat = await bot.get_chat(match.home_user_id)
                away_chat = await bot.get_chat(match.away_user_id)
                home_name = home_chat.username or home_chat.full_name or f"ID{match.home_user_id}"
                away_name = away_chat.username or away_chat.full_name or f"ID{match.away_user_id}"
            except Exception:
                home_name, away_name = f"ID{match.home_user_id}", f"ID{match.away_user_id}"

            summary = format_match_summary(home_name, away_name, sim_result, events_data)
            await bot.send_message(settings.group_id, summary)
            await asyncio.sleep(1.5)

        # Все матчи сыграны — помечаем турнир завершённым
        tournament.status = "finished"
        await session.commit()


async def build_standings_text(session: AsyncSession, tournament_id: int | None = None) -> str:
    """Строит текст турнирной таблицы."""
    # Собираем всех участников (whitelist) для базовой строки 0 очков
    wl_result = await session.execute(select(Whitelist))
    all_user_ids = [w.user_id for w in wl_result.scalars().all()]

    if tournament_id:
        result = await session.execute(
            select(Match).where(
                Match.tournament_id == tournament_id,
                Match.home_goals.isnot(None),
            )
        )
    else:
        result = await session.execute(
            select(Match).where(Match.home_goals.isnot(None))
        )

    matches = result.scalars().all()
    stats: dict[int, dict] = {uid: {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0} for uid in all_user_ids}

    for m in matches:
        for uid, gf, ga in [
            (m.home_user_id, m.home_goals, m.away_goals),
            (m.away_user_id, m.away_goals, m.home_goals),
        ]:
            s = stats.setdefault(uid, {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0})
            s["gf"] += gf
            s["ga"] += ga
            if gf > ga:
                s["w"] += 1
            elif gf == ga:
                s["d"] += 1
            else:
                s["l"] += 1

    if not stats:
        return "📊 Турнирная таблица пуста."

    rows = sorted(
        stats.items(),
        key=lambda x: (x[1]["w"] * 3 + x[1]["d"], x[1]["gf"] - x[1]["ga"]),
        reverse=True,
    )

    title = "📊 <b>Турнирная таблица</b>\n" if tournament_id else "📊 <b>Таблица за всё время</b>\n"
    lines = [title]
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, s) in enumerate(rows):
        pts = s["w"] * 3 + s["d"]
        played = s["w"] + s["d"] + s["l"]
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(
            f"{medal} ID{uid}: {s['w']}В {s['d']}Н {s['l']}П ({played} игр) | "
            f"{s['gf']}:{s['ga']} | {pts} очк."
        )

    return "\n".join(lines)
