def get_fixtures():
    url = "https://www.thesportsdb.com/api/v1/json/123/eventsnextleague.php?id=4328"

    response = requests.get(url, timeout=10)
    data = response.json()

    events = data.get("events", [])

    if not events:
        return "No fixtures found."

    message = "⚽ Upcoming Premier League Matches\n\n"

    for match in events[:10]:
        home = match.get("strHomeTeam", "Unknown")
        away = match.get("strAwayTeam", "Unknown")
        date = match.get("dateEvent", "")

        message += f"{home} vs {away}\n📅 {date}\n\n"

    return message
