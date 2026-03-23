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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import Match, MatchStat, Player, Tournament, UserCard, UserSquad, Whitelist
from bot.db.session import AsyncSessionLocal
from bot.services.llm_commentator import commentate_match, format_match_summary
from bot.services.simulation import events_to_dict, simulate_match, FORMATIONS_SLOTS


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
        SLOT_TO_POS = {
            "GK": "GK",
            "CB1": "CB", "CB2": "CB", "CB3": "CB",
            "LB": "LB", "RB": "RB",
            "CDM1": "CDM", "CDM2": "CDM",
            "CM": "CM", "CM1": "CM", "CM2": "CM", "CM3": "CM",
            "LM": "LM", "RM": "RM",
            "CAM": "CAM",
            "LW": "LW", "RW": "RW",
            "ST": "ST", "ST1": "ST", "ST2": "ST",
        }
        SLOT_ORDER = ["GK", "CB1", "CB2", "CB3", "LB", "RB",
                      "CDM1", "CDM2", "CM", "CM1", "CM2", "CM3",
                      "LM", "RM", "CAM", "LW", "RW", "ST", "ST1", "ST2"]
        ordered_slots = sorted(assignments.keys(), key=lambda s: SLOT_ORDER.index(s) if s in SLOT_ORDER else 99)
        from sqlalchemy.orm import joinedload
        from bot.db.models import Player as PlayerModel
        cards = []
        for slot_name in ordered_slots:
            card_id = assignments[slot_name]
            card = await session.get(UserCard, card_id, options=[joinedload(UserCard.player)])
            if card:
                slot_pos = SLOT_TO_POS.get(slot_name, "CM")
                cards.append((card_id, card.player, slot_pos))

        # Если меньше 11 — добиваем пустые слоты фантомным игроком рейтинг 40
        if len(cards) < 11:
            formation_slots = FORMATIONS_SLOTS.get(formation, FORMATIONS_SLOTS["4-4-2"])
            filled_count = len(cards)
            for i in range(filled_count, 11):
                slot_pos = formation_slots[i] if i < len(formation_slots) else "CM"
                phantom = PlayerModel(
                    id=-1, name="(пусто)", position=slot_pos,
                    positions_json=f'["{slot_pos}"]',
                    overall_rating=1, club=None, nationality=None,
                    photo_url=None, league_id=None, is_national_team=False,
                )
                cards.append((-1, phantom, slot_pos))

        slot_pos_list = [sp for cid, p, sp in cards[:11]]
        return formation, [(cid, p) for cid, p, sp in cards[:11]], slot_pos_list

    # Фоллбэк: топ-11 по рейтингу
    from sqlalchemy import desc
    from sqlalchemy.orm import joinedload
    result = await session.execute(
        select(UserCard)
        .where(UserCard.user_id == user_id)
        .join(UserCard.player)
        .order_by(desc("overall_rating"))
        .limit(11)
        .options(joinedload(UserCard.player))
    )
    cards_raw = result.scalars().all()
    fallback_slots = FORMATIONS_SLOTS.get("4-4-2")
    slot_pos_list = [fallback_slots[i] if i < len(fallback_slots) else "CM" for i in range(len(cards_raw))]
    return "4-4-2", [(c.id, c.player) for c in cards_raw], slot_pos_list


async def get_or_create_tournament(session: AsyncSession) -> Tournament:
    """Возвращает активный или последний турнир."""
    result = await session.execute(
        select(Tournament).order_by(Tournament.id.desc()).limit(1)
    )
    t = result.scalar_one_or_none()
    if not t:
        t = Tournament(status="pending")
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

        home_formation, home_cards, home_slot_pos = await _get_squad_cards(session, match.home_user_id)
        away_formation, away_cards, away_slot_pos = await _get_squad_cards(session, match.away_user_id)

        if not home_cards or not away_cards:
            return False

        # Получаем имена из whitelist
        home_wl = await session.get(Whitelist, match.home_user_id)
        away_wl = await session.get(Whitelist, match.away_user_id)
        _home_name = (home_wl.username if home_wl and home_wl.username else None) or f"ID{match.home_user_id}"
        _away_name = (away_wl.username if away_wl and away_wl.username else None) or f"ID{match.away_user_id}"
        wl_map = {
            match.home_user_id: _home_name,
            match.away_user_id: _away_name,
        }

        await bot.send_message(
            settings.group_id,
            f"⚽ <b>Матч:</b> @{_home_name} vs @{_away_name}",
        )
        await asyncio.sleep(1)

        result = simulate_match(home_formation, home_cards, away_formation, away_cards, home_slot_pos, away_slot_pos)
        # Маппинг card_id → owner для комментатора
        card_owner = {card_id: _home_name for card_id, _ in home_cards}
        card_owner.update({card_id: _away_name for card_id, _ in away_cards})
        events_data = events_to_dict(result.events, card_owner)

        match.home_goals = result.home_goals
        match.away_goals = result.away_goals
        match.events = events_data
        match.played_at = datetime.utcnow()

        # Статистика — все игроки основы получают appearances=1
        def _save_stats(user_id, cards, stats):
            stats_by_card = {cid: stat for cid, stat in [(cid, s) for cid, s in stats.items()]}
            for card_id, player in cards:
                if card_id == -1:  # фантомный игрок
                    continue
                stat = stats_by_card.get(card_id, {"player_id": player.id, "goals": 0, "assists": 0})
                s = MatchStat(
                    match_id=match.id,
                    user_id=user_id,
                    user_card_id=card_id,
                    player_id=stat["player_id"],
                    goals=stat["goals"],
                    assists=stat["assists"],
                    appearances=1,
                )
                session.add(s)

        _save_stats(match.home_user_id, home_cards, result.home_stats)
        _save_stats(match.away_user_id, away_cards, result.away_stats)

        await session.flush()

        # MVP матча — взвешенный рандом: чем больше г+п тем выше шанс,
        # но любой игрок (включая вратаря) теоретически может стать MVP
        import random as _random
        from sqlalchemy import and_
        mvp_text = None
        all_match_stats_result = await session.execute(
            select(MatchStat).where(MatchStat.match_id == match.id)
        )
        all_match_stats = all_match_stats_result.scalars().all()
        if all_match_stats:
            # Вес = (г*3 + п*2 + 1) * random(0.5, 2.0)
            weights = [(s.goals * 3 + s.assists * 2 + 1) * _random.uniform(0.5, 2.0) for s in all_match_stats]
            mvp_row = _random.choices(all_match_stats, weights=weights, k=1)[0]
            mvp_row.mvp_count = 1
            mvp_owner = wl_map.get(mvp_row.user_id, f"ID{mvp_row.user_id}")
            mvp_player_name = mvp_row.player.name if mvp_row.player else "?"
            g, a = mvp_row.goals, mvp_row.assists
            stat_str = (f"⚽{g}" if g else "") + (f" 🎯{a}" if a else "") or "без г+п"
            mvp_text = f"🏅 <b>MVP матча:</b> {mvp_player_name} (@{mvp_owner}) — {stat_str}"

        await session.commit()

        # Если больше нет несыгранных матчей — помечаем турнир завершённым
        remaining = await get_next_unplayed_match(session, tournament)
        tournament_finished = not remaining
        if tournament_finished:
            tournament.status = "finished"
            await session.commit()
            # Строим итоги для анонса после матча
            final_standings = await build_standings_text(session, tournament.id)

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
                    await asyncio.sleep(4)
                except Exception:
                    pass
        else:
            await bot.send_message(settings.group_id, lineup_text)
            await asyncio.sleep(1)
            summary = format_match_summary(home_name, away_name, result, events_data)
            await bot.send_message(settings.group_id, summary)

        # MVP матча
        if mvp_text:
            await asyncio.sleep(2)
            await bot.send_message(settings.group_id, mvp_text, parse_mode="HTML")

        # Итоги турнира если все матчи сыграны
        if tournament_finished:
            await asyncio.sleep(3)
            # MVP турнира
            tournament_mvp_text = await _get_tournament_mvp_text(session, tournament.id, wl_map)
            standings_msg = f"🏁 <b>Турнир недели завершён!</b>\n\n{final_standings}"
            if tournament_mvp_text:
                standings_msg += f"\n\n{tournament_mvp_text}"
            await bot.send_message(settings.group_id, standings_msg, parse_mode="HTML")

        return True


async def _get_tournament_mvp_text(session: AsyncSession, tournament_id: int, wl_map: dict) -> str | None:
    """Возвращает текст с MVP турнира — игрок с макс г+п за турнир."""
    result = await session.execute(
        select(
            MatchStat.user_id,
            MatchStat.player_id,
            Player.name,
            func.sum(MatchStat.goals).label("g"),
            func.sum(MatchStat.assists).label("a"),
        )
        .join(Player, MatchStat.player_id == Player.id)
        .join(Match, MatchStat.match_id == Match.id)
        .where(Match.tournament_id == tournament_id)
        .group_by(MatchStat.user_id, MatchStat.player_id, Player.name)
        .order_by((func.sum(MatchStat.goals) + func.sum(MatchStat.assists)).desc())
        .limit(1)
    )
    row = result.first()
    if not row or (row.g + row.a) == 0:
        return None
    owner = wl_map.get(row.user_id, f"ID{row.user_id}")
    stat_str = f"⚽{row.g}" + (f" 🎯{row.a}" if row.a else "")
    return f"🏆 <b>MVP турнира:</b> {row.name} (@{owner}) — {stat_str}"


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
            home_formation, home_cards, home_slot_pos = await _get_squad_cards(session, match.home_user_id)
            away_formation, away_cards, away_slot_pos = await _get_squad_cards(session, match.away_user_id)

            if not home_cards or not away_cards:
                continue

            sim_result = simulate_match(home_formation, home_cards, away_formation, away_cards, home_slot_pos, away_slot_pos)
            events_data = events_to_dict(sim_result.events)

            match.home_goals = sim_result.home_goals
            match.away_goals = sim_result.away_goals
            match.events = events_data
            match.played_at = datetime.utcnow()

            def _save_auto_stats(user_id, cards, stats):
                for card_id, player in cards:
                    if card_id == -1:
                        continue
                    stat = stats.get(card_id, {"player_id": player.id, "goals": 0, "assists": 0})
                    s = MatchStat(
                        match_id=match.id,
                        user_id=user_id,
                        user_card_id=card_id,
                        player_id=stat["player_id"],
                        goals=stat["goals"],
                        assists=stat["assists"],
                        appearances=1,
                    )
                    session.add(s)

            _save_auto_stats(match.home_user_id, home_cards, sim_result.home_stats)
            _save_auto_stats(match.away_user_id, away_cards, sim_result.away_stats)

            await session.commit()

            home_wl = await session.get(Whitelist, match.home_user_id)
            away_wl = await session.get(Whitelist, match.away_user_id)
            home_name = (home_wl.username if home_wl and home_wl.username else None) or f"ID{match.home_user_id}"
            away_name = (away_wl.username if away_wl and away_wl.username else None) or f"ID{match.away_user_id}"

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
    wl_rows = wl_result.scalars().all()
    all_user_ids = [w.user_id for w in wl_rows]
    usernames = {w.user_id: (w.username or f"ID{w.user_id}") for w in wl_rows}

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
        name = usernames.get(uid, f"ID{uid}")
        lines.append(
            f"{medal} @{name}: {s['w']}В {s['d']}Н {s['l']}П ({played} игр) | "
            f"{s['gf']}:{s['ga']} | {pts} очк."
        )

    return "\n".join(lines)
