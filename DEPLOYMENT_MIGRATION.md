# Deployment Migration Log

This file tracks changes made while moving Lucky Bingo toward a free-tier,
credit-card-free deployment. Each item below is a discrete, independently
useful step. Entries are added as each item is worked on — not all items are
planned or scheduled yet.

The original plan (discussed 2026-08-11) considered Koyeb for hosting and
Supabase for Postgres. On 2026-08-12 the user switched that plan to **Render**
(hosting) + **Neon** (Postgres) instead — Item 2 and Item 4 below reflect that
updated choice, not the original one.

## Item 3 — Merge the Telegram bot into the web process (webhook mode)

**Status:** Done (2026-08-11/12)

**Why:** The bot previously ran as its own long-polling process
(`python -m app.bot`), separate from the FastAPI web server. That meant two
processes to run, monitor, and deploy everywhere (locally, in `compose.yaml`,
on any future host). Free-tier hosts like Koyeb typically bill/limit per
service, so cutting the process count from two to one is valuable on its own,
independent of which host is eventually chosen. Webhook mode also removes the
constant `getUpdates` long-poll loop, which is wasted work once the app has a
public HTTPS URL anyway.

**What changed:**

- `app/bot.py`
  - Added `TelegramBot.set_webhook(url, secret_token)`.
  - Removed the `deleteWebhook` call from `TelegramBot.configure()` (webhook
    mode wants a webhook registered, not deleted).
  - Removed `TelegramBot.run()`, `main()`, and the old `asyncio.run(main())`
    entry point. `python -m app.bot` now exits immediately with a message
    pointing to `uvicorn app.main:app` instead.
  - Removed the now-unused `init_db` and `asyncio` imports.
  - Removed the module-level `logging.basicConfig()` call — this module is
    now imported into the FastAPI process, which owns logging setup.
  - All handler logic (`handle_message`, `handle_callback`, `notify_admins`,
    `register_user`, `handle_update`, `menu_keyboard`, `send_launcher`) is
    unchanged.
- `app/main.py`
  - Added `TelegramBot` import and a module-level `telegram_bot: TelegramBot | None`
    singleton (mirrors the existing `manager = GameManager()` pattern).
  - Added `notify_telegram(chat_id, text, reply_markup=None)`, a shared
    helper used to replace six duplicated `httpx.AsyncClient(...)` blocks
    that previously called the Telegram API directly from `main.py`.
  - Added `POST /telegram/webhook`, which validates the
    `X-Telegram-Bot-Api-Secret-Token` header with `hmac.compare_digest`
    against `settings.telegram_webhook_secret` before dispatching to
    `telegram_bot.handle_update()`. Always returns 200 (even on a handling
    error) so a single bad update cannot cause Telegram to retry-storm the
    webhook.
  - `lifespan()` now constructs `telegram_bot`, calls `.configure()`, and
    registers the webhook at `PUBLIC_URL/telegram/webhook` on startup if
    `PUBLIC_URL` is `https://`. Failures are logged as warnings, not raised —
    a Telegram API hiccup must not prevent the app itself from starting.
    The bot's `httpx` client is closed on shutdown.
  - Added `logging.basicConfig(...)` at module load, since `main.py` is now
    the actual process entry point that should own logging configuration.
- `app/config.py` — added `telegram_webhook_secret` (from the
  `TELEGRAM_WEBHOOK_SECRET` env var).
- `.env.example` — added a `TELEGRAM_WEBHOOK_SECRET` placeholder.
- `.env` — added a freshly generated real secret (not committed; local only).
- `start-testing.sh` — removed the separate bot process launch, its PID
  tracking, and its log file; the "web ready" check after the tunnel restart
  now also waits for `Telegram webhook registered` in `web.log`.
- `compose.yaml` — removed the separate `bot` service.
- `start.bat` — removed the second-window bot launch.
- `README.md` — updated the "Connect Telegram" section to a single-process
  startup, and documented the new `TELEGRAM_WEBHOOK_SECRET` requirement.
- `TESTING_WITH_CLOUDFLARE.md` — updated the process count, manual command
  list, and the "bot does not answer" troubleshooting entry for webhook-mode
  failure modes.

**Verification:**

- `pytest` — all 42 existing tests pass unchanged.
- `python -c "import app.main"` — the app module imports cleanly with no
  `NameError`/`ImportError`.
- Not verified by an actual run: registering a real webhook against the live
  `BOT_TOKEN`. That requires starting the server with a real `https://`
  `PUBLIC_URL` (e.g. via `./start-testing.sh`), which will make a real
  `setWebhook` call against Telegram's API — only do this by actually running
  the app, since it has a real side effect on the live bot's configuration.

## Item 4 — Migrate SQLite to Neon Postgres

**Status:** Done (2026-08-12)

**Why:** SQLite lives on local disk, which doesn't survive Render's free-tier
filesystem (ephemeral — wiped on every redeploy/restart) or work at all with
serverless-style hosts. Neon's free Postgres tier gives persistent storage
that both the local dev environment and a future Render deployment can share.
This was scoped as its own project (much larger than Item 3) because nearly
every function in `app/repository.py` talks to the database directly with
SQLite-specific SQL.

**What changed:**

- `sql/schema.sql` (new) — the full Postgres DDL, meant to be pasted into
  Neon's SQL editor once to create every table. Idempotent
  (`CREATE ... IF NOT EXISTS` throughout), safe to re-run.
- `app/db.py` — full rewrite:
  - `SCHEMA` now holds the same DDL as `sql/schema.sql` (Postgres-flavored,
    kept in sync by hand — the two are documented as needing to match if the
    schema ever changes again) instead of SQLite's `CREATE TABLE`.
  - SQLite's incremental migration machinery (the `MIGRATIONS` dict, the four
    `_migrate_*` "rebuild the table to widen a CHECK constraint" functions,
    the legacy-single-cartela-constraint check) is gone entirely — those
    existed only to evolve an already-deployed SQLite file in place. A fresh
    Postgres deployment just gets the final schema from day one, and
    widening a constraint in Postgres going forward is a plain
    `ALTER TABLE ... DROP/ADD CONSTRAINT`, no table rebuild needed.
  - `connect()`/`transaction()` now acquire connections from a
    `psycopg_pool.ConnectionPool` instead of opening a new SQLite file handle
    per call — opening a fresh TCP+TLS connection to Neon on every single
    repository call would add real latency and could exhaust Neon's
    connection limit under load.
  - The pool is keyed on `(database_url, db_schema)` and rebuilds itself if
    either changes; `db_schema` isn't a real production setting (defaults to
    `"public"` via `getattr(settings, "db_schema", None)`), it exists so
    tests can point at an isolated `pytest` schema on the *same* database
    without ever touching the app's real tables (see below).
  - `immediate=True` on `transaction()` is kept as a no-op, accepted only so
    every existing call site in `repository.py` didn't need editing. It
    mapped to SQLite's `BEGIN IMMEDIATE` (acquire the write lock up front,
    relying on SQLite's single-writer model to serialize conflicting writes).
    Postgres allows concurrent writers by default, so that guarantee doesn't
    carry over — see the `join_cards` note below for the one place that
    mattered.
- `app/repository.py` — full rewrite, mechanical for the most part:
  - `?` placeholders → `%s` (psycopg's style) everywhere.
  - `INSERT OR IGNORE` → `INSERT ... ON CONFLICT (columns) DO NOTHING`, using
    each table's actual unique constraint as the conflict target.
  - `cursor.lastrowid` → `RETURNING id` (or `RETURNING *` where the caller
    immediately re-fetched the full row anyway, saving a round trip to Neon).
  - `except sqlite3.IntegrityError` → `except psycopg.errors.UniqueViolation`.
  - SQLite's `date('now')` / `datetime('now', '-N days')` in `revenue_summary`
    → `CURRENT_DATE` / `NOW() - INTERVAL 'N days'`, comparing against
    `created_at::timestamptz` (the column is still stored as ISO-8601 TEXT,
    same as before — only the comparison side changed).
  - A few `SELECT COUNT(*)` queries read via `.fetchone()[0]` (positional)
    needed an explicit `AS alias` + `["alias"]` instead, since rows now come
    back as dicts (`psycopg.rows.dict_row`), not tuples.
  - `list_rooms()`/`get_room()`'s `GROUP BY r.id` needed the joined
    `u.first_name, u.telegram_id` columns added to the GROUP BY clause —
    SQLite silently allows selecting non-grouped columns from a GROUP BY
    query (picks an arbitrary matching row), Postgres requires them to be
    grouped or functionally dependent on the primary key. Same result, just
    stricter syntax.
  - **Real correctness fix, not just translation:** `users.telegram_id` is
    now `BIGINT`, not `INTEGER`. SQLite's dynamic typing let a 4-byte
    `INTEGER` column silently hold any integer regardless of the declared
    type, but Postgres `INTEGER` really is capped at ~2.1 billion — and one
    of the real admin Telegram IDs already in `.env`
    (`6092491792`) is bigger than that. This would have hit a real overflow
    error on first login under the old direct translation.
  - **Real behavior change, not just translation:** `join_cards()`'s
    per-card `INSERT` is now wrapped in `try/except psycopg.errors.UniqueViolation`,
    translating a race into the existing `ConflictError("Cartela #N has
    already been taken")` instead of an unhandled 500. Under SQLite's
    `BEGIN IMMEDIATE`, two players racing to claim the same cartela number
    could never actually collide at the INSERT — the second transaction
    blocked until the first committed, then cleanly saw the number as taken
    during its own availability check. Postgres allows both transactions to
    reach the INSERT concurrently, so the unique index on
    `(room_id, card_number)` is now the real guard, and the code needs to
    catch the violation it can raise. No other write path in `repository.py`
    has an equivalent unprotected read-then-insert race (everything else
    either has no uniqueness constraint to violate, or route through
    `ON CONFLICT DO NOTHING`, which doesn't raise at all).
- `pyproject.toml` — added `psycopg[binary,pool]`.
- `app/config.py` — `database_path` replaced with `database_url` (from the
  `DATABASE_URL` env var).
- `.env.example` / `.env` — `DATABASE_PATH` replaced with `DATABASE_URL`.
- `Dockerfile` — removed `RUN mkdir -p /app/data` (nothing writes there
  anymore).
- `compose.yaml` — removed the `bingo-data` volume (Postgres persistence now
  lives in Neon, not a local Docker volume).
- `README.md` — "Run locally" now explains a Postgres database (e.g. a free
  Neon project) is required, since there's no more zero-config local SQLite
  file; "Architecture and scaling" updated to describe Postgres instead of a
  future PostgreSQL migration.
- Test suite (`tests/conftest.py`, `tests/test_api.py`, `tests/test_money.py`):
  - Deleted `tests/test_db_migrations.py` outright — it only tested the
    SQLite incremental-migration machinery that no longer exists.
  - Every test's raw SQL (used to seed deterministic draws for game-logic
    tests) got the same `?` → `%s` treatment.
  - The old "fresh SQLite file per test" isolation (`db.settings = SimpleNamespace(database_path=...)`
    pointing at a `tempfile.TemporaryDirectory()`) is replaced by pointing at
    an isolated `pytest` Postgres *schema* on the same real Neon database via
    a new `db_schema` field on the test settings object, then `TRUNCATE
    ... RESTART IDENTITY CASCADE`-ing all tables before each test. This never
    touches the app's real `public` schema, and `RESTART IDENTITY` still
    gives every test the same "IDs start from 1" guarantee the old
    per-test SQLite file provided. Recreating a whole schema per test
    (mirroring "a fresh file per test" more literally) was ruled out as
    unnecessarily slow over the network to Neon; truncating a shared schema
    is fast and sufficient.

**Verification:**

- A throwaway `migration_smoketest` schema (never `public`) was used to
  exercise `upsert_user`'s `ON CONFLICT`, the `BIGINT` telegram_id fix,
  `create_room`/`submit_deposit`/`submit_withdrawal`/`submit_transfer`'s
  `RETURNING`, the `join_cards` unique-violation safety net, a full
  join→draw→mark→claim→settle round, `revenue_summary`'s date arithmetic,
  and `paginate_evidence_rounds`/`leaderboard`/`list_rooms` — all passed, then
  the schema was dropped.
- `pytest` — all 41 remaining tests (42 minus the deleted SQLite-migration
  test) pass against the real Neon database, isolated in a `pytest` schema.
  This took **~10.5 minutes**, versus a few seconds for the old local-SQLite
  suite — real network round-trips per query add up across ~40 tests each
  doing dozens of queries. Worth knowing before running the suite routinely
  during day-to-day development.
- Not yet done: Item 2 below (Render) — the app hasn't actually been deployed
  and run against Neon from outside this local environment yet.

## Item 2 — Render hosting

**Status:** Not started. `sql/schema.sql` has been handed to the user to run
in Neon's SQL editor; once that's done and Item 4 above is confirmed working
locally, the next step is deploying `app/main.py` to a Render free web
service with `DATABASE_URL`, `BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, and the
rest of `.env`'s variables set there instead. Known open question, not yet
verified: Render's free tier spins a web service down after ~15 minutes with
no inbound traffic, which could orphan a live round's background draw-loop
and result-resolution timers if it happens mid-game — needs to be
deliberately tested with an active game before `ENABLE_REAL_MONEY=true` ever
runs on this stack.

## Item 1 — Cloudflare Pages for the frontend

**Status:** Not agreed to yet; deprioritized in the initial evaluation since
the current single-origin FastAPI setup already serves the frontend for free.
