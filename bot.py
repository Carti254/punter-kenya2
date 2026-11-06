elif text == "⚽ Today's Matches":

    url = "https://www.thesportsdb.com/api/v1/json/123/eventsnextleague.php?id=4328"

    try:
        response = requests.get(url, timeout=10)
        data = response.json()

        events = data.get("events", [])

        if not events:
            await update.message.reply_text(
                "No fixtures found."
            )
            return

        message = "⚽ Upcoming Premier League Matches\n\n"

        for match in events[:10]:
            home = match.get("strHomeTeam", "Unknown")
            away = match.get("strAwayTeam", "Unknown")
            date = match.get("dateEvent", "")

            message += f"{home} vs {away}\n📅 {date}\n\n"

        await update.message.reply_text(message)

    except Exception as e:
        await update.message.reply_text(
            f"Error loading fixtures: {e}"
        )
