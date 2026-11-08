# PUNTER KENYA

Telegram-based football fixtures, match research, bookmaker-odds inspection and slip-building assistant. The project is designed to run locally; a paid hosting service is not required.

## What it does

- Browses football fixtures and competitions from the open-matchday dataset.
- Displays fixture times in East Africa Time (Africa/Nairobi).
- Provides form, goals, home/away, head-to-head, matchup, risk and match-intelligence reports where source data is available.
- Reads the public, undocumented SportyBet Kenya football feed when that feed is accessible.
- Lets a user resolve selections against current SportyBet fixture/market IDs, review the selections and confirm creation of a genuine SportyBet share/booking code.
- Includes manual slip management, calculator and experimental model-based analysis.

**Important limitations:** SportyBet endpoints are undocumented and can change, rate-limit or block requests. The bot does not log in to bookmaker accounts, deposit funds or place bets. A booking/share code is not a placed bet. Model probabilities are experimental and uncalibrated; they are not guarantees or financial advice. Unsupported or unavailable data must be shown as unavailable, not invented.

## Run locally (no hosting provider)

Requirements: Python 3.11+ and a Telegram bot token from BotFather.

1. Download or clone this repository.
2. Install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   # Windows PowerShell: .venv\\Scripts\\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Set your Telegram token as an environment variable:

   Linux/macOS:
   ```bash
   export BOT_TOKEN="your-telegram-bot-token"
   ```

   Windows PowerShell:
   ```powershell
   $env:BOT_TOKEN="your-telegram-bot-token"
   ```

4. Start the bot:

   ```bash
   python bot.py
   ```

Keep the terminal/process running while you use the bot. Local polling stops when the computer is shut down or the process exits. Never paste your token into source code, GitHub, screenshots or public chats.

## Database (optional)

The bot can start without a database. In database-free local mode, commands that need persistent storage (for example saved slips, forecast records, odds history and database diagnostics) will not be available.

To enable those features, configure a MySQL-compatible database using:

- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`
- `DB_SSL=true` (TLS is required)

The database account needs permission to create and write the tables used by the bot. Do not commit database credentials.

## Telegram commands

- `/start`, `/help`, `/about` — start and help.
- `/leagues`, `/syncleagues` — competition catalogue.
- `/fixtures`, `/fixtures today`, `/fixtures tomorrow` — fixture browsing.
- `/leaguefixtures COMPETITION [today|tomorrow]` — competition fixtures.
- `/matchintel HOME vs AWAY` — match information.
- `/form`, `/goals`, `/homeaway`, `/h2h`, `/strength`, `/matchup`, `/markets`, `/risk`, `/scenarios`, `/deepdive` — match research commands.
- `/bookies` — check the SportyBet public feed.
- `/odds HOME vs AWAY [today|tomorrow]` — inspect matched prices.
- `/topvalue`, `/aipicks`, `/accumulator` — experimental scanner tools; may correctly return NO BET.
- `/calculator ODDS STAKE` — calculate implied probability and potential return.
- `/record`, `/oddshistory`, `/myslips` — persistent tools; require the optional database.
- `/fullintel` — full command directory.

The Telegram menu also includes the SportyBet slip builder. It accepts one selection per line in this format:

```text
Fixture | Market | Selection
Arsenal vs Chelsea | 1X2 | Arsenal
Liverpool vs Brighton | 1X2 | Draw
```

The bot must show the resolved current selections and obtain confirmation before requesting a booking/share code from SportyBet. It must never claim that a bet has been placed.

## Data sources and bookmaker coverage

- Fixtures and competition catalogue: [open-matchday](https://github.com/oladeeayo/open-matchday). This is a community-maintained dataset, not a real-time live-score service.
- Bookmaker adapter: SportyBet Kenya only. Betika and BetPawa are not configured.
- SportyBet's public endpoints are not a supported public API contract. Feed completeness and availability are not guaranteed.

## Development and tests

Install dependencies, then run:

```bash
python -m py_compile bot.py database.py bookmaker_odds.py sportybet_client.py slip_manager.py
python -m unittest discover -s tests -v
```

GitHub Actions runs Python validation and unit tests on pushes and pull requests to `main`.
