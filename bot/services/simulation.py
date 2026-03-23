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
    "ST": 35, "CF": 30,
    "CAM": 20, "LW": 18, "RW": 18,
    "CM": 10, "LM": 10, "RM": 10,
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
) -> list[PlayerSlot]:
    """Создаёт расстановку из списка карточек и схемы."""
    slots_positions = FORMATIONS_SLOTS.get(formation, FORMATIONS_SLOTS["4-4-2"])
    lineup = []
    for i, (card_id, player) in enumerate(cards[:11]):
        slot_pos = slots_positions[i] if i < len(slots_positions) else "CM"
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
    """
    base = 1.4  # среднее голов за матч у одной команды
    diff = (home_str - away_str) / 10.0
    diff = max(-2.5, min(2.5, diff))
    home_lambda = base + diff * 0.5 + random.uniform(-0.3, 0.3)
    away_lambda = base - diff * 0.5 + random.uniform(-0.3, 0.3)
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
    """Выбирает слот по вероятностям позиции."""
    weights = [prob_table.get(s.slot_position, 1) for s in slots]
    total = sum(weights)
    if total == 0:
        return random.choice(slots) if slots else None
    return random.choices(slots, weights=weights, k=1)[0]


def generate_events(
    home_goals: int,
    away_goals: int,
    home_lineup: list[PlayerSlot],
    away_lineup: list[PlayerSlot],
) -> tuple[list[MatchEvent], dict, dict]:
    """Генерирует события матча: голы, ассисты, карточки, промахи."""
    events: list[MatchEvent] = []
    home_stats: dict[int, dict] = {}
    away_stats: dict[int, dict] = {}

    used_minutes: set[int] = set()

    def pick_minute(lo: int = 1, hi: int = 90) -> int:
        m = random.randint(lo, hi)
        while m in used_minutes:
            m = random.randint(lo, hi)
        used_minutes.add(m)
        return m

    # Голы
    all_goals = (
        [("home", None) for _ in range(home_goals)] +
        [("away", None) for _ in range(away_goals)]
    )
    random.shuffle(all_goals)

    for team, _ in all_goals:
        lineup = home_lineup if team == "home" else away_lineup
        stats = home_stats if team == "home" else away_stats
        minute = pick_minute()

        scorer = _pick_by_prob(lineup, GOAL_PROBS)
        assister_candidates = [s for s in lineup if s != scorer]
        assister = _pick_by_prob(assister_candidates, ASSIST_PROBS) if random.random() < 0.7 else None

        event = MatchEvent(
            minute=minute,
            event_type="goal",
            scorer_slot=scorer,
            assist_slot=assister,
            team=team,
        )
        events.append(event)

        if scorer:
            cid = scorer.user_card_id
            stats.setdefault(cid, {"goals": 0, "assists": 0, "player_id": scorer.player.id})
            stats[cid]["goals"] += 1
        if assister:
            cid = assister.user_card_id
            stats.setdefault(cid, {"goals": 0, "assists": 0, "player_id": assister.player.id})
            stats[cid]["assists"] += 1

    # Жёлтые карточки (1-3 штуки)
    n_yellow = random.randint(1, 3)
    for _ in range(n_yellow):
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

    # Красная карточка (с вероятностью 15%)
    if random.random() < 0.15:
        team = random.choice(["home", "away"])
        lineup = home_lineup if team == "home" else away_lineup
        player_slot = random.choice(lineup) if lineup else None
        if player_slot:
            events.append(MatchEvent(
                minute=pick_minute(),
                event_type="red_card",
                scorer_slot=player_slot,
                team=team,
            ))

    # Промахи/моменты (1-3 штуки)
    n_miss = random.randint(1, 3)
    for _ in range(n_miss):
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
    return events, home_stats, away_stats


def simulate_match(
    home_formation: str,
    home_cards: list[tuple[int, Player]],
    away_formation: str,
    away_cards: list[tuple[int, Player]],
) -> MatchResult:
    """
    Основная функция симуляции матча.
    Принимает составы двух команд, возвращает MatchResult.
    """
    home_lineup = build_lineup(home_formation, home_cards)
    away_lineup = build_lineup(away_formation, away_cards)

    h_str = team_strength(home_lineup)
    a_str = team_strength(away_lineup)

    h_lam, a_lam = expected_goals(h_str, a_str)
    h_goals = poisson_goals(h_lam)
    a_goals = poisson_goals(a_lam)

    events, home_stats, away_stats = generate_events(
        h_goals, a_goals, home_lineup, away_lineup
    )

    return MatchResult(
        home_goals=h_goals,
        away_goals=a_goals,
        events=events,
        home_stats=home_stats,
        away_stats=away_stats,
    )


def events_to_dict(events: list[MatchEvent], card_owner: dict[int, str] | None = None) -> list[dict]:
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
