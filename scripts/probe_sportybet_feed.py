"""Read-only diagnostic for SportyBet's public upcoming-event market catalogue.

This script does not authenticate, place bets, or create booking codes. It compares
whether the public feed returns upcoming football events with the current market
catalogue filter versus an omitted or single-market filter.
"""
from __future__ import annotations

import json
import time
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = "https://www.sportybet.com/api/ke/factsCenter/pcUpcomingEvents"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Current-Country": "KE",
    "Referer": "https://www.sportybet.com/ke/sport/football",
    "User-Agent": "Mozilla/5.0 (compatible; PunterKenya/1.0)",
}
BASE_PARAMS = {
    "sportId": "sr:sport:1",
    "pageSize": 100,
    "pageNum": 1,
    "todayGames": "false",
    "timeline": 720,
    "_t": int(time.time() * 1000),
}
VARIANTS = {
    "current_market_catalogue": "1,18,10,29,11,26,36,14,60100",
    "match_result_only": "1",
    "no_market_id_parameter": None,
}


def main() -> None:
    output = {}
    for name, market_ids in VARIANTS.items():
        params = dict(BASE_PARAMS)
        if market_ids is not None:
            params["marketId"] = market_ids
        try:
            response = requests.get(BASE, params=params, headers=HEADERS, timeout=(8, 20))
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") or {}
            tournaments = data.get("tournaments") or []
            events = [
                event
                for tournament in tournaments if isinstance(tournament, dict)
                for event in (tournament.get("events") or [])
                if isinstance(event, dict)
            ]
            output[name] = {
                "http_status": response.status_code,
                "biz_code": payload.get("bizCode"),
                "tournaments": len(tournaments),
                "events": len(events),
                "sample_fixtures": [
                    {
                        "fixture": f"{event.get('homeTeamName')} vs {event.get('awayTeamName')}",
                        "kickoff_eat": datetime.fromtimestamp(
                            int(event.get("estimateStartTime") or 0) / 1000,
                            timezone.utc,
                        ).astimezone(ZoneInfo("Africa/Nairobi")).strftime("%Y-%m-%d %H:%M"),
                    }
                    for event in events[:10]
                    if event.get("estimateStartTime")
                ],
                "sample_market_ids": sorted({
                    str(market.get("id"))
                    for event in events[:10]
                    for market in (event.get("markets") or [])
                    if isinstance(market, dict)
                })[:30],
            }
        except Exception as exc:
            output[name] = {"error": f"{type(exc).__name__}: {str(exc)[:220]}"}
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
