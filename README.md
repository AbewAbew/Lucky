# Lucky Bingo

A 75-ball Bingo game designed as a Telegram bot plus Mini App. Telegram registers players, the bot handles wallet and deposit commands, and the Mini App provides the live lobby, visual cartela selection, financial breakdown, and game interface.

## Included in this MVP

- Telegram Mini App authentication with server-side HMAC verification
- Local demo authentication for development
- Automatic player registration and referral attribution
- 2, 5, and 10 birr categories with 400 selectable cartelas per round
- Visual 1–400 picker with full cartela preview before confirmation
- Up to five cartelas per player, displayed together throughout the live game
- Each tier auto-starts on a visible 20-second countdown only after five distinct Telegram players have joined; extra cartelas from one player do not satisfy the minimum
- Exact santim-based wallet and settlement calculations
- Gross pool − 5% commission − configured transfer cost = winner payout
- Manual Telebirr/CBE Birr deposit requests with unique transaction IDs
- Telebirr/CBE withdrawal requests with reserved balances, a 100 birr minimum, admin review, and unique payout references
- Atomic approval by one of three authorized Telegram administrators
- Permanent wallet, deposit, settlement, and approval audit records
- Public rooms and invite links
- Unique, position-shuffled 5×5 cards with valid B/I/N/G/O ranges and a free centre
- Server-authoritative random draws over WebSockets
- Manual marking and optional auto-marking
- Atomic, server-verified row, column, diagonal, and four-corners Bingo claims
- Cartela-specific BINGO buttons; an incorrect claim blocks only that cartela for the round
- Same-called-number winners share the net payout equally
- Immediate number-call stop and a 15-second result review after the first winning call
- Automatic round dismissal and full entry refunds when more than four cards win on the same call
- Persistent game history and leaderboard
- Bot commands and a native Mini App launcher
- Protected live administrator board with called-number and 1–400 sold-cartela marking, game control, deposit queue, and withdrawal queue
- Docker deployment with Postgres persistence (Neon-compatible)

Real-money debits and payouts are disabled by default. Do not set `ENABLE_REAL_MONEY=true` before completing licensing, age-verification, payment-provider, Telegram-platform, and jurisdiction reviews for every market served.

## Run locally

Python 3.12 is required, along with a Postgres database — a free [Neon](https://neon.tech) project works well. Create one, paste its connection string into `DATABASE_URL` in `.env` (see `.env.example`), and run `sql/schema.sql` in Neon's SQL editor to create the tables (the app also creates them itself on startup if you skip this step, but running it yourself first is faster). Unlike the SQLite-based versions of this project, there is no zero-config local database file anymore.

On Windows, double-click `start.bat`. It creates the virtual environment, installs
missing dependencies, creates `.env` on first launch, starts the server, and opens
the app in your browser.

For the complete Telegram testing setup, including the temporary HTTPS tunnel,
follow [TESTING_WITH_CLOUDFLARE.md](TESTING_WITH_CLOUDFLARE.md). Before adding
the third administrator or enabling real-money transactions, follow the
production checklist in [NEXT_STEPS.md](NEXT_STEPS.md).

For WSL or Linux Telegram testing, start the web server (which also runs the
Telegram bot) and a temporary HTTPS tunnel together with one command:

```bash
./start-testing.sh
```

The launcher installs a project-local Cloudflare binary when needed, updates the
temporary `PUBLIC_URL`, and stops both processes when you press `Ctrl+C`.
See [TESTING_WITH_CLOUDFLARE.md](TESTING_WITH_CLOUDFLARE.md) for troubleshooting.

For local browser-only development on macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
set -a
source .env
set +a
uvicorn app.main:app --reload
```

Open <http://localhost:8000>. Because `ALLOW_DEV_AUTH=true`, the browser registers local Telegram ID `999000`, which is also the first demo administrator. Open <http://localhost:8000/admin> for the protected control board. Each tier starts a 20-second countdown after its fifth distinct player joins, then calls numbers automatically.

Run the test suite with:

```bash
pytest
```

## Connect Telegram

1. Open `@BotFather` in Telegram and create a bot with `/newbot`.
2. Set the bot username and token in `.env`.
3. Set `TELEGRAM_WEBHOOK_SECRET` in `.env` to a long random value (see `.env.example`).
4. Deploy this project at a public HTTPS URL, then set `PUBLIC_URL` to that origin.
5. In BotFather, open **Bot Settings → Configure Mini App**, enable the Main Mini App, and enter the public URL.
6. In production, set `ALLOW_DEV_AUTH=false` and replace both `APP_SECRET` and `ADMIN_KEY` with long random secrets.
7. Put exactly three numeric Telegram user IDs in `ADMIN_TELEGRAM_IDS`.
8. Add the Telebirr/CBE Birr destination accounts and account holder name.
9. Start the single process:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The Telegram bot runs inside this same process and registers its webhook at
`PUBLIC_URL/telegram/webhook` automatically on startup — there is no separate
bot process to run. Telegram still requires the Mini App itself to be hosted
over HTTPS. Each administrator can use `/myid` to obtain the numeric ID
required by `ADMIN_TELEGRAM_IDS`. Administrators can use `/admin`; players can
use `/deposit`, `/pay`, `/balance`, and `/transactions`.

## Financial configuration

The relevant `.env` settings are:

```dotenv
ENABLE_REAL_MONEY=false
ADMIN_TELEGRAM_IDS=111111111,222222222,333333333
TELEBIRR_ACCOUNT=09XXXXXXXX
CBE_BIRR_ACCOUNT=1000XXXXXXXXX
PAYMENT_ACCOUNT_NAME=Lucky Bingo
MINIMUM_DEPOSIT_BIRR=10
MINIMUM_WITHDRAWAL_BIRR=100
DEFAULT_TRANSFER_COST_BIRR=0
AUTO_START_DELAY_SECONDS=20
```

`DEFAULT_TRANSFER_COST_BIRR` is stored on each newly created round. Set the actual operator-approved transfer fee before opening a round. Deposits below `MINIMUM_DEPOSIT_BIRR` are rejected by both the bot and Mini App. Withdrawal requests below `MINIMUM_WITHDRAWAL_BIRR` are rejected, and pending requests reserve the requested amount so it cannot also be spent on cartelas. Admin approval requires a unique bank payout reference; rejection releases the reservation. Deposit references are normalized and globally unique; this prevents reuse but does not prove payment authenticity. An administrator must verify references in the actual banking application.

## Winner settlement rules

The server evaluates every sold card after every called number, so a slow player connection cannot erase a valid Bingo. Calls stop immediately on the first winning call. All cards that complete a valid pattern on that exact call are one winning group, and the result remains pending for 15 seconds for review. The net payout is split between one to four winning cartelas. Any indivisible santim remainder is assigned deterministically by ascending card number. If five or more cartelas win on that call, the round is dismissed, all entries are refunded, and no commission or transfer cost is charged.

Alternatively, use Docker:

```bash
cp .env.example .env
docker compose up --build
```

## Operator API

Interactive API documentation is available at `/docs`. Admin operations require the `X-Admin-Key` header.

Create a room:

```bash
curl -X POST http://localhost:8000/api/admin/rooms \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Key: change-me-admin-key' \
  -d '{"name":"Lucky 5 Birr Special","max_players":400,"auto_start_min_players":5,"call_interval_seconds":5,"stake_santim":500,"transfer_cost_santim":0}'
```

Start a room manually:

```bash
curl -X POST http://localhost:8000/api/admin/rooms/2/start \
  -H 'X-Admin-Key: change-me-admin-key'
```

The three standard tier rooms are created automatically and are operator-started. Money is always sent through the API as integer santim.

## Architecture and scaling

Postgres (Neon) holds persistence and the in-process event manager keeps the first deployment easy to operate. Run exactly one web worker with this configuration — the connection pool and game-engine background tasks in `app/main.py` are per-process, so a second worker would run its own independent copy of both. Before horizontal scaling, move event distribution and locks to Redis and game scheduling to a dedicated worker. The server—not the browser—must remain the source of truth for draws, marks, and claims.

Important production additions include automated database backups, structured audit logs, monitoring, rate limiting, translated copy, privacy/terms pages, and a full operator dashboard.
