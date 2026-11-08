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

PUNTER uses Google Gemini for natural-language request routing and football slip screenshot reading. Gemini interprets requests and extracts candidate selections; deterministic PUNTER tools remain responsible for fixture lookup, live bookmaker prices, probability calculations and validation. Screenshot selections must be reviewed and checked against current SportyBet Kenya markets before a booking code can be requested.

Configure these variables privately in Railway **Variables** (or your local environment):

- `GEMINI_API_KEY` — Google AI Studio API key. Never commit or share it.
- `GEMINI_MODEL` — optional model name; defaults to `gemini-2.5-flash`.

Without the key, existing non-AI commands continue to work. Gemini usage and quotas depend on the Google AI Studio account and current model availability.

## SportyClaw Premium capability programme

PUNTER's target is a complete, integrated research and betting-workflow assistant. The following capabilities are part of the product scope; a capability is not considered complete until its data source, implementation, tests and Telegram workflow have been verified.

- **Natural-language control:** understand requests, preferences, constraints and multi-step tasks; route them to deterministic bot functions.
- **Research desk:** fixture discovery, recent form, home/away splits, head-to-head, goals, team news where sourced, match context, risk and evidence citations.
- **Slip intelligence:** read screenshots, parse pasted slips, split/trim/combine/edit slips, remove or replace legs, change markets, validate each leg against live bookmaker markets and preview booking codes.
- **Edge hunter:** scan fixtures and available prices, estimate only model-supported probabilities, remove bookmaker margin where possible, compare fair prices and rank candidates with transparent uncertainty.
- **Specialist market models:** goals and scorelines; corners; cards; shots and shots on target; player props; half-time markets; handicaps and Asian lines. Each market needs its own suitable data and settlement logic. Unsupported markets remain unavailable until validated.
- **Bookmaker comparison:** compare available Betika and SportyBet prices, retain timestamps and identify price differences. Additional bookmakers require a lawful, stable and tested data source; no bookmaker coverage is implied until connected.
- **Analyst council:** separate evidence checks for form, goals, corners, cards, player data, odds and risk, followed by a final decision that exposes disagreements and missing evidence rather than fabricating consensus.
- **Performance laboratory:** persist forecasts and closing prices; report hit rate, Brier score, log loss, calibration, ROI, yield and closing-line value by market, league and time period. Use chronological out-of-sample testing.
- **Persistent user memory:** save explicit user preferences, prior forecasts, results and workflow state in the database, with controls to inspect and clear stored preferences.
- **Smart stake adviser:** configurable bankroll, conservative fractional-Kelly or flat-stake scenarios, exposure limits and risk warnings. Never promise returns or encourage chasing losses.
- **Rollover manager:** user-defined duration and daily combined-odds targets (including 2, 5, 10 and 20), with progress tracking and transparent risk warnings. It must not imply a rollover is safe or profitable.
- **Natural-language X/social post intake:** accept user-provided public post URLs or text where accessible, extract claims and verify them against independent sources. Do not treat social posts as confirmed team news.
- **User-first Telegram experience:** concise menus, ordinary-language controls, saved workflows and clear confirmations before any external booking-code request.

**Implementation status:** this programme is broader than the current AI-routing and screenshot-reading work. Live X ingestion, full specialist models, analyst council, persistent preference memory, complete slip editing, advanced bankroll/rollover tools, and broad bookmaker integrations must not be described as finished until implemented and tested. Probabilities remain experimental until supported by sufficient out-of-sample evidence.
 
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
- `/autobuild [TARGET] [MARKET FILTER] [rank=value|odds|probability] [legs=N|minlegs=N maxlegs=N]` — build a SportyBet preview with a configurable number of different matches. Targets of 10+ default to at least 5 legs (up to 20); smaller targets default to 2. Examples: `/autobuild 50` (at least 5 legs), `/autobuild 50 legs=8` (exactly 8 legs), `/autobuild 50 minlegs=5 maxlegs=12`, or `/autobuild 10 market=1X2 rank=value`. `/build` and `/slip` preserve these arguments too. Every leg shows the PUNTER statistical-model hit estimate and the slip shows the multiplied combined estimate. The combined figure assumes independence between matches and is experimental/uncalibrated, not a guarantee.
- `/calculator ODDS STAKE` — calculate implied probability and potential return.
- `/record`, `/oddshistory`, `/myslips` — prediction history, odds history and saved slips; require the optional database.
- `/calibrate` — run a date-ordered walk-forward evaluation using only matches before each test date; compare the current Poisson model, a venue-weighted/shrunk candidate and a training-only prevalence baseline on the final 30% holdout. Temperature calibration is reported separately. It requires at least 50 training and 30 holdout forecasts and never changes live model settings automatically.
- `/betlog HOME vs AWAY | MARKET | SELECTION | ODDS | STAKE [| BOOKMAKER | YYYY-MM-DD]` — preserve original odds and stake; the fixture date is resolved from the fixture catalogue where uniquely possible.
- `/betsettle BET_ID HOME_GOALS AWAY_GOALS` — settle a logged 1X2 or standard total-goals selection against the confirmed full-time score.
- `/betresults` — refresh completed match results and automatically settle pending logged bets only when home team, away team and match date match one unique result. A six-hour background job performs the same check when the database is enabled.\n- `/betstats` — report logged wins, losses, voids, profit/loss and ROI.
- `/performance` — alias for the settled-bet performance report.
- `/bankroll [AMOUNT]` — save or inspect your planning bankroll in KSh; requires the database.
- `/stake PROBABILITY ODDS` — capped 25% fractional-Kelly illustration using the saved bankroll; probability is supplied by the user and is not validated as a prediction.
- `/rollover DAYS DAILY_TARGET_ODDS` — save a rollover target and show the mathematical all-days-win projection; this is not a forecast or a recommendation.
- `/marketsim HOME_GOALS_RATE | AWAY_GOALS_RATE | MARKET | BOOKMAKER_ODDS` — transparent independent-Poisson scenario and price comparison using user-supplied goal rates. It is not a fixture prediction and reports truncated tail mass. This is separate from prediction accuracy.
- `/fullintel` — full command directory.

The Telegram menu also includes the SportyBet slip builder. AI building accepts a target odds figure and optional market/ranking controls. Manual building lets you request any market and outcome that SportyBet actually returns for the selected fixture; the AI forecast scanner only assigns probabilities to markets supported by its available evidence, and it reports unsupported markets rather than inventing estimates. BTTS and Under 2.5 are not hard-excluded. Match analysis accepts both team names and tolerates small spelling mistakes; use `TEAM A vs TEAM B`.

It accepts one selection per line in this format:

```text
Fixture | Market | Selection
Arsenal vs Chelsea | 1X2 | Arsenal
Liverpool vs Brighton | 1X2 | Draw
```

The bot must show the resolved current selections and obtain confirmation before requesting a booking/share code from SportyBet. It must never claim that a bet has been placed.

## Free multi-provider research sources

The automatic SportyBet scan can silently enrich exact fixture matches from these optional free football APIs. The providers supply research evidence only; SportyBet remains the source of the odds used to build a SportyBet preview.

- \`BSD_API_KEY\` — free Bzzoiro Sports Data football API key. Register at https://sports.bzzoiro.com/register/. The published free tier advertises 7,500 football requests/day, fixtures, lineups, statistics, xG, model forecasts and consensus odds across its covered competitions. The free tier does not expose named bookmaker prices or best-price comparison; those are paid features. See https://sports.bzzoiro.com/docs/football/ and https://sports.bzzoiro.com/pricing/.
- \`PITCHAPI_API_KEY\` — free PitchAPI key. Register at https://pitchapi.dev/ and copy the key from the account dashboard. Its documentation advertises a free football API with no request allowance, 77 leagues and historical data from 2021, including match stats, shots, player stats, lineups, events and advanced analytics. Coverage varies by league and match. See https://pitchapi.dev/.
- \`API_FOOTBALL_KEY\` — optional existing API-Sports / API-Football key. The published free tier is limited to 100 requests/day and is used by the existing specialist-statistics adapter. See https://www.api-football.com/pricing.

The new BSD and PitchAPI adapters match fixtures by both team names, home/away orientation and nearby UTC date. Ambiguous matches are rejected. BSD corner probabilities are applied only when the exact total-corners line exists in the provider forecast. Other external probabilities are retained as independent cross-checks and do not silently replace PUNTER's base model. Predicted lineups are never described as confirmed. The adapter caps enrichment at 20 matched fixtures per scan to protect free quotas and fails open if a provider is unavailable.

Use \`/sources\` in Telegram to see whether the three optional keys are configured. This checks environment-variable presence, not whether a key is valid. Add keys in Railway Variables; never paste them into Telegram, GitHub source code or this chat.

## Optional match-statistics provider\n\nSet `API_FOOTBALL_KEY` to enable the API-Sports / API-Football adapter in `football_stats_provider.py`. It requests the fixture by UTC date and exact home/away team names, then retrieves full-time statistics, half-time statistics, player statistics and match events. It refuses ambiguous fixture matches. API-Football documents corners, yellow/red cards and the `half=true` statistics option (available from the 2024 season); player-statistics coverage varies by competition. A valid API key and provider coverage are required. The free plan is limited to 100 requests/day; pricing and coverage must be checked before production use. The automatic settlement job now uses this provider as a fallback when the community results CSV cannot settle a bet or when the market needs non-score evidence. The evidence grader currently supports half-time 1X2, half-time total goals, standard full-time total corners, separate total yellow-card and red-card lines, and exact-name player goalscorer, goals, shots, shots-on-target, assists, yellow-card and red-card selections when the provider supplies the required fields. Player Over/Under selections must include an exact provider player name. Each evidence-based settlement stores its provider source and raw evidence JSON in the bet record. Missing provider data, integer-line push cases, bookmaker-specific card-points rules, ambiguous player names and unsupported Asian lines remain pending; they are never guessed. Provider use is opt-in through API_FOOTBALL_KEY and consumes API quota, so monitor the provider's current plan limits.\n\n## Premium research and tracking additions

- `/council HOME vs AWAY [YYYY-MM-DD]` — queries API-Football recent-match samples for corners, yellow cards, total shots, shots on target and first-half goals. Each panel reports its sample size and a transparent Poisson baseline. This is experimental and uncalibrated; it does not issue a consensus bet without an independently checked bookmaker price.
- Each available `/council` panel is saved to the forecast ledger. `/playerprop` also stores Over/Under forecasts. A scheduled grader settles team and player specialist forecasts from completed API-Football statistics; missing player appearances or missing stats remain unsettled. `/forecaststats` reports Brier score and log loss. `/forecastsettle FORECAST_ID WON|LOST` remains available for manually checked outcomes when automatic evidence is unavailable.\n- `/playerprop HOME vs AWAY | PLAYER | METRIC | LINE [| YYYY-MM-DD]` — estimates a named player's shots, shots on target, goals, assists or cards from their last five available appearances; refuses to forecast with fewer than four appearances or an ambiguous player match, stores the estimate and grades it automatically after the fixture when player statistics are available.
- `/xresearch HOME vs AWAY` — searches recent public X posts through the official X API v2. Configure `X_BEARER_TOKEN` privately. X API recent-search access may require a paid/eligible developer plan. Posts are unverified leads, not confirmed injury or lineup news.
- Automatic read-only odds snapshots run every 30 minutes for today's and tomorrow's fixtures while the database and Telegram job queue are running.
- `/clv` — compares logged settled selections with the latest exact matching captured price on the match date. It reports coverage and average closing-line value; missing snapshots are excluded, and CLV is a price-quality metric rather than proof of profitability.
- `/rolloverstatus` — inspect recorded progress. `/rolloverday WON|LOST|MISSED [ACTUAL_ODDS]` records a day's outcome. A loss or missed day breaks the sequence. Progress is user-entered and is not inferred from a win or forecast.
- Migration v5 adds rollover plan dates and daily progress storage. Existing odds history is stored in `punter_odds_snapshots`.

The accumulator builder already scans supported priced markets, ranks candidates and combines selections to a requested target. It remains an experimental builder: probability coverage is limited to markets for which the current fixture model can produce evidence, and the model has not been demonstrated to be calibrated or profitable. Specialist estimates are not yet validated on a sufficiently large chronological holdout. API quota, data coverage, exact fixture matching and bookmaker settlement rules remain constraints.

## Data sources and bookmaker coverage

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


### Operational commands added
- `/preferences`, `/preferences key=value ...`, and `/preferences clear` inspect, save and clear explicit non-secret user preferences.
- `/forecaststats` reports settled specialist forecast Brier score and log loss by market; `/forecastsettle ID WON|LOST` remains available for a manually verified outcome.
- Specialist council over/under forecasts are stored against their API-Football fixture IDs. A background job checks completed fixtures every six hours and settles supported totals only when final match statistics are present. Integer-line pushes and unsupported markets are left unsettled.
- Rollover status automatically marks prior unreported days as MISSED; the current day remains open until the user records its result.
- Automatic bookmaker snapshots cover today and tomorrow every 30 minutes while the bot is online. CLV uses the latest same-bookmaker matching quote captured before the recorded Nairobi kickoff time.
