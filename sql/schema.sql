-- Lucky Bingo — Postgres schema (Neon)
--
-- Paste this whole file into the Neon SQL editor and run it once to create
-- every table Lucky needs. It is idempotent (IF NOT EXISTS everywhere), so
-- running it again later is harmless.
--
-- The app also creates these same tables itself on startup (app/db.py's
-- init_db(), executed from the identical schema baked into that file) as a
-- safety net for local/dev use — the two must be kept in sync if the schema
-- ever changes. Running this file yourself first means the app's own startup
-- check has nothing left to do.

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT NOT NULL,
    last_name TEXT,
    photo_url TEXT,
    language_code TEXT,
    referred_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'waiting'
        CHECK(state IN ('waiting', 'running', 'finished', 'cancelled')),
    max_players INTEGER NOT NULL DEFAULT 100,
    auto_start_min_players INTEGER NOT NULL DEFAULT 1,
    auto_start_at TEXT,
    call_interval_seconds REAL NOT NULL DEFAULT 3,
    scheduled_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    winner_user_id INTEGER REFERENCES users(id),
    stake_santim INTEGER NOT NULL DEFAULT 0,
    commission_bps INTEGER NOT NULL DEFAULT 500,
    transfer_cost_santim INTEGER NOT NULL DEFAULT 0,
    card_capacity INTEGER NOT NULL DEFAULT 400,
    winning_sequence INTEGER,
    grace_deadline_sequence INTEGER,
    result_status TEXT NOT NULL DEFAULT 'open',
    result_deadline_at TEXT,
    result_detected_at TEXT,
    final_called_number INTEGER,
    disputed_at TEXT,
    outcome TEXT CHECK(outcome IN ('winner', 'dismissed', 'no_winner')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id SERIAL PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    numbers_json TEXT NOT NULL,
    marks_json TEXT NOT NULL DEFAULT '[0]',
    auto_mark INTEGER NOT NULL DEFAULT 0,
    blocked INTEGER NOT NULL DEFAULT 0,
    card_number INTEGER,
    entry_cost_santim INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draws (
    id SERIAL PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 75),
    sequence INTEGER NOT NULL,
    called_at TEXT NOT NULL,
    UNIQUE(room_id, number),
    UNIQUE(room_id, sequence)
);

CREATE TABLE IF NOT EXISTS claims (
    id SERIAL PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    accepted INTEGER NOT NULL,
    claimed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_entries (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    amount_santim INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('deposit', 'entry', 'payout', 'refund', 'adjustment', 'bonus', 'transfer_out', 'transfer_in')),
    reference_type TEXT NOT NULL,
    reference_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    created_by_user_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE(user_id, kind, reference_type, reference_id)
);

CREATE TABLE IF NOT EXISTS deposits (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL CHECK(provider IN ('telebirr', 'cbe', 'cbe_account', 'manual')),
    amount_santim INTEGER NOT NULL CHECK(amount_santim > 0),
    transaction_id TEXT NOT NULL,
    transaction_id_normalized TEXT NOT NULL,
    receipt_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'approved', 'rejected')),
    reviewed_by_user_id INTEGER REFERENCES users(id),
    review_note TEXT,
    submitted_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE(provider, transaction_id_normalized)
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL CHECK(provider IN ('telebirr', 'cbe', 'cbe_account')),
    amount_santim INTEGER NOT NULL CHECK(amount_santim > 0),
    account_number TEXT NOT NULL,
    account_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'approved', 'rejected')),
    reviewed_by_user_id INTEGER REFERENCES users(id),
    review_note TEXT,
    payout_reference TEXT,
    payout_reference_normalized TEXT,
    submitted_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS settlements (
    id SERIAL PRIMARY KEY,
    room_id INTEGER NOT NULL UNIQUE REFERENCES rooms(id) ON DELETE RESTRICT,
    winner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    gross_pool_santim INTEGER NOT NULL,
    commission_santim INTEGER NOT NULL,
    transfer_cost_santim INTEGER NOT NULL,
    payout_santim INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS round_winners (
    id SERIAL PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE RESTRICT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    winning_sequence INTEGER NOT NULL,
    payout_santim INTEGER NOT NULL DEFAULT 0,
    detected_at TEXT NOT NULL,
    UNIQUE(room_id, card_id)
);

CREATE TABLE IF NOT EXISTS round_evidence (
    id SERIAL PRIMARY KEY,
    room_id INTEGER NOT NULL UNIQUE REFERENCES rooms(id) ON DELETE RESTRICT,
    final_called_number INTEGER NOT NULL,
    winning_sequence INTEGER NOT NULL,
    draws_json TEXT NOT NULL,
    winners_json TEXT NOT NULL,
    players_json TEXT NOT NULL,
    room_created_at TEXT,
    room_started_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS round_disputes (
    id SERIAL PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE RESTRICT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(room_id, user_id)
);

CREATE TABLE IF NOT EXISTS transfers (
    id SERIAL PRIMARY KEY,
    sender_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    amount_santim INTEGER NOT NULL CHECK(amount_santim > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'approved', 'rejected')),
    reviewed_by_user_id INTEGER REFERENCES users(id),
    review_note TEXT,
    submitted_at TEXT NOT NULL,
    reviewed_at TEXT,
    CHECK(sender_user_id != recipient_user_id)
);

CREATE INDEX IF NOT EXISTS idx_cards_room ON cards(room_id);
CREATE INDEX IF NOT EXISTS idx_draws_room_sequence ON draws(room_id, sequence);
CREATE INDEX IF NOT EXISTS idx_claims_user ON claims(user_id, accepted);
CREATE INDEX IF NOT EXISTS idx_wallet_user_created ON wallet_entries(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status, submitted_at);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status, submitted_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_withdrawal_payout_reference
    ON withdrawals(payout_reference_normalized)
    WHERE payout_reference_normalized IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_deposit_txid_global
    ON deposits(transaction_id_normalized);
CREATE INDEX IF NOT EXISTS idx_round_winners_room ON round_winners(room_id);
CREATE INDEX IF NOT EXISTS idx_round_disputes_room ON round_disputes(room_id, created_at);
CREATE INDEX IF NOT EXISTS idx_transfers_status ON transfers(status, submitted_at);
CREATE INDEX IF NOT EXISTS idx_transfers_sender ON transfers(sender_user_id, submitted_at);
CREATE INDEX IF NOT EXISTS idx_transfers_recipient ON transfers(recipient_user_id, submitted_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cards_room_number
    ON cards(room_id, card_number) WHERE card_number IS NOT NULL;
