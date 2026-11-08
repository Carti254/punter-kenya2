# PUNTER KENYA

Telegram-based football fixtures, match research, bookmaker-odds inspection and slip-building assistant. The project is designed to run locally; a paid hosting service is not required.

## What it does

- Browses football fixtures and competitions from the open-matchday dataset.
- Displays fixture times in East Africa Time (Africa/Nairobi).
- Provides form, goals, home/away, head-to-head, matchup, risk and match-intelligence reports where source data is available.
- Reads public, undocumented Betika Kenya and SportyBet Kenya football odds feeds when those feeds are accessible.
- Lets a user resolve selections against current SportyBet fixture/market IDs, review the selections and confirm creation of a genuine SportyBet share/booking code.
- Includes manual and AI-assisted SportyBet slip management, calculator, bet logging, automatic results settlement and experimental model-based analysis.

**Important limitations:** SportyBet endpoints are undocumented and can change, rate-limit or block requests. The bot does not log in to bookmaker accounts, deposit funds or place bets. A booking/share code is not a placed bet. Model probabilities are experimental and uncalibrated; they are not guarantees or financial advice. Unsupported or unavailable data must be shown as unavailable, not invented.


## AI assistant (optional)

PUNTER can use the official OpenAI Python SDK for natural-language navigation and football slip screenshot reading. The AI routes requests into the bot's existing fixture, match-research, odds and slip-preview workflows; it does not invent bookmaker odds or calculate model probabilities. Screenshot selections are only candidates until they are checked against current SportyBet Kenya fixtures, markets and prices.

Set these variables in Railway **Variables** (or your local environment):

- `OPENAI_API_KEY` — your OpenAI API key (keep it secret; never commit it).
- `OPENAI_MODEL` — optional text model; defaults to `gpt-4.1-mini`.
- `OPENAI_VISION_MODEL` — optional image-capable model; defaults to `gpt-4.1-mini`.

After adding the key, redeploy the service. In Telegram you can ask questions such as “Analyse Brighton vs Brentford”, “Show me today's fixtures”, “Compare the odds for Arsenal vs Chelsea”, or “Build a slip preview to combined odds 5”. You can also send a betting-slip screenshot, inspect the extracted legs, and reply `VALIDATE` to check them against live SportyBet Kenya prices.

AI usage is billed separately by OpenAI and requires an API account with billing enabled. No OpenAI key is included in this repository. Without the key, existing commands and manual slip workflows continue to work.

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

4. (Optional) Configure GOAL API supplementary results/statistics access by setting `GOAL_API_BASE` and `GOAL_API_KEY` from the existing provider account. The main open-matchday fixture and competition catalogue does not require these values.

5. Start the bot:

   ```bash
   python bot.py
   ```

Keep the terminal/process running while you use the bot. Local polling stops when the computer is shut down or the process exits. Never paste tokens or API keys into source code, GitHub, screenshots or public chats.

## Database (optional)

The bot can start without a database. In database-free local mode, commands that need persistent storage (for example saved slips, forecast records, odds history and database diagnostics) will not be available.

To enable those features, configure a MySQL-compatible database using the variables below. The bot runs idempotent, versioned schema migrations at startup when the database is configured; migration version records are stored in `punter_schema_migrations`. The migration also adds the optional `matches.last_synced_at` column when the legacy `matches` table exists and lacks it.

- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`
- `DB_SSL=true` (TLS is required)

The database account needs permission to create and write the tables used by the bot. Do not commit database credentials.\n\nFor an explicit production check, open the deployed service's console and run `python schema_migrations.py`. It connects using the existing `DB_*` environment variables, applies the idempotent migrations, and verifies the recorded schema version without printing credentials. The bot also runs migrations during startup when database access is configured.

## Telegram commands

- `/start`, `/help`, `/about` — start and help.
- `/leagues`, `/syncleagues` — competition catalogue.
- `/fixtures`, `/fixtures today`, `/fixtures tomorrow` — fixture browsing.
- `/leaguefixtures COMPETITION [today|tomorrow]` — competition fixtures.
- `/matchintel HOME vs AWAY` — match information.
- `/form`, `/goals`, `/homeaway`, `/h2h`, `/strength`, `/matchup`, `/markets`, `/risk`, `/scenarios`, `/deepdive` — match research commands.
- `/bookies` — check both Betika and SportyBet public feeds.
- `/odds HOME vs AWAY [today|tomorrow]` — inspect matched prices.
- `/topvalue`, `/aipicks`, `/accumulator` — experimental scanner tools; may correctly return NO BET.
- `/autobuild [TARGET] [MARKET FILTER] [rank=value|odds|probability]` — build a SportyBet preview using your chosen market filter, target odds and ranking. Examples: `/autobuild 5 BTTS rank=odds`, `/autobuild 20 Correct Score rank=odds`, or `/autobuild 10 market=1X2,Under 2.5 rank=value`. There is no forced low-risk ranking; the default ranks by estimated value, while `odds` and `probability` are explicit alternatives.
- `/calculator ODDS STAKE` — calculate implied probability and potential return.
- `/record`, `/oddshistory`, `/myslips` — prediction history, odds history and saved slips; require the optional database.
- `/calibrate` — run a date-ordered walk-forward evaluation using only matches before each test date; compare the current Poisson model, a venue-weighted/shrunk candidate and a training-only prevalence baseline on the final 30% holdout. Temperature calibration is reported separately. It requires at least 50 training and 30 holdout forecasts and never changes live model settings automatically.
- `/betlog HOME vs AWAY | MARKET | SELECTION | ODDS | STAKE [| BOOKMAKER | YYYY-MM-DD]` — preserve original odds and stake; the fixture date is resolved from the fixture catalogue where uniquely possible.
- `/betsettle BET_ID HOME_GOALS AWAY_GOALS` — settle a logged 1X2 or standard total-goals selection against the confirmed full-time score.
- `/betresults` — refresh completed match results and automatically settle pending logged bets only when home team, away team and match date match one unique result. A six-hour background job performs the same check when the database is enabled.\n- `/betstats` — report logged wins, losses, voids, profit/loss and ROI. This is separate from prediction accuracy.
- `/fullintel` — full command directory.

The Telegram menu also includes the SportyBet slip builder. AI building accepts a target odds figure and optional market/ranking controls. Manual building lets you request any market and outcome that SportyBet actually returns for the selected fixture; the AI forecast scanner only assigns probabilities to markets supported by its available evidence, and it reports unsupported markets rather than inventing estimates. BTTS and Under 2.5 are not hard-excluded. Match analysis accepts both team names and tolerates small spelling mistakes; use `TEAM A vs TEAM B`.

It accepts one selection per line in this format:

```text
Fixture | Market | Selection
Arsenal vs Chelsea | 1X2 | Arsenal
Liverpool vs Brighton | 1X2 | Draw
```

The bot must show the resolved current selections and obtain confirmation before requesting a booking/share code from SportyBet. It must never claim that a bet has been placed.

## Optional match-statistics provider\n\nSet `API_FOOTBALL_KEY` to enable the API-Sports / API-Football adapter in `football_stats_provider.py`. It requests the fixture by UTC date and exact home/away team names, then retrieves full-time statistics, half-time statistics, player statistics and match events. It refuses ambiguous fixture matches. API-Football documents corners, yellow/red cards and the `half=true` statistics option (available from the 2024 season); player-statistics coverage varies by competition. A valid API key and provider coverage are required. The free plan is limited to 100 requests/day; pricing and coverage must be checked before production use. The automatic settlement job now uses this provider as a fallback when the community results CSV cannot settle a bet or when the market needs non-score evidence. The evidence grader currently supports half-time 1X2, half-time total goals, standard full-time total corners, separate total yellow-card and red-card lines, and exact-name player goalscorer, goals, shots, shots-on-target, assists, yellow-card and red-card selections when the provider supplies the required fields. Player Over/Under selections must include an exact provider player name. Each evidence-based settlement stores its provider source and raw evidence JSON in the bet record. Missing provider data, integer-line push cases, bookmaker-specific card-points rules, ambiguous player names and unsupported Asian lines remain pending; they are never guessed. Provider use is opt-in through API_FOOTBALL_KEY and consumes API quota, so monitor the provider's current plan limits.\n\n## Data sources and bookmaker coverage

- Fixtures and competition catalogue: [open-matchday](https://github.com/oladeeayo/open-matchday). This is a community-maintained dataset, not a real-time live-score service.
- Bookmaker adapters: Betika Kenya and SportyBet Kenya. BetPawa is not configured. Betika odds use the public `/v1/uo/matches` list and `/v1/uo/match?parent_match_id=` detail requests; these endpoints are undocumented.
- SportyBet and Betika endpoints are not supported public API contracts. They may change, rate-limit or block requests. Malformed/incomplete feeds are surfaced as errors rather than silently treated as empty.

## Development and tests

Install dependencies, then run:

```bash
python -m py_compile bot.py database.py bookmaker_odds.py sportybet_client.py slip_manager.py
python -m unittest discover -s tests -v
```

GitHub Actions runs Python validation and unit tests on pushes and pull requests to `main`.
