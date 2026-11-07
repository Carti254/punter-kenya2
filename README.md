# PUNTER KENYA

PUNTER KENYA is a Telegram football-fixture bot. Its current fixture catalogue is powered by the public **open-matchday** dataset; no football-data API key is required for fixture and league discovery.

## Current data source

- Provider: [open-matchday](https://github.com/oladeeayo/open-matchday)
- Competition catalogue: fetched from the provider's league index.
- Fixtures: fetched from the provider's season CSV files.
- Time zone shown to users: East Africa Time (Africa/Nairobi).
- Fixture browsing is limited to **today and tomorrow** when using league-specific searches.
- Provider data is refreshed by its maintainers; it is not a real-time live-score feed.

PUNTER KENYA is an independent project and is not affiliated with open-matchday. The provider's dataset is subject to its own licence and attribution requirements. Review the provider repository before redistributing or using the data commercially.

## Telegram commands

- `/start` — start the bot.
- `/help` — show command help.
- `/about` — show provider attribution.
- `/leagues` — search the synced competition catalogue.
- `/syncleagues` — refresh the competition catalogue.
- `/fixtures` — browse upcoming fixtures.
- `/leaguefixtures COMPETITION` — show fixtures for a competition today and tomorrow.
- `/leaguefixtures COMPETITION today` — show today's fixtures.
- `/leaguefixtures COMPETITION tomorrow` — show tomorrow's fixtures.
- `/matchintel HOME TEAM vs AWAY TEAM` — retrieve available fixture information.
- `/syncfixtures` — sync available fixture records to the database.
- `/live` — explains current live-data limitations.
- `/dbtest` — test the database connection.
- `/calculator` — use the betting calculator.

Live scores and confirmed starting lineups are **not implemented yet**. Do not treat scheduled fixtures as live matches. A statistical prediction engine and verified team-history analysis are also not yet complete; the bot should not present unsupported predictions as facts.

## Deployment configuration

Use Python 3.13 or a compatible recent Python version.

Install dependencies:

```bash
pip install -r requirements.txt
```

Required Telegram environment variable:

- `BOT_TOKEN` (or `TELEGRAM_BOT_TOKEN`)

The database connection in `database.py` uses these environment variables:

- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`

TLS is required for the database connection. `DB_SSL` must remain `true` (the default).

Start the bot:

```bash
python bot.py
```

Never commit Telegram tokens, database passwords, or other credentials to this repository. Set them in the hosting provider's secret/environment-variable settings.

## Project files

- `bot.py` — Telegram handlers, provider retrieval, fixture formatting and command registration.
- `database.py` — secure MySQL-compatible database connection helpers.
- `requirements.txt` — Python dependencies.
- `.github/workflows/python-syntax.yml` — syntax validation on pushes and pull requests targeting `main`.

## Development checks

The GitHub Actions workflow compiles `bot.py` and `database.py` with Python 3.13 to catch syntax errors. This check does not replace a live Telegram deployment test.
