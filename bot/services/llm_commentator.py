"""
LLM-комментатор матча.
Поддерживает OpenAI и OpenRouter (переключается через LLM_PROVIDER в .env).
"""
import json
import os

from openai import AsyncOpenAI

from bot.config import settings
from bot.services.simulation import MatchResult


def _make_client() -> tuple[AsyncOpenAI, str]:
    """Возвращает (client, model) в зависимости от LLM_PROVIDER."""
    if settings.llm_provider == "openrouter":
        client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
        )
        return client, "qwen/qwen3.5-plus-02-15"
    else:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        return client, "gpt-5.4-mini"


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
    model: str,
    skill_prompt: str,
    payload: dict,
) -> list[str]:
    """Генерирует 5-6 сообщений для одного тайма."""
    user_message = (
        f"Прокомментируй {'первый' if payload['half'] == 1 else 'второй'} тайм матча. "
        f"Сделай ровно 10-12 коротких живых сообщений.\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )

    response = await client.chat.completions.create(
        model=model,
        max_completion_tokens=3000,
        messages=[
            {"role": "system", "content": skill_prompt},
            {"role": "user", "content": user_message},
        ],
    )

    import logging
    import re
    raw = response.choices[0].message.content.strip()
    logging.getLogger(__name__).debug("LLM raw response: %r", raw[:500])

    # Убираем <think>...</think> теги (reasoning модели типо Qwen)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

    # Парсим JSON-массив
    try:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start != -1 and end > start:
            parsed = json.loads(raw[start:end])
            if isinstance(parsed, list) and parsed:
                # Если LLM вернул список из одного элемента-строки который сам является JSON
                if len(parsed) == 1 and isinstance(parsed[0], str) and parsed[0].strip().startswith("["):
                    try:
                        inner = json.loads(parsed[0])
                        if isinstance(inner, list):
                            parsed = inner
                    except (json.JSONDecodeError, ValueError):
                        pass
                result_msgs = [item.strip() for item in parsed if isinstance(item, str) and item.strip()]
                if result_msgs:
                    return result_msgs
    except (json.JSONDecodeError, ValueError):
        pass

    # Фоллбэк — разбиваем по двойным переносам или одиночным
    lines = [msg.strip() for msg in raw.split("\n\n") if msg.strip()]
    if not lines:
        lines = [msg.strip() for msg in raw.split("\n") if msg.strip()]
    return lines[:12]


async def rate_players(
    client: "AsyncOpenAI",
    model: str,
    home_username: str,
    away_username: str,
    result: MatchResult,
    events_data: list[dict],
) -> dict[int, float]:
    """
    Просит LLM выставить оценку каждому игроку матча (1.0–3.0).
    Возвращает {user_card_id: score}.
    """
    # Собираем список игроков из lineups
    players_list = []
    for slot in result.events:
        pass  # events не содержат card_id напрямую

    # Собираем из home_stats и away_stats
    all_stats = {**result.home_stats, **result.away_stats}
    if not all_stats:
        return {}

    players_info = []
    for card_id, stat in all_stats.items():
        if card_id == -1:
            continue
        players_info.append({
            "card_id": card_id,
            "player_id": stat["player_id"],
            "goals": stat["goals"],
            "assists": stat["assists"],
        })

    # Добавляем события для контекста
    payload = {
        "home_team": home_username,
        "away_team": away_username,
        "score": f"{result.home_goals}:{result.away_goals}",
        "players": players_info,
        "events": [e for e in events_data if e.get("type") in ("goal", "save", "miss", "yellow_card", "red_card")],
    }

    prompt = (
        "Ты оцениваешь игроков футбольного матча. "
        "На основе событий матча выставь каждому игроку оценку от 1.0 до 3.0 "
        "где 1.0 = обычный матч, 2.0 = хорошая игра, 3.0 = выдающийся матч. "
        "Учитывай голы, ассисты, сейвы, карточки и общее влияние на игру. "
        "Верни ТОЛЬКО JSON объект вида {\"card_id\": score, ...} без пояснений.\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```"
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            max_completion_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        import re as _re
        raw = response.choices[0].message.content.strip()
        raw = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL).strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            parsed = json.loads(raw[start:end])
            return {int(k): float(v) for k, v in parsed.items()}
    except Exception:
        pass
    return {}


async def commentate_match(
    home_username: str,
    away_username: str,
    home_formation: str,
    away_formation: str,
    result: MatchResult,
    events_data: list[dict],
) -> tuple[list[str], dict[int, float]]:
    """
    Генерирует полный комментарий матча: анонс + 1 тайм + перерыв + 2 тайм + итог.
    Возвращает (список сообщений, {card_id: llm_score}).
    """
    client, model = _make_client()
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
            client, model, skill_prompt,
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
            client, model, skill_prompt,
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

    # Оценки игроков от LLM (параллельно не делаем — уже потратили токены)
    llm_scores = await rate_players(client, model, home_username, away_username, result, events_data)

    return messages, llm_scores


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
