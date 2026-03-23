"""
LLM-комментатор матча через OpenAI GPT-4.1 nano.
Публикует матч двумя таймами по 5-6 сообщений каждый.
"""
import json
import os

from openai import AsyncOpenAI

from bot.config import settings
from bot.services.simulation import MatchResult


def _load_skill_prompt() -> str:
    skill_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "skills", "match-commentator", "SKILL.md",
    )
    with open(skill_path, encoding="utf-8") as f:
        content = f.read()
    parts = content.split("---", 2)
    return parts[2].strip() if len(parts) >= 3 else content


def _split_events_by_half(events_data: list[dict]) -> tuple[list[dict], list[dict]]:
    """Делит события матча на два тайма (1-45 и 46-90 мин)."""
    first = [e for e in events_data if e["minute"] <= 45]
    second = [e for e in events_data if e["minute"] > 45]
    return first, second


def _build_half_payload(
    home_username: str,
    away_username: str,
    half: int,
    events: list[dict],
    score_before: tuple[int, int],
    score_after: tuple[int, int],
    home_formation: str,
    away_formation: str,
) -> dict:
    return {
        "home_team": home_username,
        "away_team": away_username,
        "half": half,
        "home_formation": home_formation,
        "away_formation": away_formation,
        "events": events,
        "score_at_start": {"home": score_before[0], "away": score_before[1]},
        "score_at_end": {"home": score_after[0], "away": score_after[1]},
    }


async def commentate_half(
    client: AsyncOpenAI,
    skill_prompt: str,
    payload: dict,
) -> list[str]:
    """Генерирует 5-6 сообщений для одного тайма через GPT-4.1 nano."""
    user_message = (
        f"Прокомментируй {'первый' if payload['half'] == 1 else 'второй'} тайм матча. "
        f"Сделай 5-6 коротких живых сообщений.\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )

    response = await client.chat.completions.create(
        model="gpt-5.4-nano",
        max_completion_tokens=1500,
        messages=[
            {"role": "system", "content": skill_prompt},
            {"role": "user", "content": user_message},
        ],
    )

    raw = response.choices[0].message.content.strip()

    # Парсим JSON-массив
    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start != -1 and end > start:
            return json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError):
        pass

    # Фоллбэк — разбиваем по двойным переносам
    return [msg.strip() for msg in raw.split("\n\n") if msg.strip()][:6]


async def commentate_match(
    home_username: str,
    away_username: str,
    home_formation: str,
    away_formation: str,
    result: MatchResult,
    events_data: list[dict],
) -> list[str]:
    """
    Генерирует полный комментарий матча: анонс + 1 тайм + перерыв + 2 тайм + итог.
    Возвращает список строк — каждая строка отдельное Telegram-сообщение.
    """
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    skill_prompt = _load_skill_prompt()

    first_half_events, second_half_events = _split_events_by_half(events_data)

    # Счёт после 1 тайма
    h1 = sum(1 for e in first_half_events if e["team"] == "home" and e["type"] == "goal")
    a1 = sum(1 for e in first_half_events if e["team"] == "away" and e["type"] == "goal")

    messages: list[str] = []

    # Анонс матча
    messages.append(
        f"⚽ МАТЧ НАЧИНАЕТСЯ!\n"
        f"🏠 {home_username} ({home_formation})\n"
        f"✈️ {away_username} ({away_formation})\n\n"
        f"Судья даёт свисток!"
    )

    # Первый тайм
    try:
        first_half_msgs = await commentate_half(
            client, skill_prompt,
            _build_half_payload(
                home_username, away_username,
                half=1,
                events=first_half_events,
                score_before=(0, 0),
                score_after=(h1, a1),
                home_formation=home_formation,
                away_formation=away_formation,
            ),
        )
        messages.extend(first_half_msgs)
    except Exception:
        messages.append(f"⏱ Первый тайм завершён\n\n🏠 {h1} — {a1} ✈️")

    # Перерыв
    messages.append(f"🔔 ПЕРЕРЫВ!\n\n🏠 {home_username} {h1} — {a1} {away_username} ✈️")

    # Второй тайм
    try:
        second_half_msgs = await commentate_half(
            client, skill_prompt,
            _build_half_payload(
                home_username, away_username,
                half=2,
                events=second_half_events,
                score_before=(h1, a1),
                score_after=(result.home_goals, result.away_goals),
                home_formation=home_formation,
                away_formation=away_formation,
            ),
        )
        messages.extend(second_half_msgs)
    except Exception:
        messages.append(
            f"⏱ Второй тайм завершён\n\n"
            f"🏠 {result.home_goals} — {result.away_goals} ✈️"
        )

    # Финал — список голов
    goals = [e for e in events_data if e["type"] == "goal"]
    goals_by_team: dict[str, list[str]] = {home_username: [], away_username: []}
    for e in sorted(goals, key=lambda x: x["minute"]):
        scorer = e.get("scorer") or {}
        assist = e.get("assist")
        owner = scorer.get("owner", "")
        name = scorer.get("name", "?")
        assist_str = f" (acc. {assist['name']})" if assist else ""
        line = f"{name}{assist_str} {e['minute']}'"
        team_name = home_username if e["team"] == "home" else away_username
        goals_by_team.setdefault(team_name, []).append(line)

    goals_text = ""
    for team_name, glist in goals_by_team.items():
        if glist:
            goals_text += f"\n{team_name}: " + ", ".join(glist)

    if result.home_goals > result.away_goals:
        winner_line = f"🏆 Победа {home_username}!"
    elif result.away_goals > result.home_goals:
        winner_line = f"🏆 Победа {away_username}!"
    else:
        winner_line = "🤝 Ничья!"

    messages.append(
        f"🏁 ФИНАЛЬНЫЙ СВИСТОК!\n\n"
        f"🏠 {home_username} {result.home_goals} — {result.away_goals} {away_username} ✈️"
        f"{goals_text}\n\n"
        f"{winner_line}"
    )

    return messages


def format_match_summary(
    home_username: str,
    away_username: str,
    result: MatchResult,
    events_data: list[dict],
) -> str:
    """
    Краткий итог матча без LLM — для авто-анонса через день.
    Показывает счёт, голы, ассисты, жёлтые/красные карточки.
    """
    lines = [
        f"📊 *{home_username} {result.home_goals} — {result.away_goals} {away_username}*\n"
    ]

    goals = [e for e in events_data if e["type"] == "goal"]
    if goals:
        lines.append("⚽ Голы:")
        for e in goals:
            team_icon = "🏠" if e["team"] == "home" else "✈️"
            scorer = e.get("scorer", {})
            assist = e.get("assist")
            scorer_str = scorer.get("name", "?") if scorer else "?"
            assist_str = f" (acc. {assist['name']})" if assist else ""
            lines.append(f"  {team_icon} {e['minute']}' — {scorer_str}{assist_str}")

    yellows = [e for e in events_data if e["type"] == "yellow_card"]
    if yellows:
        lines.append("\n🟨 Жёлтые:")
        for e in yellows:
            team_icon = "🏠" if e["team"] == "home" else "✈️"
            lines.append(f"  {team_icon} {e['minute']}' — {e.get('player', {}).get('name', '?')}")

    reds = [e for e in events_data if e["type"] == "red_card"]
    if reds:
        lines.append("\n🟥 Красные:")
        for e in reds:
            team_icon = "🏠" if e["team"] == "home" else "✈️"
            lines.append(f"  {team_icon} {e['minute']}' — {e.get('player', {}).get('name', '?')}")

    return "\n".join(lines)
