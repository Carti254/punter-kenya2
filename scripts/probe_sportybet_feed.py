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
    "default_current_market_catalogue": {
        "market_ids": "1,18,10,29,11,26,36,14,60100",
        "option": None,
    },
    "full_list_option_2": {
        "market_ids": "1,18,10,29,11,26,36,14,60100",
        "option": "2",
    },
    "match_result_only_option_2": {
        "market_ids": "1",
        "option": "2",
    },
    "no_market_id_parameter": {
        "market_ids": None,
        "option": "2",
    },
}


def main() -> None:
    output = {}
    for name, variant in VARIANTS.items():
        params = dict(BASE_PARAMS)
        market_ids = variant["market_ids"]
        if market_ids is not None:
            params["marketId"] = market_ids
        if variant["option"] is not None:
            params["option"] = variant["option"]
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
            date_counts = {}
            for event in events:
                stamp = event.get("estimateStartTime")
                if stamp:
                    date_text = datetime.fromtimestamp(
                        int(stamp) / 1000, timezone.utc
                    ).astimezone(ZoneInfo("Africa/Nairobi")).date().isoformat()
                    date_counts[date_text] = date_counts.get(date_text, 0) + 1
            output[name] = {
                "http_status": response.status_code,
                "biz_code": payload.get("bizCode"),
                "total_num": data.get("totalNum") if isinstance(data, dict) else None,
                "tournaments": len(tournaments),
                "events": len(events),
                "kickoff_dates_eat": dict(sorted(date_counts.items())),
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
    # Verify whether pageNum is honoured or whether the endpoint repeats page 1.
    try:
        params = dict(BASE_PARAMS, pageNum=2)
        params["marketId"] = VARIANTS["full_list_option_2"]["market_ids"]
        params["option"] = "2"
        response = requests.get(BASE, params=params, headers=HEADERS, timeout=(8, 20))
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        tournaments = data.get("tournaments") or []
        page2_events = [
            event
            for tournament in tournaments if isinstance(tournament, dict)
            for event in (tournament.get("events") or [])
            if isinstance(event, dict)
        ]
        output["page_2_check"] = {
            "http_status": response.status_code,
            "events": len(page2_events),
            "unique_event_ids": len({str(event.get("eventId")) for event in page2_events}),
            "sample_fixtures": [
                f"{event.get('homeTeamName')} vs {event.get('awayTeamName')}"
                for event in page2_events[:5]
            ],
        }
    except Exception as exc:
        output["page_2_check"] = {"error": f"{type(exc).__name__}: {str(exc)[:220]}"}

    # The index carries only the requested market catalogue. Test the
    # per-event endpoint that returns the complete market set for one event.
    try:
        params = dict(BASE_PARAMS, pageNum=1, option="2")
        params["marketId"] = VARIANTS["full_list_option_2"]["market_ids"]
        response = requests.get(BASE, params=params, headers=HEADERS, timeout=(8, 20))
        response.raise_for_status()
        index_payload = response.json()
        index_data = index_payload.get("data") or {}
        index_events = [
            event
            for tournament in (index_data.get("tournaments") or [])
            if isinstance(tournament, dict)
            for event in (tournament.get("events") or [])
            if isinstance(event, dict)
        ]
        if not index_events:
            raise RuntimeError("No event available on the index page for detail lookup.")
        event_id = index_events[0].get("eventId")
        detail_response = requests.get(
            "https://www.sportybet.com/api/ke/factsCenter/event",
            params={"eventId": event_id, "productId": "3", "_t": int(time.time() * 1000)},
            headers=HEADERS,
            timeout=(8, 20),
        )
        detail_response.raise_for_status()
        detail_payload = detail_response.json()
        detail_data = detail_payload.get("data") or {}
        detail_markets = detail_data.get("markets") or [] if isinstance(detail_data, dict) else []
        output["event_detail_all_markets"] = {
            "http_status": detail_response.status_code,
            "biz_code": detail_payload.get("bizCode"),
            "event_id": event_id,
            "fixture": f"{index_events[0].get('homeTeamName')} vs {index_events[0].get('awayTeamName')}",
            "data_type": type(detail_data).__name__,
            "market_count": len(detail_markets),
            "sample_markets": [
                {"id": market.get("id"), "description": market.get("desc")}
                for market in detail_markets[:20]
                if isinstance(market, dict)
            ],
        }
    except Exception as exc:
        output["event_detail_all_markets"] = {"error": f"{type(exc).__name__}: {str(exc)[:250]}"}

    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
