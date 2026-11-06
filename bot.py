
import os
import re
import math
import time
import logging
import asyncio
from datetime import datetime, timezone
from collections import defaultdict

import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# PUNTERKENYA AI
# Telegram Football Research & Betting Analysis Bot
# Python 3.10+
# python-telegram-bot 21.10
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

API_FOOTBALL_URL = "https://v3.football.api-sports.io"
ODDS_API_URL = "https://api.the-odds-api.com/v4"

# ============================================================
# CONFIGURATION
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("PunterKenyaAI")

REQUEST_TIMEOUT = 20
CACHE_TTL = 180
MAX_FIXTURES = 30
DEFAULT_REGION = "eu"

ODDS_SPORTS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
    "soccer_england_championship",
    "soccer_england_league1",
    "soccer_england_league2",
    "soccer_portugal_primeira_liga",
    "soccer_netherlands_eredivisie",
    "soccer_belgium_first_div",
    "soccer_turkey_super_league",
    "soccer_scotland_premiership",
    "soccer_greece_super_league",
    "soccer_saudi_professional_league",
]

CACHE = {}
USER_REQUESTS = defaultdict(list)

# ============================================================
# HTTP CLIENT
# ============================================================

session = requests.Session()


def cached_request(key, url, headers=None, params=None, ttl=CACHE_TTL):
    now = time.time()

    if key in CACHE:
        timestamp, cached_data = CACHE[key]

        if now - timestamp < ttl:
            return cached_data

    try:
        response = session.get(
            url,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()
        data = response.json()

        CACHE[key] = (now, data)

        return data

    except requests.RequestException as exc:
        logger.error("HTTP request failed: %s", exc)
        raise RuntimeError("An external football data service is temporarily unavailable.") from exc

    except ValueError as exc:
        logger.error("Invalid JSON response: %s", exc)
        raise RuntimeError("The data provider returned an invalid response.") from exc


def football_get(endpoint, params=None, ttl=CACHE_TTL):
    if not API_FOOTBALL_KEY:
        raise RuntimeError("API_FOOTBALL_KEY is not configured.")

    headers = {
        "x-apisports-key": API_FOOTBALL_KEY,
    }

    data = cached_request(
        f"football:{endpoint}:{params}",
        f"{API_FOOTBALL_URL}/{endpoint}",
        headers=headers,
        params=params or {},
        ttl=ttl,
    )

    errors = data.get("errors")

    if errors:
        raise RuntimeError(f"API-Football error: {errors}")

    return data.get("response", [])


def odds_get(endpoint, params=None, ttl=CACHE_TTL):
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY is not configured.")

    params = dict(params or {})
    params["apiKey"] = ODDS_API_KEY

    data = cached_request(
        f"odds:{endpoint}:{params}",
        f"{ODDS_API_URL}/{endpoint}",
        params=params,
        ttl=ttl,
    )

    return data


# ============================================================
# GENERAL UTILITIES
# ============================================================

def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def poisson_probability(mean, goals):
    if mean <= 0:
        return 1.0 if goals == 0 else 0.0

    return (math.exp(-mean) * mean ** goals) / math.factorial(goals)


def poisson_distribution(mean, maximum=12):
    probabilities = [
        poisson_probability(mean, goals)
        for goals in range(maximum + 1)
    ]

    total = sum(probabilities)

    if total:
        probabilities = [p / total for p in probabilities]

    return probabilities


def calculate_probabilities(home_xg, away_xg):
    home_dist = poisson_distribution(home_xg)
    away_dist = poisson_distribution(away_xg)

    home_win = 0
    draw = 0
    away_win = 0
    btts = 0
    over_15 = 0
    over_25 = 0

    for h, hp in enumerate(home_dist):
        for a, ap in enumerate(away_dist):
            probability = hp * ap

            if h > a:
                home_win += probability
            elif h == a:
                draw += probability
            else:
                away_win += probability

            if h > 0 and a > 0:
                btts += probability

            if h + a >= 2:
                over_15 += probability

            if h + a >= 3:
                over_25 += probability

    return {
        "home_win": home_win,
        "draw": draw,
        "away_win": away_win,
        "btts": btts,
        "over_15": over_15,
        "over_25": over_25,
        "home_xg": home_xg,
        "away_xg": away_xg,
    }


def percentage(value):
    return f"{value * 100:.1f}%"


def decimal_odds(probability):
    if probability <= 0:
        return 0

    return 1 / probability


def implied_probability(odds):
    return 1 / odds if odds > 1 else 0


def normalize_name(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def similarity(a, b):
    a = normalize_name(a)
    b = normalize_name(b)

    if not a or not b:
        return 0

    if a == b:
        return 1

    if a in b or b in a:
        return 0.9

    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def match_names(home, away, event):
    return (
        similarity(home, event.get("home_team", "")) > 0.72
        and similarity(away, event.get("away_team", "")) > 0.72
    )


def get_form_string(fixtures, team_id):
    results = []

    for fixture in fixtures:
        home = fixture["teams"]["home"]
        away = fixture["teams"]["away"]

        goals_home = fixture["goals"]["home"]
        goals_away = fixture["goals"]["away"]

        if goals_home is None or goals_away is None:
            continue

        if home["id"] == team_id:
            if goals_home > goals_away:
                results.append("W")
            elif goals_home == goals_away:
                results.append("D")
            else:
                results.append("L")

        elif away["id"] == team_id:
            if goals_away > goals_home:
                results.append("W")
            elif goals_away == goals_home:
                results.append("D")
            else:
                results.append("L")

    return "".join(results[:5])


# ============================================================
# FOOTBALL DATA
# ============================================================

def get_fixtures(next_matches=20):
    return football_get(
        "fixtures",
        {"next": next_matches},
        ttl=120,
    )


def get_live_fixtures():
    return football_get(
        "fixtures",
        {"live": "all"},
        ttl=30,
    )


def search_team(team_name):
    teams = football_get(
        "teams",
        {"search": team_name},
        ttl=3600,
    )

    if not teams:
        return None

    team_name_normalized = normalize_name(team_name)

    teams.sort(
        key=lambda item: similarity(
            team_name_normalized,
            item["team"]["name"],
        ),
        reverse=True,
    )

    return teams[0]


def get_team_form(team_id):
    return football_get(
        "fixtures",
        {
            "team": team_id,
            "last": 10,
            "status": "FT",
        },
        ttl=300,
    )


def get_h2h(home_id, away_id):
    return football_get(
        "fixtures/headtohead",
        {
            "h2h": f"{home_id}-{away_id}",
            "last": 10,
        },
        ttl=600,
    )


def get_standings(league_id, season):
    return football_get(
        "standings",
        {
            "league": league_id,
            "season": season,
        },
        ttl=1800,
    )


def get_injuries(fixture_id):
    return football_get(
        "injuries",
        {
            "fixture": fixture_id,
        },
        ttl=600,
    )


# ============================================================
# ODDS DATA
# ============================================================

def get_sport_odds(sport):
    try:
        return odds_get(
            f"sports/{sport}/odds",
            {
                "regions": DEFAULT_REGION,
                "markets": "h2h,totals",
                "oddsFormat": "decimal",
            },
            ttl=180,
        )

    except RuntimeError as exc:
        logger.warning("Odds unavailable for %s: %s", sport, exc)
        return []


def get_all_odds():
    events = []

    for sport in ODDS_SPORTS:
        try:
            events.extend(get_sport_odds(sport))
        except Exception:
            logger.exception("Failed retrieving odds for %s", sport)

    return events


def find_match_odds(home, away, odds_events=None):
    if odds_events is None:
        odds_events = get_all_odds()

    for event in odds_events:
        if match_names(home, away, event):
            return event

    return None


def extract_market_odds(event):
    if not event:
        return {}

    results = {}

    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            market_key = market.get("key")

            for outcome in market.get("outcomes", []):
                name = outcome.get("name", "").lower()
                price = safe_float(outcome.get("price"))

                if price <= 1:
                    continue

                if market_key == "h2h":
                    if name == event.get("home_team", "").lower():
                        results.setdefault("home_win", []).append(price)

                    elif name == event.get("away_team", "").lower():
                        results.setdefault("away_win", []).append(price)

                    elif name == "draw":
                        results.setdefault("draw", []).append(price)

                elif market_key == "totals":
                    point = safe_float(outcome.get("point"))

                    if point == 1.5 and name == "over":
                        results.setdefault("over_15", []).append(price)

                    elif point == 2.5 and name == "over":
                        results.setdefault("over_25", []).append(price)

    return results


def best_odds(odds_list):
    return max(odds_list) if odds_list else None


# ============================================================
# PREDICTION ENGINE
# ============================================================

def calculate_team_strength(fixtures, team_id, home_team):
    scored = []
    conceded = []

    for fixture in fixtures:
        home = fixture["teams"]["home"]
        away = fixture["teams"]["away"]

        goals_home = fixture["goals"]["home"]
        goals_away = fixture["goals"]["away"]

        if goals_home is None or goals_away is None:
            continue

        if home["id"] == team_id:
            scored.append(goals_home)
            conceded.append(goals_away)

        elif away["id"] == team_id:
            scored.append(goals_away)
            conceded.append(goals_home)

    if not scored:
        return {
            "scored": 1.2,
            "conceded": 1.2,
            "matches": 0,
        }

    return {
        "scored": sum(scored) / len(scored),
        "conceded": sum(conceded) / len(conceded),
        "matches": len(scored),
    }


def build_prediction(home, away, home_form, away_form):
    home_strength = calculate_team_strength(
        home_form,
        home["team"]["id"],
        True,
    )

    away_strength = calculate_team_strength(
        away_form,
        away["team"]["id"],
        False,
    )

    home_xg = (
        home_strength["scored"] * 0.55
        + away_strength["conceded"] * 0.45
    )

    away_xg = (
        away_strength["scored"] * 0.55
        + home_strength["conceded"] * 0.45
    )

    # Avoid extreme estimates from small or unusual samples.
    home_xg = max(0.15, min(home_xg, 4.0))
    away_xg = max(0.15, min(away_xg, 4.0))

    return (
        calculate_probabilities(home_xg, away_xg),
        home_strength,
        away_strength,
    )


def calculate_value(probability, odds):
    if not odds or odds <= 1:
        return None

    market_probability = implied_probability(odds)

    return {
        "model_probability": probability,
        "market_probability": market_probability,
        "edge": probability - market_probability,
        "expected_value": probability * odds - 1,
        "odds": odds,
    }


def get_recommendations(probabilities, odds):
    recommendations = []

    markets = {
        "Home Win": "home_win",
        "Draw": "draw",
        "Away Win": "away_win",
        "Over 1.5 Goals": "over_15",
        "Over 2.5 Goals": "over_25",
    }

    for label, key in markets.items():
        price = best_odds(odds.get(key, []))

        if not price:
            continue

        value = calculate_value(
            probabilities[key],
            price,
        )

        if value and value["edge"] >= 0.04 and value["expected_value"] > 0:
            recommendations.append({
                "market": label,
                **value,
            })

    return sorted(
        recommendations,
        key=lambda item: item["expected_value"],
        reverse=True,
    )


# ============================================================
# MATCH ANALYSIS
# ============================================================

def analyze_match(home_name, away_name):
    home = search_team(home_name)
    away = search_team(away_name)

    if not home or not away:
        raise RuntimeError("One or both teams could not be found.")

    home_id = home["team"]["id"]
    away_id = away["team"]["id"]

    home_form = get_team_form(home_id)
    away_form = get_team_form(away_id)

    probabilities, home_strength, away_strength = build_prediction(
        home,
        away,
        home_form,
        away_form,
    )

    h2h = get_h2h(home_id, away_id)

    odds_event = find_match_odds(
        home["team"]["name"],
        away["team"]["name"],
    )

    odds = extract_market_odds(odds_event)

    fixture_id = None

    for fixture in get_fixtures(100):
        if (
            fixture["teams"]["home"]["id"] == home_id
            and fixture["teams"]["away"]["id"] == away_id
        ):
            fixture_id = fixture["fixture"]["id"]
            break

    injuries = get_injuries(fixture_id) if fixture_id else []

    return {
        "home": home,
        "away": away,
        "home_form": home_form,
        "away_form": away_form,
        "home_strength": home_strength,
        "away_strength": away_strength,
        "probabilities": probabilities,
        "h2h": h2h,
        "odds": odds,
        "odds_event": odds_event,
        "injuries": injuries,
    }


def format_analysis(data):
    home = data["home"]["team"]["name"]
    away = data["away"]["team"]["name"]

    p = data["probabilities"]

    home_form = get_form_string(
        data["home_form"],
        data["home"]["team"]["id"],
    )

    away_form = get_form_string(
        data["away_form"],
        data["away"]["team"]["id"],
    )

    h2h = data["h2h"]

    home_wins = 0
    draws = 0
    away_wins = 0

    for match in h2h:
        gh = match["goals"]["home"]
        ga = match["goals"]["away"]

        if gh is None or ga is None:
            continue

        if match["teams"]["home"]["id"] == data["home"]["team"]["id"]:
            if gh > ga:
                home_wins += 1
            elif gh == ga:
                draws += 1
            else:
                away_wins += 1
        else:
            if ga > gh:
                home_wins += 1
            elif ga == gh:
                draws += 1
            else:
                away_wins += 1

    lines = [
        f"⚽ {home} vs {away}",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "📊 MATCH PREDICTIONS",
        "━━━━━━━━━━━━━━━━━━",
        f"🏠 Home Win: {percentage(p['home_win'])}",
        f"🤝 Draw: {percentage(p['draw'])}",
        f"✈️ Away Win: {percentage(p['away_win'])}",
        f"⚽ BTTS: {percentage(p['btts'])}",
        f"🔥 Over 1.5: {percentage(p['over_15'])}",
        f"🔥 Over 2.5: {percentage(p['over_25'])}",
        "",
        "📈 EXPECTED GOALS",
        f"{home}: {p['home_xg']:.2f}",
        f"{away}: {p['away_xg']:.2f}",
        "",
        "📋 RECENT FORM",
        f"{home}: {home_form or 'Unavailable'}",
        f"{away}: {away_form or 'Unavailable'}",
        "",
        "🤝 HEAD TO HEAD",
        f"Matches: {len(h2h)}",
        f"{home} wins: {home_wins}",
        f"Draws: {draws}",
        f"{away} wins: {away_wins}",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "💰 AVAILABLE ODDS",
        "━━━━━━━━━━━━━━━━━━",
    ]

    if data["odds"]:
        for market, prices in data["odds"].items():
            lines.append(
                f"{market.replace('_', ' ').title()}: {best_odds(prices):.2f}"
            )
    else:
        lines.append("No matching bookmaker odds found.")

    recommendations = get_recommendations(
        p,
        data["odds"],
    )

    lines.extend([
        "",
        "🎯 VALUE ANALYSIS",
    ])

    if recommendations:
        for item in recommendations[:3]:
            lines.append(
                f"• {item['market']} @ {item['odds']:.2f}"
                f" | Edge: {item['edge'] * 100:.1f}%"
                f" | EV: {item['expected_value'] * 100:.1f}%"
            )
    else:
        lines.append("No qualifying value bets identified.")

    lines.extend([
        "",
        "🏥 INJURY INFORMATION",
    ])

    if data["injuries"]:
        for injury in data["injuries"][:8]:
            player = injury.get("player", {}).get("name", "Unknown")
            team = injury.get("team", {}).get("name", "Unknown")
            reason = injury.get("player", {}).get("reason", "Unavailable")

            lines.append(f"• {player} ({team}): {reason}")
    else:
        lines.append("No injury records returned by API-Football.")

    lines.extend([
        "",
        "⚠️ Probabilities are statistical estimates, not guarantees.",
        "Check confirmed lineups and market prices before staking.",
    ])

    return "\n".join(lines)


# ============================================================
# TELEGRAM RATE LIMITING
# ============================================================

def user_allowed(user_id):
    now = time.time()

    USER_REQUESTS[user_id] = [
        timestamp
        for timestamp in USER_REQUESTS[user_id]
        if now - timestamp < 60
    ]

    if len(USER_REQUESTS[user_id]) >= 12:
        return False

    USER_REQUESTS[user_id].append(now)

    return True


async def send_long_message(update, message):
    for start in range(0, len(message), 4000):
        await update.message.reply_text(
            message[start:start + 4000]
        )


async def execute(update, function, *args):
    try:
        result = await asyncio.to_thread(function, *args)
        return result

    except Exception as exc:
        logger.exception("Command failed: %s", exc)

        await update.message.reply_text(
            f"⚠️ Unable to complete your request.\n\n{exc}"
        )

        return None


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = """
🔥 PUNTERKENYA AI

Your football research and betting-analysis assistant.

━━━━━━━━━━━━━━━━━━

⚽ MATCH ANALYSIS

/analyze Arsenal vs Chelsea

📅 UPCOMING FIXTURES

/fixtures

💎 BANKER RESEARCH

/banker

📊 VALUE BETS

/valuebets

🎯 ACCUMULATORS

/acca

🔴 LIVE MATCHES

/live

━━━━━━━━━━━━━━━━━━

I analyse football statistics, team form,
head-to-head records, bookmaker odds
and estimated probabilities.

No guaranteed wins. Always assess risk.
"""

    await update.message.reply_text(message)


async def fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches = await execute(update, get_fixtures, MAX_FIXTURES)

    if matches is None:
        return

    if not matches:
        await update.message.reply_text("No upcoming fixtures found.")
        return

    lines = ["⚽ UPCOMING FOOTBALL FIXTURES", ""]

    for match in matches[:20]:
        fixture = match["fixture"]
        teams = match["teams"]
        league = match["league"]

        date = fixture.get("date", "")
        try:
            formatted_date = datetime.fromisoformat(
                date.replace("Z", "+00:00")
            ).strftime("%d %b %H:%M UTC")
        except ValueError:
            formatted_date = date

        lines.append(
            f"🏆 {league['name']}\n"
            f"{teams['home']['name']} vs {teams['away']['name']}\n"
            f"🕒 {formatted_date}\n"
        )

    await send_long_message(update, "\n".join(lines))


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n/analyze Arsenal vs Chelsea"
        )
        return

    query = " ".join(context.args)

    if " vs " not in query.lower():
        await update.message.reply_text(
            "Please use this format:\n/analyze Arsenal vs Chelsea"
        )
        return

    home, away = re.split(
        r"\s+vs\s+",
        query,
        maxsplit=1,
        flags=re.IGNORECASE,
    )

    home = home.strip()
    away = away.strip()

    if not home or not away:
        await update.message.reply_text("Please provide both team names.")
        return

    await update.message.reply_text(
        f"🔍 Researching {home} vs {away}...\n"
        "Checking form, H2H, injuries and odds."
    )

    data = await execute(
        update,
        analyze_match,
        home,
        away,
    )

    if data:
        await send_long_message(
            update,
            format_analysis(data),
        )


# ============================================================
# BANKER ENGINE
# ============================================================

def banker_engine():
    matches = get_fixtures(40)
    odds_events = get_all_odds()

    candidates = []

    for match in matches:
        home = match["teams"]["home"]
        away = match["teams"]["away"]

        try:
            home_form = get_team_form(home["id"])
            away_form = get_team_form(away["id"])

            home_info = {
                "team": home,
            }

            away_info = {
                "team": away,
            }

            probabilities, _, _ = build_prediction(
                home_info,
                away_info,
                home_form,
                away_form,
            )

            event = find_match_odds(
                home["name"],
                away["name"],
                odds_events,
            )

            odds = extract_market_odds(event)

            markets = [
                ("Over 1.5 Goals", "over_15"),
                ("Home Win", "home_win"),
                ("Away Win", "away_win"),
            ]

            best = None

            for label, key in markets:
                price = best_odds(odds.get(key, []))

                if not price:
                    continue

                probability = probabilities[key]

                # Conservative banker filter.
                if probability < 0.75:
                    continue

                if probability * price - 1 <= 0:
                    continue

                candidate = {
                    "home": home["name"],
                    "away": away["name"],
                    "market": label,
                    "probability": probability,
                    "odds": price,
                }

                if best is None or probability > best["probability"]:
                    best = candidate

            if best:
                candidates.append(best)

        except Exception as exc:
            logger.warning(
                "Skipping banker fixture %s vs %s: %s",
                home["name"],
                away["name"],
                exc,
            )

    return sorted(
        candidates,
        key=lambda item: item["probability"],
        reverse=True,
    )[:10]


async def banker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 Scanning upcoming matches for high-probability selections..."
    )

    candidates = await execute(update, banker_engine)

    if candidates is None:
        return

    if not candidates:
        await update.message.reply_text(
            "No selections currently meet the banker criteria.\n"
            "Do not force a bet when the available evidence is insufficient."
        )
        return

    lines = [
        "💎 PUNTERKENYA BANKER RESEARCH",
        "",
        "Selections passing the statistical filters:",
        "",
    ]

    for index, item in enumerate(candidates, 1):
        lines.extend([
            f"{index}. {item['home']} vs {item['away']}",
            f"🎯 {item['market']}",
            f"📊 Estimated probability: {percentage(item['probability'])}",
            f"💰 Best available odds: {item['odds']:.2f}",
            "",
        ])

    lines.append(
        "⚠️ High probability does not mean guaranteed profit."
    )

    await send_long_message(update, "\n".join(lines))


# ============================================================
# VALUE BET ENGINE
# ============================================================

def valuebet_engine():
    matches = get_fixtures(40)
    odds_events = get_all_odds()

    opportunities = []

    for match in matches:
        home = match["teams"]["home"]
        away = match["teams"]["away"]

        try:
            home_form = get_team_form(home["id"])
            away_form = get_team_form(away["id"])

            probabilities, _, _ = build_prediction(
                {"team": home},
                {"team": away},
                home_form,
                away_form,
            )

            event = find_match_odds(
                home["name"],
                away["name"],
                odds_events,
            )

            odds = extract_market_odds(event)

            opportunities.extend([
                {
                    "home": home["name"],
                    "away": away["name"],
                    **item,
                }
                for item in get_recommendations(
                    probabilities,
                    odds,
                )
            ])

        except Exception as exc:
            logger.warning(
                "Value analysis failed: %s",
                exc,
            )

    return sorted(
        opportunities,
        key=lambda item: item["expected_value"],
        reverse=True,
    )[:15]


async def valuebets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💎 Searching bookmaker markets for potential value bets..."
    )

    results = await execute(update, valuebet_engine)

    if results is None:
        return

    if not results:
        await update.message.reply_text(
            "No qualifying value bets found.\n"
            "No selection is better than forcing a poor price."
        )
        return

    lines = [
        "💎 PUNTERKENYA VALUE BETS",
        "",
        "Selections with positive estimated expected value:",
        "",
    ]

    for index, item in enumerate(results, 1):
        lines.extend([
            f"{index}. {item['home']} vs {item['away']}",
            f"🎯 {item['market']}",
            f"💰 Odds: {item['odds']:.2f}",
            f"📊 Model probability: {percentage(item['model_probability'])}",
            f"📉 Market probability: {percentage(item['market_probability'])}",
            f"📈 Probability edge: {item['edge'] * 100:.1f}%",
            f"💎 Expected value: {item['expected_value'] * 100:.1f}%",
            "",
        ])

    await send_long_message(update, "\n".join(lines))


# ============================================================
# ACCUMULATOR ENGINE
# ============================================================

def accumulator_engine():
    matches = get_fixtures(40)
    odds_events = get_all_odds()

    candidates = []

    for match in matches:
        home = match["teams"]["home"]
        away = match["teams"]["away"]

        try:
            home_form = get_team_form(home["id"])
            away_form = get_team_form(away["id"])

            probabilities, _, _ = build_prediction(
                {"team": home},
                {"team": away},
                home_form,
                away_form,
            )

            event = find_match_odds(
                home["name"],
                away["name"],
                odds_events,
            )

            odds = extract_market_odds(event)

            # Use only selections passing both probability
            # and positive expected-value filters.
            for label, key in [
                ("Over 1.5 Goals", "over_15"),
                ("Home Win", "home_win"),
                ("Away Win", "away_win"),
            ]:
                price = best_odds(odds.get(key, []))

                if not price:
                    continue

                probability = probabilities[key]

                if probability < 0.70:
                    continue

                if probability * price - 1 <= 0:
                    continue

                candidates.append({
                    "home": home["name"],
                    "away": away["name"],
                    "market": label,
                    "probability": probability,
                    "odds": price,
                })

        except Exception as exc:
            logger.warning("Accumulator fixture skipped: %s", exc)

    return sorted(
        candidates,
        key=lambda item: item["probability"],
        reverse=True,
    )


async def acca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 Building a statistical accumulator...\n"
        "Checking fixtures, probabilities and bookmaker prices."
    )

    results = await execute(update, accumulator_engine)

    if results is None:
        return

    # Avoid duplicate fixtures in the same accumulator.
    selected = []
    used_matches = set()

    for item in results:
        match_key = (
            item["home"],
            item["away"],
        )

        if match_key in used_matches:
            continue

        selected.append(item)
        used_matches.add(match_key)

        if len(selected) == 3:
            break

    if len(selected) < 2:
        await update.message.reply_text(
            "Insufficient qualifying matches for an accumulator."
        )
        return

    combined_odds = math.prod(
        item["odds"] for item in selected
    )

    # Independence assumption; actual correlation may change this.
    combined_probability = math.prod(
        item["probability"] for item in selected
    )

    lines = [
        "🎯 PUNTERKENYA ACCUMULATOR",
        "",
        f"📊 Selections: {len(selected)}",
        "",
    ]

    for index, item in enumerate(selected, 1):
        lines.extend([
            f"{index}. {item['home']} vs {item['away']}",
            f"🎯 {item['market']}",
            f"Odds: {item['odds']:.2f}",
            f"Probability: {percentage(item['probability'])}",
            "",
        ])

    lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        f"💰 Combined odds: {combined_odds:.2f}",
        f"📊 Estimated combined probability: {percentage(combined_probability)}",
        "━━━━━━━━━━━━━━━━━━",
        "",
        "⚠️ Combined probability assumes independent selections.",
        "Multiple selections increase overall risk.",
    ])

    await send_long_message(update, "\n".join(lines))


# ============================================================
# LIVE MATCHES
# ============================================================

def live_engine():
    matches = get_live_fixtures()

    results = []

    for match in matches:
        fixture = match["fixture"]
        teams = match["teams"]
        goals = match["goals"]
        league = match["league"]

        results.append({
            "league": league["name"],
            "home": teams["home"]["name"],
            "away": teams["away"]["name"],
            "home_goals": goals["home"],
            "away_goals": goals["away"],
            "elapsed": fixture["status"].get("elapsed"),
            "status": fixture["status"].get("short"),
            "fixture_id": fixture["id"],
        })

    return results


async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔴 Fetching live football matches..."
    )

    matches = await execute(update, live_engine)

    if matches is None:
        return

    if not matches:
        await update.message.reply_text(
            "🔴 No live matches currently available."
        )
        return

    lines = [
        "🔴 LIVE FOOTBALL MATCHES",
        "",
    ]

    for match in matches[:30]:
        lines.extend([
            f"🏆 {match['league']}",
            f"{match['home']} {match['home_goals']} - "
            f"{match['away_goals']} {match['away']}",
            f"⏱ {match['elapsed'] or '?'} minutes | {match['status']}",
            "",
        ])

    await send_long_message(update, "\n".join(lines))


# ============================================================
# NATURAL LANGUAGE MATCH REQUESTS
# ============================================================

async def natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text.strip()

    match = re.match(
        r"^(.+?)\s+vs\s+(.+?)$",
        message,
        re.IGNORECASE,
    )

    if not match:
        await update.message.reply_text(
            "Send a command or enter a match like:\n"
            "Arsenal vs Chelsea"
        )
        return

    context.args = [
        match.group(1),
        "vs",
        match.group(2),
    ]

    await analyze(update, context)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram application error",
        exc_info=context.error,
    )


# ============================================================
# APPLICATION STARTUP
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    if not API_FOOTBALL_KEY:
        raise RuntimeError("API_FOOTBALL_KEY environment variable is missing.")

    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY environment variable is missing.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("fixtures", fixtures)
    )

    application.add_handler(
        CommandHandler("analyze", analyze)
    )

    application.add_handler(
        CommandHandler("banker", banker)
    )

    application.add_handler(
        CommandHandler("valuebets", valuebets)
    )

    application.add_handler(
        CommandHandler("acca", acca)
    )

    application.add_handler(
        CommandHandler("live", live)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            natural_language,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("PunterKenya AI is starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
