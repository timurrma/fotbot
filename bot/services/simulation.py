"""
Симуляция матча.

Алгоритм:
1. Считаем силу каждой команды с учётом штрафов за несоответствие позиции
2. Определяем ожидаемое количество голов через Пуассон
3. Для каждого гола — выбираем автора и ассистента по вероятностям позиции
4. Собираем список событий для LLM-комментатора
"""
import random
from dataclasses import dataclass, field
from typing import Optional

from bot.db.models import Player

# Штрафы за несоответствие позиции игрока слоту схемы
POSITION_PENALTY = {
    "same": 0,       # своя или дополнительная позиция
    "zone": -5,      # чужая позиция в своей зоне
    "cross": -10,    # чужая зона
    "gk_swap": -30,  # вратарь на поле или полевой в ворота
}

# Зоны позиций
POSITION_ZONES = {
    "GK": 0,
    "CB": 1, "LB": 1, "RB": 1,
    "CDM": 2, "CM": 2, "LM": 2, "RM": 2,
    "CAM": 3, "LW": 3, "RW": 3,
    "CF": 4, "ST": 4,
}

# Вероятности гола и ассиста по позиции (в процентах, сумма ~100)
GOAL_PROBS = {
    "ST": 35, "CF": 32,
    "CAM": 22, "LW": 20, "RW": 20,
    "CM": 10, "LM": 15, "RM": 15,
    "CDM": 5,
    "LB": 7, "RB": 7,
    "CB": 4,
    "GK": 1,
}
ASSIST_PROBS = {
    "CAM": 25, "LW": 20, "RW": 20,
    "CM": 20, "LM": 15, "RM": 15,
    "ST": 10, "CF": 10, "CDM": 10,
    "LB": 12, "RB": 12,
    "CB": 2,
    "GK": 1,
}

FORMATIONS_SLOTS: dict[str, list[str]] = {
    "4-4-2": ["GK", "CB", "CB", "LB", "RB", "CM", "CM", "LM", "RM", "ST", "ST"],
    "4-3-3": ["GK", "CB", "CB", "LB", "RB", "CM", "CM", "CDM", "LW", "RW", "ST"],
    "3-5-2": ["GK", "CB", "CB", "CB", "CM", "CM", "CDM", "LM", "RM", "ST", "ST"],
    "5-3-2": ["GK", "CB", "CB", "CB", "LB", "RB", "CM", "CM", "CDM", "ST", "ST"],
}


@dataclass
class PlayerSlot:
    slot_position: str       # требуемая позиция слота в схеме
    player: Player
    user_card_id: int
    effective_rating: int = 0

    def __post_init__(self) -> None:
        self.effective_rating = self.player.overall_rating + compute_penalty(
            self.player, self.slot_position
        )
        self.effective_rating = max(40, self.effective_rating)


@dataclass
class MatchEvent:
    minute: int
    event_type: str          # "goal" | "miss" | "save"
    scorer_slot: Optional[PlayerSlot] = None
    assist_slot: Optional[PlayerSlot] = None
    team: str = ""           # "home" | "away"


@dataclass
class MatchResult:
    home_goals: int
    away_goals: int
    events: list[MatchEvent] = field(default_factory=list)
    home_stats: dict[int, dict] = field(default_factory=dict)  # {user_card_id: {goals, assists}}
    away_stats: dict[int, dict] = field(default_factory=dict)


def compute_penalty(player: Player, slot_position: str) -> int:
    """Вычисляет штраф за несоответствие позиции."""
    player_positions = player.positions  # список из positions_json

    # Своя позиция
    if slot_position in player_positions:
        return POSITION_PENALTY["same"]

    # Вратарь/не вратарь
    is_player_gk = "GK" in player_positions
    is_slot_gk = slot_position == "GK"
    if is_player_gk != is_slot_gk:
        return POSITION_PENALTY["gk_swap"]

    # Зоны
    player_zone = max(
        POSITION_ZONES.get(pos, 2) for pos in player_positions
    ) if player_positions else POSITION_ZONES.get(player.position, 2)
    slot_zone = POSITION_ZONES.get(slot_position, 2)

    if abs(player_zone - slot_zone) <= 1:
        return POSITION_PENALTY["zone"]
    return POSITION_PENALTY["cross"]


def build_lineup(
    formation: str,
    cards: list[tuple[int, Player]],  # [(user_card_id, Player), ...]
    slot_positions: Optional[list] = None,  # реальные позиции слотов если переданы
) -> list[PlayerSlot]:
    """Создаёт расстановку из списка карточек и схемы."""
    fallback_positions = FORMATIONS_SLOTS.get(formation, FORMATIONS_SLOTS["4-4-2"])
    lineup = []
    for i, (card_id, player) in enumerate(cards[:11]):
        if slot_positions and i < len(slot_positions):
            slot_pos = slot_positions[i]
        else:
            slot_pos = fallback_positions[i] if i < len(fallback_positions) else "CM"
        lineup.append(PlayerSlot(
            slot_position=slot_pos,
            player=player,
            user_card_id=card_id,
        ))
    return lineup


def team_strength(lineup: list[PlayerSlot]) -> float:
    """Средний эффективный рейтинг команды."""
    if not lineup:
        return 65.0
    return sum(s.effective_rating for s in lineup) / len(lineup)


def expected_goals(home_str: float, away_str: float) -> tuple[float, float]:
    """
    Вычисляет ожидаемые голы (λ) для Пуассона.
    Разница в силе сдвигает λ, но не более ±2.5.
    10% шанс на "безумный матч" с повышенным base λ.
    """
    # 10% шанс на результативный матч (3-5 голов на команду)
    if random.random() < 0.10:
        base = random.uniform(2.5, 3.5)
    else:
        base = 1.6

    diff = (home_str - away_str) / 10.0
    diff = max(-2.5, min(2.5, diff))
    # Уменьшили влияние разницы 0.5 → 0.3 (слабая команда имеет больше шансов)
    home_lambda = base + diff * 0.3 + random.uniform(-0.4, 0.4)
    away_lambda = base - diff * 0.3 + random.uniform(-0.4, 0.4)
    return max(0.1, home_lambda), max(0.1, away_lambda)


def poisson_goals(lam: float) -> int:
    """Случайное число голов из распределения Пуассона (max 6)."""
    import math
    # Симулируем через экспоненциальные интервалы
    L = math.exp(-lam)
    k, p = 0, 1.0
    while p > L and k < 6:
        p *= random.random()
        k += 1
    return k - 1


def _pick_by_prob(slots: list[PlayerSlot], prob_table: dict[str, int]) -> Optional[PlayerSlot]:
    """Выбирает слот по вероятностям позиции × рейтинг игрока.

    Итоговый вес = позиционная вероятность * (effective_rating / 75).
    Так Холанд (90) с весом позиции 35 получит ~2× больше шансов чем
    защитник (65) с весом позиции 4, даже если у них одинаковый вес позиции.
    """
    if not slots:
        return None
    weights = [
        prob_table.get(s.slot_position, 1) * (s.effective_rating / 75.0)
        for s in slots
    ]
    return random.choices(slots, weights=weights, k=1)[0]


def _generate_goals_for_phase(
    h_lam: float,
    a_lam: float,
    home_lineup: list[PlayerSlot],
    away_lineup: list[PlayerSlot],
    home_stats: dict,
    away_stats: dict,
    used_minutes: set,
    minute_lo: int,
    minute_hi: int,
) -> list[MatchEvent]:
    """Генерирует голы для одной фазы матча (до или после красной)."""
    import math
    events = []

    def pick_minute(lo: int, hi: int) -> int:
        if lo > hi:
            lo, hi = hi, lo
        m = random.randint(lo, hi)
        attempts = 0
        while m in used_minutes and attempts < 20:
            m = random.randint(lo, hi)
            attempts += 1
        used_minutes.add(m)
        return m

    duration = max(1, minute_hi - minute_lo)
    # Масштабируем λ на длину фазы (из расчёта полного матча = 90 мин)
    h_goals = poisson_goals(h_lam * duration / 90)
    a_goals = poisson_goals(a_lam * duration / 90)

    for team, n_goals in [("home", h_goals), ("away", a_goals)]:
        lineup = home_lineup if team == "home" else away_lineup
        stats = home_stats if team == "home" else away_stats
        for _ in range(n_goals):
            minute = pick_minute(minute_lo, minute_hi)
            scorer = _pick_by_prob(lineup, GOAL_PROBS)
            assister_candidates = [s for s in lineup if s != scorer]
            assister = _pick_by_prob(assister_candidates, ASSIST_PROBS) if random.random() < 0.7 else None
            events.append(MatchEvent(
                minute=minute,
                event_type="goal",
                scorer_slot=scorer,
                assist_slot=assister,
                team=team,
            ))
            if scorer:
                cid = scorer.user_card_id
                stats.setdefault(cid, {"goals": 0, "assists": 0, "player_id": scorer.player.id})
                stats[cid]["goals"] += 1
            if assister:
                cid = assister.user_card_id
                stats.setdefault(cid, {"goals": 0, "assists": 0, "player_id": assister.player.id})
                stats[cid]["assists"] += 1

    return events


def generate_events(
    h_lam: float,
    a_lam: float,
    home_lineup: list[PlayerSlot],
    away_lineup: list[PlayerSlot],
) -> tuple[list[MatchEvent], dict, dict, int, int]:
    """Генерирует события матча: голы, ассисты, карточки, промахи.

    Если выпадает красная карточка — голы после неё пересчитываются с новым λ:
    команда в меньшинстве ×0.6, соперник ×1.3. Чем раньше красная — тем сильнее эффект.
    """
    events: list[MatchEvent] = []
    home_stats: dict[int, dict] = {}
    away_stats: dict[int, dict] = {}
    used_minutes: set[int] = set()

    def pick_minute(lo: int = 1, hi: int = 90) -> int:
        if lo > hi:
            lo, hi = hi, lo
        m = random.randint(lo, hi)
        attempts = 0
        while m in used_minutes and attempts < 20:
            m = random.randint(lo, hi)
            attempts += 1
        used_minutes.add(m)
        return m

    # Красная карточка (15% шанс)
    red_minute: Optional[int] = None
    red_team: Optional[str] = None
    if random.random() < 0.15:
        team = random.choice(["home", "away"])
        lineup = home_lineup if team == "home" else away_lineup
        player_slot = random.choice(lineup) if lineup else None
        if player_slot:
            red_minute = pick_minute(5, 85)
            red_team = team
            events.append(MatchEvent(
                minute=red_minute,
                event_type="red_card",
                scorer_slot=player_slot,
                team=team,
            ))

    if red_minute is not None:
        # Фаза 1: до красной (1..red_minute-1) — обычные λ
        phase1 = _generate_goals_for_phase(
            h_lam, a_lam,
            home_lineup, away_lineup,
            home_stats, away_stats,
            used_minutes,
            minute_lo=1, minute_hi=max(1, red_minute - 1),
        )
        events.extend(phase1)

        # Фаза 2: после красной (red_minute+1..90) — скорректированные λ
        remaining = 90 - red_minute
        scale = remaining / 90  # чем позже красная — тем меньше эффект

        if red_team == "home":
            h_lam2 = h_lam * (1 - 0.4 * scale)   # home теряет до 40% угрозы
            a_lam2 = a_lam * (1 + 0.3 * scale)    # away получает до 30% бонуса
        else:
            a_lam2 = a_lam * (1 - 0.4 * scale)
            h_lam2 = h_lam * (1 + 0.3 * scale)

        phase2 = _generate_goals_for_phase(
            h_lam2, a_lam2,
            home_lineup, away_lineup,
            home_stats, away_stats,
            used_minutes,
            minute_lo=min(90, red_minute + 1), minute_hi=90,
        )
        events.extend(phase2)
    else:
        # Нет красной — обычная генерация за весь матч
        phase = _generate_goals_for_phase(
            h_lam, a_lam,
            home_lineup, away_lineup,
            home_stats, away_stats,
            used_minutes,
            minute_lo=1, minute_hi=90,
        )
        events.extend(phase)

    # Жёлтые карточки (1-3 штуки)
    for _ in range(random.randint(1, 3)):
        team = random.choice(["home", "away"])
        lineup = home_lineup if team == "home" else away_lineup
        player_slot = random.choice(lineup) if lineup else None
        if player_slot:
            events.append(MatchEvent(
                minute=pick_minute(),
                event_type="yellow_card",
                scorer_slot=player_slot,
                team=team,
            ))

    # Промахи/моменты (1-3 штуки)
    for _ in range(random.randint(1, 3)):
        team = random.choice(["home", "away"])
        lineup = home_lineup if team == "home" else away_lineup
        player_slot = _pick_by_prob(lineup, GOAL_PROBS) if lineup else None
        if player_slot:
            events.append(MatchEvent(
                minute=pick_minute(),
                event_type="miss",
                scorer_slot=player_slot,
                team=team,
            ))

    events.sort(key=lambda e: e.minute)

    actual_home = sum(1 for e in events if e.event_type == "goal" and e.team == "home")
    actual_away = sum(1 for e in events if e.event_type == "goal" and e.team == "away")

    return events, home_stats, away_stats, actual_home, actual_away


def simulate_match(
    home_formation: str,
    home_cards: list[tuple[int, Player]],
    away_formation: str,
    away_cards: list[tuple[int, Player]],
    home_slot_positions: Optional[list] = None,
    away_slot_positions: Optional[list] = None,
) -> MatchResult:
    """
    Основная функция симуляции матча.
    Принимает составы двух команд, возвращает MatchResult.
    """
    home_lineup = build_lineup(home_formation, home_cards, home_slot_positions)
    away_lineup = build_lineup(away_formation, away_cards, away_slot_positions)

    h_str = team_strength(home_lineup)
    a_str = team_strength(away_lineup)

    h_lam, a_lam = expected_goals(h_str, a_str)

    events, home_stats, away_stats, actual_home, actual_away = generate_events(
        h_lam, a_lam, home_lineup, away_lineup
    )

    return MatchResult(
        home_goals=actual_home,
        away_goals=actual_away,
        events=events,
        home_stats=home_stats,
        away_stats=away_stats,
    )


def events_to_dict(events: list[MatchEvent], card_owner: Optional[dict] = None) -> list[dict]:
    """Сериализует события для хранения в БД и передачи в LLM."""
    card_owner = card_owner or {}
    result = []
    for e in events:
        item: dict = {
            "minute": e.minute,
            "type": e.event_type,
            "team": e.team,
        }
        if e.event_type == "goal":
            item["scorer"] = {
                "name": e.scorer_slot.player.name,
                "owner": card_owner.get(e.scorer_slot.user_card_id, ""),
                "position": e.scorer_slot.slot_position,
                "rating": e.scorer_slot.player.overall_rating,
            } if e.scorer_slot else None
            item["assist"] = {
                "name": e.assist_slot.player.name,
                "owner": card_owner.get(e.assist_slot.user_card_id, ""),
                "position": e.assist_slot.slot_position,
            } if e.assist_slot else None
        elif e.event_type in ("yellow_card", "red_card", "miss", "save"):
            item["player"] = {
                "name": e.scorer_slot.player.name,
                "owner": card_owner.get(e.scorer_slot.user_card_id, ""),
                "position": e.scorer_slot.slot_position,
            } if e.scorer_slot else None
        result.append(item)
    return result
