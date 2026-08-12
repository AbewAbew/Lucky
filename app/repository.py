from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from app.auth import TelegramUser
from app.config import settings
from app.db import connect, transaction, utc_now
from app.game import FREE_SPACE, generate_card, has_bingo


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


TIERS_SANTIM = (200, 500, 1_000)
COMMISSION_BPS = 500
MAX_CARDS_PER_PLAYER = 5
TIER_AUTO_START_MIN_CARDS = 5

PROVIDER_LABELS = {
    "telebirr": "Telebirr",
    "cbe": "CBE Birr",
    "cbe_account": "CBE Bank Account",
    "manual": "Manual",
}


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider.upper())


def start_requirement_met(room: dict[str, Any]) -> bool:
    if getattr(settings, "test_single_player_start", False):
        return int(room["unique_player_count"] or 0) >= 1 and int(
            room["player_count"] or 0
        ) >= MAX_CARDS_PER_PLAYER
    threshold = int(room["auto_start_min_players"] or 0)
    return threshold == 0 or int(room["unique_player_count"] or 0) >= threshold


def _row_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def calculate_prize(
    gross_pool_santim: int,
    commission_bps: int = COMMISSION_BPS,
    transfer_cost_santim: int = 0,
) -> dict[str, int]:
    commission = gross_pool_santim * commission_bps // 10_000
    transfer = min(max(0, transfer_cost_santim), max(0, gross_pool_santim - commission))
    return {
        "gross_pool_santim": gross_pool_santim,
        "commission_santim": commission,
        "transfer_cost_santim": transfer,
        "winner_payout_santim": max(0, gross_pool_santim - commission - transfer),
    }


def _decorate_room(room: dict[str, Any]) -> dict[str, Any]:
    room["result_status"] = room.get("result_status") or "open"
    room.update(
        calculate_prize(
            int(room.get("gross_pool_santim") or 0),
            int(room.get("commission_bps") or COMMISSION_BPS),
            int(room.get("transfer_cost_santim") or 0),
        )
    )
    if room.get("outcome") == "dismissed":
        room["commission_santim"] = 0
        room["transfer_cost_santim"] = 0
        room["winner_payout_santim"] = 0
        room["refund_santim"] = room["gross_pool_santim"]
    else:
        room["refund_santim"] = 0
    room["stake_birr"] = room["stake_santim"] / 100
    room["available_cards"] = max(0, room["card_capacity"] - room["player_count"])
    room["test_single_player_start"] = bool(
        getattr(settings, "test_single_player_start", False)
    )
    return room


def upsert_user(
    user: TelegramUser, referred_by_telegram_id: int | None = None
) -> dict[str, Any]:
    now = utc_now()
    with transaction(immediate=True) as connection:
        referrer_id = None
        if referred_by_telegram_id and referred_by_telegram_id != user.telegram_id:
            referrer = connection.execute(
                "SELECT id FROM users WHERE telegram_id = %s", (referred_by_telegram_id,)
            ).fetchone()
            referrer_id = referrer["id"] if referrer else None
        is_new_user = (
            connection.execute(
                "SELECT 1 FROM users WHERE telegram_id = %s", (user.telegram_id,)
            ).fetchone()
            is None
        )
        connection.execute(
            """
            INSERT INTO users (
                telegram_id, username, first_name, last_name, photo_url,
                language_code, referred_by, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                photo_url = excluded.photo_url,
                language_code = excluded.language_code,
                updated_at = excluded.updated_at
            """,
            (
                user.telegram_id,
                user.username,
                user.first_name,
                user.last_name,
                user.photo_url,
                user.language_code,
                referrer_id,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM users WHERE telegram_id = %s", (user.telegram_id,)
        ).fetchone()
        bonus_granted_santim = 0
        if is_new_user and settings.signup_bonus_santim > 0:
            connection.execute(
                """
                INSERT INTO wallet_entries (
                    user_id, amount_santim, kind, reference_type,
                    reference_id, description, created_at
                ) VALUES (%s, %s, 'bonus', 'signup', %s, %s, %s)
                ON CONFLICT (user_id, kind, reference_type, reference_id) DO NOTHING
                """,
                (
                    row["id"],
                    settings.signup_bonus_santim,
                    row["id"],
                    f"Welcome bonus ({settings.signup_bonus_santim / 100:g} birr, "
                    "not withdrawable)",
                    now,
                ),
            )
            bonus_granted_santim = settings.signup_bonus_santim
    result = dict(row)
    result["signup_bonus_granted_santim"] = bonus_granted_santim
    return result


def get_user_by_telegram_id(telegram_id: int) -> dict[str, Any] | None:
    with connect() as connection:
        return _row_dict(
            connection.execute(
                "SELECT * FROM users WHERE telegram_id = %s", (telegram_id,)
            ).fetchone()
        )


def ensure_tier_rooms() -> None:
    with transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE rooms SET state = 'cancelled'
            WHERE stake_santim = 0 AND state IN ('waiting', 'running')
              AND NOT EXISTS (SELECT 1 FROM cards WHERE cards.room_id = rooms.id)
            """
        )
        connection.execute(
            """
            UPDATE rooms SET auto_start_min_players = %s
            WHERE stake_santim IN (200, 500, 1000)
              AND state IN ('waiting', 'running')
            """,
            (TIER_AUTO_START_MIN_CARDS,),
        )
        existing_stakes = {
            row["stake_santim"]
            for row in connection.execute(
                "SELECT stake_santim FROM rooms WHERE state IN ('waiting', 'running')"
            ).fetchall()
        }
        for stake_santim in TIERS_SANTIM:
            if stake_santim in existing_stakes:
                continue
            connection.execute(
                """
                INSERT INTO rooms (
                    name, state, max_players, auto_start_min_players,
                    call_interval_seconds, stake_santim, commission_bps,
                    transfer_cost_santim, card_capacity, created_at
                ) VALUES (%s, 'waiting', 400, %s, %s, %s, %s, %s, 400, %s)
                """,
                (
                    f"Lucky {stake_santim // 100} Birr",
                    TIER_AUTO_START_MIN_CARDS,
                    settings.call_interval_seconds,
                    stake_santim,
                    COMMISSION_BPS,
                    settings.default_transfer_cost_santim,
                    utc_now(),
                ),
            )


def ensure_quick_room() -> None:
    """Backward-compatible entry point used by the application lifecycle."""
    ensure_tier_rooms()


def create_room(
    name: str,
    *,
    max_players: int = 100,
    auto_start_min_players: int = 1,
    call_interval_seconds: float | None = None,
    stake_santim: int = 200,
    transfer_cost_santim: int | None = None,
    card_capacity: int = 400,
) -> dict[str, Any]:
    if not name.strip():
        raise ValueError("Room name is required")
    with transaction(immediate=True) as connection:
        cursor = connection.execute(
            """
            INSERT INTO rooms (
                name, max_players, auto_start_min_players,
                call_interval_seconds, stake_santim, commission_bps,
                transfer_cost_santim, card_capacity, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                name.strip(),
                max(2, min(max_players, card_capacity)),
                max(0, auto_start_min_players),
                call_interval_seconds or settings.call_interval_seconds,
                stake_santim,
                COMMISSION_BPS,
                settings.default_transfer_cost_santim
                if transfer_cost_santim is None
                else max(0, transfer_cost_santim),
                max(1, min(card_capacity, 400)),
                utc_now(),
            ),
        )
        row = cursor.fetchone()
    return dict(row)


def list_rooms() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT r.*, COUNT(c.id) AS player_count,
                   COUNT(DISTINCT c.user_id) AS unique_player_count,
                   COALESCE(SUM(c.entry_cost_santim), 0) AS gross_pool_santim,
                   (SELECT COUNT(*) FROM round_winners rw WHERE rw.room_id = r.id) AS winner_count,
                   u.first_name AS winner_name,
                   u.telegram_id AS winner_telegram_id
            FROM rooms r
            LEFT JOIN cards c ON c.room_id = r.id
            LEFT JOIN users u ON u.id = r.winner_user_id
            GROUP BY r.id, u.first_name, u.telegram_id
            ORDER BY CASE r.state
                WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,
                CASE WHEN r.state IN ('waiting', 'running') THEN r.stake_santim END ASC,
                r.id DESC
            LIMIT 30
            """
        ).fetchall()
    return [_decorate_room(dict(row)) for row in rows]


def get_room(room_id: int) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT r.*, COUNT(c.id) AS player_count,
                   COUNT(DISTINCT c.user_id) AS unique_player_count,
                   COALESCE(SUM(c.entry_cost_santim), 0) AS gross_pool_santim,
                   (SELECT COUNT(*) FROM round_winners rw WHERE rw.room_id = r.id) AS winner_count,
                   u.first_name AS winner_name,
                   u.telegram_id AS winner_telegram_id
            FROM rooms r
            LEFT JOIN cards c ON c.room_id = r.id
            LEFT JOIN users u ON u.id = r.winner_user_id
            WHERE r.id = %s GROUP BY r.id, u.first_name, u.telegram_id
            """,
            (room_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError("Room not found")
    return _decorate_room(dict(row))


def _ledger_balance_in(connection: psycopg.Connection, user_id: int) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(SUM(amount_santim), 0) AS balance FROM wallet_entries WHERE user_id = %s",
            (user_id,),
        ).fetchone()["balance"]
    )


def _reserved_withdrawals_in(connection: psycopg.Connection, user_id: int) -> int:
    return int(
        connection.execute(
            """
            SELECT COALESCE(SUM(amount_santim), 0) AS reserved
            FROM withdrawals WHERE user_id = %s AND status = 'pending'
            """,
            (user_id,),
        ).fetchone()["reserved"]
    )


def _reserved_transfers_in(connection: psycopg.Connection, user_id: int) -> int:
    return int(
        connection.execute(
            """
            SELECT COALESCE(SUM(amount_santim), 0) AS reserved
            FROM transfers WHERE sender_user_id = %s AND status = 'pending'
            """,
            (user_id,),
        ).fetchone()["reserved"]
    )


def _wallet_balance_in(connection: psycopg.Connection, user_id: int) -> int:
    return (
        _ledger_balance_in(connection, user_id)
        - _reserved_withdrawals_in(connection, user_id)
        - _reserved_transfers_in(connection, user_id)
    )


def _bonus_total_in(connection: psycopg.Connection, user_id: int) -> int:
    return int(
        connection.execute(
            """
            SELECT COALESCE(SUM(amount_santim), 0) AS bonus
            FROM wallet_entries WHERE user_id = %s AND kind = 'bonus'
            """,
            (user_id,),
        ).fetchone()["bonus"]
    )


def _withdrawable_balance_in(connection: psycopg.Connection, user_id: int) -> int:
    """Spendable balance minus any never-withdrawable bonus money still in it.

    Bonus santim is excluded permanently, not just until it is "wagered": once
    spent it stops reducing this figure (nothing left to exclude), and any
    winnings on top of it count fully, matching a simple non-withdrawable
    promotional credit rather than a playthrough/wagering requirement.
    """
    return max(
        0, _wallet_balance_in(connection, user_id) - _bonus_total_in(connection, user_id)
    )


def _wallet_breakdown_in(connection: psycopg.Connection, user_id: int) -> dict[str, int]:
    """Current-moment balance snapshot for admin review screens."""
    balance = _wallet_balance_in(connection, user_id)
    bonus = _bonus_total_in(connection, user_id)
    return {
        "balance_santim": balance,
        "bonus_santim": bonus,
        "withdrawable_balance_santim": max(0, balance - bonus),
    }


def wallet_balance(user_id: int) -> int:
    with connect() as connection:
        return _wallet_balance_in(connection, user_id)


def _numbers_for_card(room_id: int, card_number: int) -> list[list[int]]:
    seed = hashlib.sha256(
        f"Lucky:{settings.app_secret}:{room_id}:{card_number}".encode()
    ).digest()
    return generate_card(seed)


def join_cards(
    room_id: int,
    user_id: int,
    card_numbers: list[int] | None = None,
) -> list[dict[str, Any]]:
    with transaction(immediate=True) as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if room is None:
            raise NotFoundError("Room not found")
        if room["state"] not in {"waiting", "running"}:
            raise ConflictError("This game is no longer accepting players")

        existing = connection.execute(
            """
            SELECT * FROM cards WHERE room_id = %s AND user_id = %s
            ORDER BY card_number
            """,
            (room_id, user_id),
        ).fetchall()
        existing_numbers = {row["card_number"] for row in existing}
        used_numbers = {
            row["card_number"]
            for row in connection.execute(
                "SELECT card_number FROM cards WHERE room_id = %s AND card_number IS NOT NULL",
                (room_id,),
            ).fetchall()
        }
        capacity = min(room["max_players"], room["card_capacity"])
        requested = list(dict.fromkeys(card_numbers or []))
        if not requested:
            if existing:
                return [_serialize_card(row) for row in existing]
            available = [
                number for number in range(1, capacity + 1) if number not in used_numbers
            ]
            if not available:
                raise ConflictError("This room is full")
            requested = [secrets.choice(available)]

        if any(not 1 <= number <= capacity for number in requested):
            raise ConflictError(f"Choose cartela numbers from 1 to {capacity}")
        new_numbers = [number for number in requested if number not in existing_numbers]
        if len(existing) + len(new_numbers) > MAX_CARDS_PER_PLAYER:
            raise ConflictError(
                f"You can choose up to {MAX_CARDS_PER_PLAYER} cartelas per game"
            )
        taken = [number for number in new_numbers if number in used_numbers]
        if taken:
            raise ConflictError(f"Cartela #{taken[0]} has already been taken")
        if not new_numbers:
            return [_serialize_card(row) for row in existing]
        if room["state"] == "running":
            raise ConflictError("This game has already started")
        if len(used_numbers) + len(new_numbers) > capacity:
            raise ConflictError("This room does not have enough available cartelas")

        stake_santim = int(room["stake_santim"])
        if settings.enable_real_money:
            total_cost = stake_santim * len(new_numbers)
            if _wallet_balance_in(connection, user_id) < total_cost:
                raise ConflictError(
                    f"You need {total_cost / 100:.2f} birr for these cartelas"
                )

        for card_number in new_numbers:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO cards (
                        room_id, user_id, numbers_json, marks_json, card_number,
                        entry_cost_santim, created_at
                    ) VALUES (%s, %s, %s, '[0]', %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        room_id,
                        user_id,
                        json.dumps(_numbers_for_card(room_id, card_number)),
                        card_number,
                        stake_santim,
                        utc_now(),
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                # Postgres allows concurrent writers (unlike SQLite's single-writer
                # model this codebase originally relied on), so two players can
                # race to claim the same cartela number; the unique index on
                # (room_id, card_number) is the real guard, this just turns the
                # loser's crash into the same clean conflict SQLite serialized away.
                raise ConflictError(
                    f"Cartela #{card_number} has already been taken"
                ) from exc
            card_id = cursor.fetchone()["id"]
            if settings.enable_real_money:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_at
                    ) VALUES (%s, %s, 'entry', 'card', %s, %s, %s)
                    """,
                    (
                        user_id,
                        -stake_santim,
                        card_id,
                        f"Lucky {stake_santim / 100:g} birr cartela #{card_number}",
                        utc_now(),
                    ),
                )

        rows = connection.execute(
            """
            SELECT * FROM cards WHERE room_id = %s AND user_id = %s
            ORDER BY card_number
            """,
            (room_id, user_id),
        ).fetchall()
    return [_serialize_card(row) for row in rows]


def join_room(
    room_id: int, user_id: int, card_number: int | None = None
) -> dict[str, Any]:
    cards = join_cards(
        room_id,
        user_id,
        [card_number] if card_number is not None else None,
    )
    if card_number is not None:
        return next(card for card in cards if card["card_number"] == card_number)
    return cards[0]


def cancel_cards(
    room_id: int,
    user_id: int,
    card_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Cancel one or all of a player's cartelas before the round starts.

    Deletes the card rows outright (freeing the cartela number immediately
    for anyone else) rather than soft-cancelling them, so every existing
    query over `cards` — sold numbers, pool totals, player counts — stays
    correct without needing a "not cancelled" filter bolted on everywhere.
    Any real-money entry cost is reversed via the same idempotent 'refund'
    ledger pattern used for dismissed rounds.
    """
    with transaction(immediate=True) as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if room is None:
            raise NotFoundError("Room not found")
        if room["state"] != "waiting":
            raise ConflictError("Cartelas can only be cancelled before the game starts")

        owned = connection.execute(
            "SELECT * FROM cards WHERE room_id = %s AND user_id = %s",
            (room_id, user_id),
        ).fetchall()
        if not owned:
            raise NotFoundError("You don't have any cartelas in this room")

        if card_ids is None:
            to_cancel = list(owned)
        else:
            owned_by_id = {card["id"]: card for card in owned}
            missing = [card_id for card_id in card_ids if card_id not in owned_by_id]
            if missing:
                raise NotFoundError("That cartela does not belong to you")
            to_cancel = [owned_by_id[card_id] for card_id in dict.fromkeys(card_ids)]

        for card in to_cancel:
            if settings.enable_real_money and card["entry_cost_santim"] > 0:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_at
                    ) VALUES (%s, %s, 'refund', 'card', %s, %s, %s)
                    ON CONFLICT (user_id, kind, reference_type, reference_id) DO NOTHING
                    """,
                    (
                        user_id,
                        card["entry_cost_santim"],
                        card["id"],
                        f"Lucky cartela #{card['card_number']} cancelled before start",
                        utc_now(),
                    ),
                )
            connection.execute("DELETE FROM cards WHERE id = %s", (card["id"],))

        remaining = connection.execute(
            """
            SELECT * FROM cards WHERE room_id = %s AND user_id = %s
            ORDER BY card_number
            """,
            (room_id, user_id),
        ).fetchall()
    return [_serialize_card(row) for row in remaining]


def available_card_numbers(room_id: int) -> list[int]:
    room = get_room(room_id)
    with connect() as connection:
        used = {
            row["card_number"]
            for row in connection.execute(
                "SELECT card_number FROM cards WHERE room_id = %s AND card_number IS NOT NULL",
                (room_id,),
            ).fetchall()
        }
    return [
        number for number in range(1, room["card_capacity"] + 1) if number not in used
    ]


def sold_card_numbers(room_id: int) -> list[int]:
    get_room(room_id)
    with connect() as connection:
        return [
            row["card_number"]
            for row in connection.execute(
                """
                SELECT card_number FROM cards
                WHERE room_id = %s AND card_number IS NOT NULL
                ORDER BY card_number
                """,
                (room_id,),
            ).fetchall()
        ]


def get_room_participant_telegram_ids(room_id: int) -> list[int]:
    with connect() as connection:
        return [
            row["telegram_id"]
            for row in connection.execute(
                """
                SELECT DISTINCT u.telegram_id
                FROM cards c JOIN users u ON u.id = c.user_id
                WHERE c.room_id = %s
                """,
                (room_id,),
            ).fetchall()
        ]


def arm_auto_start(room_id: int, delay_seconds: float) -> dict[str, Any]:
    """Arm (or unarm) the auto-start countdown.

    Returns the room decorated with `just_armed`: True only when *this call*
    transitioned the room from unarmed to armed, not merely when the room
    happens to already be armed. Callers use that distinction to fire a
    one-time "room is starting" notification without re-sending it on every
    subsequent join, or after a server restart resumes an already-armed room.
    """
    just_armed = False
    with transaction(immediate=True) as connection:
        room = connection.execute(
            """
            SELECT r.*, COUNT(c.id) AS player_count,
                   COUNT(DISTINCT c.user_id) AS unique_player_count
            FROM rooms r LEFT JOIN cards c ON c.room_id = r.id
            WHERE r.id = %s GROUP BY r.id
            """,
            (room_id,),
        ).fetchone()
        if room is None:
            raise NotFoundError("Room not found")
        threshold = int(room["auto_start_min_players"] or 0)
        ready = start_requirement_met(room)
        if (
            room["state"] == "waiting"
            and threshold > 0
            and ready
            and not room["auto_start_at"]
        ):
            starts_at = datetime.now(UTC) + timedelta(seconds=max(0, delay_seconds))
            connection.execute(
                "UPDATE rooms SET auto_start_at = %s WHERE id = %s",
                (starts_at.isoformat(), room_id),
            )
            just_armed = True
        elif (
            room["state"] == "waiting"
            and threshold > 0
            and not ready
            and room["auto_start_at"]
        ):
            connection.execute(
                "UPDATE rooms SET auto_start_at = NULL WHERE id = %s", (room_id,)
            )
    result = get_room(room_id)
    result["just_armed"] = just_armed
    return result


def preview_card(room_id: int, user_id: int, card_number: int) -> dict[str, Any]:
    """Preview a cartela's numbers, whether or not it belongs to this player.

    A card's numbers are deterministic from (room_id, card_number) alone, so
    showing them isn't sensitive — this lets a player look at, and watch for
    free, a cartela another player already bought, without exposing who owns
    it or letting the viewer treat it as their own paid entry.
    """
    room = get_room(room_id)
    capacity = min(room["max_players"], room["card_capacity"])
    if not 1 <= card_number <= capacity:
        raise NotFoundError("Cartela number is outside this room")
    with connect() as connection:
        existing = connection.execute(
            "SELECT * FROM cards WHERE room_id = %s AND card_number = %s AND user_id = %s",
            (room_id, card_number, user_id),
        ).fetchone()
    if existing is not None:
        result = _serialize_card(existing)
        result["committed"] = True
        return result
    return {
        "id": None,
        "room_id": room_id,
        "card_number": card_number,
        "numbers": _numbers_for_card(room_id, card_number),
        "marks": [FREE_SPACE],
        "auto_mark": False,
        "entry_cost_santim": room["stake_santim"],
        "committed": False,
    }


def _serialize_card(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["numbers"] = json.loads(result.pop("numbers_json"))
    result["marks"] = json.loads(result.pop("marks_json"))
    result["auto_mark"] = bool(result["auto_mark"])
    result["blocked"] = bool(result["blocked"])
    return result


def get_cards(room_id: int, user_id: int) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM cards WHERE room_id = %s AND user_id = %s
            ORDER BY card_number
            """,
            (room_id, user_id),
        ).fetchall()
    return [_serialize_card(row) for row in rows]


def get_card(room_id: int, user_id: int) -> dict[str, Any] | None:
    cards = get_cards(room_id, user_id)
    return cards[0] if cards else None


def get_draws(room_id: int) -> list[int]:
    with connect() as connection:
        return [
            row["number"]
            for row in connection.execute(
                "SELECT number FROM draws WHERE room_id = %s ORDER BY sequence",
                (room_id,),
            ).fetchall()
        ]


def get_round_winners(room_id: int) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT rw.user_id, rw.card_id, rw.winning_sequence, rw.payout_santim,
                   c.card_number, c.numbers_json, u.telegram_id, u.first_name, u.username
            FROM round_winners rw
            JOIN cards c ON c.id = rw.card_id
            JOIN users u ON u.id = rw.user_id
            WHERE rw.room_id = %s ORDER BY c.card_number ASC
            """,
            (room_id,),
        ).fetchall()
    winners = []
    for row in rows:
        winner = dict(row)
        winner["numbers"] = json.loads(winner.pop("numbers_json"))
        winners.append(winner)
    return winners


def get_round_evidence(room_id: int) -> dict[str, Any] | None:
    with connect() as connection:
        evidence = connection.execute(
            "SELECT * FROM round_evidence WHERE room_id = %s", (room_id,)
        ).fetchone()
        if evidence is None:
            return None
        disputes = connection.execute(
            """
            SELECT rd.id, rd.reason, rd.created_at, u.id AS user_id,
                   u.telegram_id, u.first_name, u.username
            FROM round_disputes rd
            JOIN users u ON u.id = rd.user_id
            WHERE rd.room_id = %s ORDER BY rd.created_at, rd.id
            """,
            (room_id,),
        ).fetchall()
    result = dict(evidence)
    result["draws"] = json.loads(result.pop("draws_json"))
    result["winners"] = json.loads(result.pop("winners_json"))
    result["players"] = json.loads(result.pop("players_json"))
    result["disputes"] = [dict(row) for row in disputes]
    return result


def paginate_evidence_rounds(
    page: int = 1, page_size: int = 10, search: str = ""
) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(50, max(1, page_size))
    normalized_search = search.strip().lstrip("#")
    where_clause = ""
    parameters: list[Any] = []
    if normalized_search:
        where_clause = "WHERE r.id = %s" if normalized_search.isdigit() else "WHERE 1 = 0"
        if normalized_search.isdigit():
            parameters.append(int(normalized_search))

    with connect() as connection:
        total = connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM round_evidence re
            JOIN rooms r ON r.id = re.room_id
            {where_clause}
            """,
            parameters,
        ).fetchone()["total"]
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = connection.execute(
            f"""
            SELECT r.id, r.name, r.state, r.result_status, r.outcome,
                   r.stake_santim, r.created_at, r.started_at, r.finished_at,
                   r.result_detected_at, r.result_deadline_at, r.disputed_at,
                   r.final_called_number, r.winning_sequence,
                   re.created_at AS evidence_created_at,
                   (SELECT COUNT(*) FROM cards c WHERE c.room_id = r.id) AS card_count,
                   (SELECT COUNT(DISTINCT c.user_id) FROM cards c WHERE c.room_id = r.id) AS player_count,
                   (SELECT COUNT(*) FROM round_winners rw WHERE rw.room_id = r.id) AS winner_count,
                   (SELECT COUNT(*) FROM round_disputes rd WHERE rd.room_id = r.id) AS dispute_count
            FROM round_evidence re
            JOIN rooms r ON r.id = re.room_id
            {where_clause}
            ORDER BY re.id DESC
            LIMIT %s OFFSET %s
            """,
            [*parameters, page_size, offset],
        ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


def list_evidence_rounds() -> list[dict[str, Any]]:
    """Return all summaries for internal callers that need the complete record."""
    with connect() as connection:
        total = connection.execute(
            "SELECT COUNT(*) AS total FROM round_evidence"
        ).fetchone()["total"]
    if not total:
        return []
    return paginate_evidence_rounds(page_size=min(total, 50))["items"] if total <= 50 else [
        item
        for page in range(1, (total + 49) // 50 + 1)
        for item in paginate_evidence_rounds(page=page, page_size=50)["items"]
    ]


def _user_has_disputed(room_id: int, user_id: int) -> bool:
    with connect() as connection:
        return connection.execute(
            "SELECT 1 FROM round_disputes WHERE room_id = %s AND user_id = %s",
            (room_id, user_id),
        ).fetchone() is not None


def game_state(room_id: int, user_id: int) -> dict[str, Any]:
    room = get_room(room_id)
    cards = get_cards(room_id, user_id)
    return {
        "room": room,
        "cards": cards,
        "card": cards[0] if cards else None,
        "draws": get_draws(room_id),
        "balance_santim": wallet_balance(user_id),
        "winners": get_round_winners(room_id),
        "disputed_by_me": _user_has_disputed(room_id, user_id),
    }


def set_auto_mark(room_id: int, user_id: int, enabled: bool) -> list[dict[str, Any]]:
    with transaction(immediate=True) as connection:
        cursor = connection.execute(
            "UPDATE cards SET auto_mark = %s WHERE room_id = %s AND user_id = %s",
            (int(enabled), room_id, user_id),
        )
        if cursor.rowcount == 0:
            raise NotFoundError("Join the room before changing card mode")
    return get_cards(room_id, user_id)


def mark_number(
    room_id: int, user_id: int, number: int, card_id: int | None = None
) -> dict[str, Any]:
    with transaction(immediate=True) as connection:
        if card_id is None:
            row = connection.execute(
                """
                SELECT * FROM cards WHERE room_id = %s AND user_id = %s
                ORDER BY card_number LIMIT 1
                """,
                (room_id, user_id),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM cards WHERE id = %s AND room_id = %s AND user_id = %s",
                (card_id, room_id, user_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("Join the room before marking a card")
        if row["blocked"]:
            raise ConflictError("This cartela is blocked for the rest of this round")
        card = json.loads(row["numbers_json"])
        if number not in {value for card_row in card for value in card_row}:
            raise ConflictError("That number is not on your card")
        was_drawn = connection.execute(
            "SELECT 1 FROM draws WHERE room_id = %s AND number = %s", (room_id, number)
        ).fetchone()
        if was_drawn is None:
            raise ConflictError("That number has not been called")
        marks = set(json.loads(row["marks_json"])) | {FREE_SPACE, number}
        connection.execute(
            "UPDATE cards SET marks_json = %s WHERE id = %s",
            (json.dumps(sorted(marks)), row["id"]),
        )
        updated = connection.execute(
            "SELECT * FROM cards WHERE id = %s", (row["id"],)
        ).fetchone()
    return _serialize_card(updated)


def start_room(room_id: int) -> dict[str, Any]:
    with transaction(immediate=True) as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if room is None:
            raise NotFoundError("Room not found")
        if room["state"] == "finished":
            raise ConflictError("This game has already finished")
        if room["state"] == "waiting":
            counts = connection.execute(
                """
                SELECT COUNT(*) AS player_count,
                       COUNT(DISTINCT user_id) AS unique_player_count
                FROM cards WHERE room_id = %s
                """,
                (room_id,),
            ).fetchone()
            minimum = int(room["auto_start_min_players"] or 0)
            readiness = dict(room)
            readiness.update(dict(counts))
            if minimum and not start_requirement_met(readiness):
                if getattr(settings, "test_single_player_start", False):
                    raise ConflictError("Test mode requires one player with five cartelas")
                raise ConflictError(f"At least {minimum} different players are required to start")
            connection.execute(
                """
                UPDATE rooms SET state = 'running', started_at = %s,
                    auto_start_at = NULL WHERE id = %s
                """,
                (utc_now(), room_id),
            )
    return get_room(room_id)


def draw_next(room_id: int) -> int | None:
    random = __import__("secrets").SystemRandom()
    with transaction(immediate=True) as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if (
            room is None
            or room["state"] != "running"
            or (room["result_status"] or "open") != "open"
        ):
            return None
        rows = connection.execute(
            "SELECT number FROM draws WHERE room_id = %s", (room_id,)
        ).fetchall()
        drawn = {row["number"] for row in rows}
        remaining = list(set(range(1, 76)) - drawn)
        if not remaining:
            connection.execute(
                """
                UPDATE rooms SET state = 'finished', outcome = 'no_winner',
                    finished_at = %s WHERE id = %s
                """,
                (utc_now(), room_id),
            )
            return None
        number = random.choice(remaining)
        connection.execute(
            "INSERT INTO draws (room_id, number, sequence, called_at) VALUES (%s, %s, %s, %s)",
            (room_id, number, len(drawn) + 1, utc_now()),
        )
    return number


def _earliest_winning_sequence(card: list[list[int]], draws: list[int]) -> int | None:
    eligible: set[int] = set()
    for sequence, number in enumerate(draws, start=1):
        eligible.add(number)
        if has_bingo(card, eligible):
            return sequence
    return None


def _refund_round_in(
    connection: psycopg.Connection, room_id: int, cards: list[dict[str, Any]]
) -> None:
    if not settings.enable_real_money:
        return
    for card in cards:
        if card["entry_cost_santim"] <= 0:
            continue
        connection.execute(
            """
            INSERT INTO wallet_entries (
                user_id, amount_santim, kind, reference_type,
                reference_id, description, created_at
            ) VALUES (%s, %s, 'refund', 'card', %s, %s, %s)
            ON CONFLICT (user_id, kind, reference_type, reference_id) DO NOTHING
            """,
            (
                card["user_id"],
                card["entry_cost_santim"],
                card["id"],
                f"Lucky cartela #{card['card_number']} dismissed-round refund",
                utc_now(),
            ),
        )


def _settle_winners_in(
    connection: psycopg.Connection,
    room: dict[str, Any],
    winners: list[dict[str, Any]],
) -> None:
    gross_pool = int(
        connection.execute(
            "SELECT COALESCE(SUM(entry_cost_santim), 0) AS total FROM cards WHERE room_id = %s",
            (room["id"],),
        ).fetchone()["total"]
    )
    prize = calculate_prize(
        gross_pool,
        int(room["commission_bps"]),
        int(room["transfer_cost_santim"]),
    )
    ordered = sorted(winners, key=lambda row: (row["card_number"], row["id"]))
    base_share, remainder = divmod(prize["winner_payout_santim"], len(ordered))
    payouts_by_user: dict[int, int] = {}
    for index, winner in enumerate(ordered):
        share = base_share + (1 if index < remainder else 0)
        connection.execute(
            "UPDATE round_winners SET payout_santim = %s WHERE room_id = %s AND card_id = %s",
            (share, room["id"], winner["id"]),
        )
        payouts_by_user[winner["user_id"]] = (
            payouts_by_user.get(winner["user_id"], 0) + share
        )
    if settings.enable_real_money:
        for winner_user_id, share in payouts_by_user.items():
            if not share:
                continue
            connection.execute(
                """
                INSERT INTO wallet_entries (
                    user_id, amount_santim, kind, reference_type,
                    reference_id, description, created_at
                ) VALUES (%s, %s, 'payout', 'room', %s, %s, %s)
                ON CONFLICT (user_id, kind, reference_type, reference_id) DO NOTHING
                """,
                (
                    winner_user_id,
                    share,
                    room["id"],
                    f"Lucky payout ({len(ordered)} winning cartela(s))",
                    utc_now(),
                ),
            )
    connection.execute(
        """
        INSERT INTO settlements (
            room_id, winner_user_id, gross_pool_santim,
            commission_santim, transfer_cost_santim, payout_santim, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            room["id"],
            ordered[0]["user_id"],
            prize["gross_pool_santim"],
            prize["commission_santim"],
            prize["transfer_cost_santim"],
            prize["winner_payout_santim"],
            utc_now(),
        ),
    )
    connection.execute(
        """
        UPDATE rooms SET state = 'finished', outcome = 'winner',
            result_status = 'settled', finished_at = %s
        WHERE id = %s
        """,
        (utc_now(), room["id"]),
    )


def process_winner_window(room_id: int) -> dict[str, Any] | None:
    with transaction(immediate=True) as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if room is None:
            raise NotFoundError("Room not found")
        if room["state"] != "running":
            return None
        if (room["result_status"] or "open") != "open":
            return None
        draws = [
            row["number"]
            for row in connection.execute(
                "SELECT number FROM draws WHERE room_id = %s ORDER BY sequence",
                (room_id,),
            ).fetchall()
        ]
        cards = connection.execute(
            "SELECT * FROM cards WHERE room_id = %s ORDER BY card_number", (room_id,)
        ).fetchall()

        if room["winning_sequence"] is None:
            detected: list[tuple[dict[str, Any], int]] = []
            for card in cards:
                if card["blocked"]:
                    continue
                sequence = _earliest_winning_sequence(
                    json.loads(card["numbers_json"]), draws
                )
                if sequence is not None:
                    detected.append((card, sequence))
            if not detected:
                return None
            winning_sequence = min(sequence for _, sequence in detected)
            winners = [
                card for card, sequence in detected if sequence == winning_sequence
            ]
            detected_at = datetime.now(UTC)
            deadline = detected_at + timedelta(
                seconds=float(
                    getattr(settings, "result_confirmation_seconds", 15)
                )
            )
            for winner in winners:
                connection.execute(
                    """
                    INSERT INTO round_winners (
                        room_id, card_id, user_id, winning_sequence, detected_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        room_id,
                        winner["id"],
                        winner["user_id"],
                        winning_sequence,
                        detected_at.isoformat(),
                    ),
                )
            draw_evidence = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT sequence, number, called_at FROM draws
                    WHERE room_id = %s ORDER BY sequence
                    """,
                    (room_id,),
                ).fetchall()
            ]
            player_rows = connection.execute(
                """
                SELECT c.id AS card_id, c.card_number, c.numbers_json,
                       c.created_at AS card_created_at, u.id AS user_id,
                       u.telegram_id, u.first_name, u.username
                FROM cards c JOIN users u ON u.id = c.user_id
                WHERE c.room_id = %s ORDER BY c.card_number, c.id
                """,
                (room_id,),
            ).fetchall()
            player_evidence = []
            for player_row in player_rows:
                item = dict(player_row)
                item["numbers"] = json.loads(item.pop("numbers_json"))
                player_evidence.append(item)
            winner_ids = {winner["id"] for winner in winners}
            winner_evidence = [
                {
                    **item,
                    "winning_sequence": winning_sequence,
                    "detected_at": detected_at.isoformat(),
                }
                for item in player_evidence
                if item["card_id"] in winner_ids
            ]
            connection.execute(
                """
                INSERT INTO round_evidence (
                    room_id, final_called_number, winning_sequence, draws_json,
                    winners_json, players_json, room_created_at, room_started_at,
                    created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (room_id) DO NOTHING
                """,
                (
                    room_id,
                    draws[winning_sequence - 1],
                    winning_sequence,
                    json.dumps(draw_evidence, separators=(",", ":")),
                    json.dumps(winner_evidence, separators=(",", ":")),
                    json.dumps(player_evidence, separators=(",", ":")),
                    room["created_at"],
                    room["started_at"],
                    detected_at.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE rooms SET winning_sequence = %s, grace_deadline_sequence = NULL,
                    winner_user_id = %s, result_status = 'pending',
                    result_deadline_at = %s, result_detected_at = %s,
                    final_called_number = %s WHERE id = %s
                """,
                (
                    winning_sequence,
                    winners[0]["user_id"],
                    deadline.isoformat(),
                    detected_at.isoformat(),
                    draws[winning_sequence - 1],
                    room_id,
                ),
            )
        else:
            return None

    return {
        "type": "bingo_pending",
        "room": get_room(room_id),
        "winners": get_round_winners(room_id),
    }


def finalize_pending_result(
    room_id: int, *, force: bool = False
) -> dict[str, Any] | None:
    action: str | None = None
    with transaction(immediate=True) as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if room is None:
            raise NotFoundError("Room not found")
        if room["result_status"] == "disputed":
            return None
        if room["result_status"] != "pending":
            return None
        deadline = datetime.fromisoformat(room["result_deadline_at"])
        if not force and datetime.now(UTC) < deadline:
            return None
        winners = connection.execute(
            """
            SELECT c.* FROM round_winners rw
            JOIN cards c ON c.id = rw.card_id
            WHERE rw.room_id = %s ORDER BY c.card_number
            """,
            (room_id,),
        ).fetchall()
        if len(winners) > 4:
            cards = connection.execute(
                "SELECT * FROM cards WHERE room_id = %s ORDER BY card_number",
                (room_id,),
            ).fetchall()
            _refund_round_in(connection, room_id, cards)
            connection.execute(
                """
                UPDATE rooms SET state = 'cancelled', outcome = 'dismissed',
                    result_status = 'dismissed', finished_at = %s WHERE id = %s
                """,
                (utc_now(), room_id),
            )
            action = "dismissed"
        else:
            _settle_winners_in(connection, room, winners)
            action = "settled"
    return {
        "type": "game_dismissed" if action == "dismissed" else "game_settled",
        "room": get_room(room_id),
        "winners": get_round_winners(room_id),
    }


def dispute_round(room_id: int, user_id: int, reason: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    cleaned_reason = reason.strip() or "Player requested a result review"
    with transaction(immediate=True) as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if room is None:
            raise NotFoundError("Room not found")
        participant = connection.execute(
            "SELECT 1 FROM cards WHERE room_id = %s AND user_id = %s",
            (room_id, user_id),
        ).fetchone()
        if participant is None:
            raise ConflictError("Only a player in this round can dispute the result")
        if room["result_status"] != "pending":
            raise ConflictError("This round is not in the result review window")
        deadline = datetime.fromisoformat(room["result_deadline_at"])
        if now > deadline:
            raise ConflictError("The 15-second result review window has closed")
        connection.execute(
            """
            INSERT INTO round_disputes (room_id, user_id, reason, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            (room_id, user_id, cleaned_reason[:300], now.isoformat()),
        )
        connection.execute(
            """
            UPDATE rooms SET state = 'cancelled', result_status = 'disputed',
                disputed_at = %s, finished_at = %s WHERE id = %s
            """,
            (now.isoformat(), now.isoformat(), room_id),
        )
    return {
        "type": "game_disputed",
        "room": get_room(room_id),
        "winners": get_round_winners(room_id),
    }


def claim_bingo(
    room_id: int, user_id: int, card_id: int | None = None
) -> bool:
    process_winner_window(room_id)
    with transaction(immediate=True) as connection:
        room = connection.execute(
            "SELECT * FROM rooms WHERE id = %s", (room_id,)
        ).fetchone()
        if room is None:
            raise NotFoundError("Room not found")
        card_rows = connection.execute(
            "SELECT * FROM cards WHERE room_id = %s AND user_id = %s ORDER BY card_number",
            (room_id, user_id),
        ).fetchall()
        if not card_rows:
            raise NotFoundError("No card found")
        if card_id is None:
            claimed_card = card_rows[0]
        else:
            claimed_card = next(
                (card for card in card_rows if card["id"] == card_id), None
            )
            if claimed_card is None:
                raise NotFoundError("That cartela does not belong to you")
        if claimed_card["blocked"]:
            raise ConflictError("This cartela is blocked for the rest of this round")

        winner = connection.execute(
            """
            SELECT rw.card_id FROM round_winners rw
            WHERE rw.room_id = %s AND rw.card_id = %s
            """,
            (room_id, claimed_card["id"]),
        ).fetchone()
        accepted = winner is not None and room["outcome"] != "dismissed"
        draws = {
            row["number"]
            for row in connection.execute(
                "SELECT number FROM draws WHERE room_id = %s", (room_id,)
            ).fetchall()
        }
        is_valid_line = has_bingo(json.loads(claimed_card["numbers_json"]), draws)
        if not is_valid_line and room["state"] == "running":
            connection.execute(
                "UPDATE cards SET blocked = 1 WHERE id = %s", (claimed_card["id"],)
            )
        connection.execute(
            """
            INSERT INTO claims (room_id, card_id, user_id, accepted, claimed_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (room_id, claimed_card["id"], user_id, int(accepted), utc_now()),
        )
    return accepted


def wallet_summary(user_id: int, limit: int = 50) -> dict[str, Any]:
    with connect() as connection:
        entries = connection.execute(
            """
            SELECT id, amount_santim, kind, reference_type, reference_id,
                   description, created_at
            FROM wallet_entries WHERE user_id = %s
            ORDER BY id DESC LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
        deposits = connection.execute(
            """
            SELECT id, provider, amount_santim, transaction_id, status,
                   submitted_at, reviewed_at, review_note
            FROM deposits WHERE user_id = %s ORDER BY id DESC LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
        withdrawals = connection.execute(
            """
            SELECT id, provider, amount_santim, account_number, account_name,
                   status, submitted_at, reviewed_at, review_note, payout_reference
            FROM withdrawals WHERE user_id = %s ORDER BY id DESC LIMIT %s
            """,
            (user_id, limit),
        ).fetchall()
        transfers = connection.execute(
            """
            SELECT t.id, t.amount_santim, t.status, t.submitted_at, t.reviewed_at,
                   CASE WHEN t.sender_user_id = %s THEN 'sent' ELSE 'received' END AS direction,
                   sender.telegram_id AS sender_telegram_id,
                   sender.first_name AS sender_first_name,
                   recipient.telegram_id AS recipient_telegram_id,
                   recipient.first_name AS recipient_first_name
            FROM transfers t
            JOIN users sender ON sender.id = t.sender_user_id
            JOIN users recipient ON recipient.id = t.recipient_user_id
            WHERE t.sender_user_id = %s OR t.recipient_user_id = %s
            ORDER BY t.id DESC LIMIT %s
            """,
            (user_id, user_id, user_id, limit),
        ).fetchall()
        ledger_balance = _ledger_balance_in(connection, user_id)
        reserved_withdrawals = _reserved_withdrawals_in(connection, user_id)
        reserved_transfers = _reserved_transfers_in(connection, user_id)
        bonus_total = _bonus_total_in(connection, user_id)
    balance = ledger_balance - reserved_withdrawals - reserved_transfers
    return {
        "balance_santim": balance,
        "ledger_balance_santim": ledger_balance,
        "reserved_withdrawal_santim": reserved_withdrawals,
        "reserved_transfer_santim": reserved_transfers,
        "bonus_santim": bonus_total,
        "withdrawable_balance_santim": max(0, balance - bonus_total),
        "real_money_enabled": settings.enable_real_money,
        "entries": [dict(row) for row in entries],
        "deposits": [dict(row) for row in deposits],
        "withdrawals": [dict(row) for row in withdrawals],
        "transfers": [dict(row) for row in transfers],
    }


def normalize_transaction_id(transaction_id: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", transaction_id.upper())
    if len(normalized) < 4 or len(normalized) > 80:
        raise ValueError("Transaction ID must contain 4 to 80 letters or numbers")
    return normalized


def submit_deposit(
    user_id: int,
    amount_santim: int,
    transaction_id: str,
    provider: str = "manual",
    receipt_file_id: str | None = None,
) -> dict[str, Any]:
    provider = provider.strip().lower()
    if provider not in {"telebirr", "cbe", "cbe_account", "manual"}:
        raise ValueError("Provider must be telebirr, cbe, cbe_account, or manual")
    if amount_santim < settings.minimum_deposit_santim:
        raise ValueError(
            f"Minimum deposit is {settings.minimum_deposit_santim / 100:.2f} birr"
        )
    if amount_santim > 100_000_000:
        raise ValueError("Deposit amount is outside the allowed range")
    normalized = normalize_transaction_id(transaction_id)
    try:
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO deposits (
                    user_id, provider, amount_santim, transaction_id,
                    transaction_id_normalized, receipt_file_id, submitted_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    user_id,
                    provider,
                    amount_santim,
                    transaction_id.strip(),
                    normalized,
                    receipt_file_id,
                    utc_now(),
                ),
            )
            row = cursor.fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise ConflictError("That transaction ID has already been submitted") from exc
    return dict(row)


def list_deposits(status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
    if status not in {"pending", "approved", "rejected"}:
        raise ValueError("Invalid deposit status")
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT d.*, u.telegram_id, u.first_name, u.username,
                   reviewer.telegram_id AS reviewer_telegram_id,
                   reviewer.first_name AS reviewer_name
            FROM deposits d
            JOIN users u ON u.id = d.user_id
            LEFT JOIN users reviewer ON reviewer.id = d.reviewed_by_user_id
            WHERE d.status = %s ORDER BY d.id ASC LIMIT %s
            """,
            (status, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_deposit(deposit_id: int) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT d.*, u.telegram_id, u.first_name, u.username
            FROM deposits d JOIN users u ON u.id = d.user_id
            WHERE d.id = %s
            """,
            (deposit_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError("Deposit request not found")
    return dict(row)


def review_deposit(
    deposit_id: int,
    admin_user_id: int,
    approve: bool,
    note: str | None = None,
) -> dict[str, Any]:
    status = "approved" if approve else "rejected"
    with transaction(immediate=True) as connection:
        deposit = connection.execute(
            "SELECT * FROM deposits WHERE id = %s", (deposit_id,)
        ).fetchone()
        if deposit is None:
            raise NotFoundError("Deposit request not found")
        if deposit["status"] != "pending":
            raise ConflictError(f"Deposit was already {deposit['status']}")
        connection.execute(
            """
            UPDATE deposits SET status = %s, reviewed_by_user_id = %s,
                review_note = %s, reviewed_at = %s
            WHERE id = %s AND status = 'pending'
            """,
            (status, admin_user_id, note, utc_now(), deposit_id),
        )
        if approve:
            connection.execute(
                """
                INSERT INTO wallet_entries (
                    user_id, amount_santim, kind, reference_type, reference_id,
                    description, created_by_user_id, created_at
                ) VALUES (%s, %s, 'deposit', 'deposit', %s, %s, %s, %s)
                """,
                (
                    deposit["user_id"],
                    deposit["amount_santim"],
                    deposit_id,
                    f"Approved {deposit['provider']} deposit",
                    admin_user_id,
                    utc_now(),
                ),
            )
    return get_deposit(deposit_id)


def submit_withdrawal(
    user_id: int,
    amount_santim: int,
    provider: str,
    account_number: str,
    account_name: str,
) -> dict[str, Any]:
    provider = provider.strip().lower()
    if provider not in {"telebirr", "cbe", "cbe_account"}:
        raise ValueError("Provider must be telebirr, cbe, or cbe_account")
    if amount_santim < settings.minimum_withdrawal_santim:
        raise ValueError(
            f"Minimum withdrawal is {settings.minimum_withdrawal_santim / 100:.2f} birr"
        )
    cleaned_number = account_number.strip()
    if not 5 <= len(cleaned_number) <= 40:
        raise ValueError("Enter a valid Telebirr or CBE account number")
    cleaned_name = " ".join(account_name.split())
    if not 2 <= len(cleaned_name) <= 100:
        raise ValueError("Enter the account holder name")

    with transaction(immediate=True) as connection:
        available = _withdrawable_balance_in(connection, user_id)
        if amount_santim > available:
            raise ConflictError(
                f"Your withdrawable balance is {available / 100:.2f} birr. "
                "Welcome bonus money cannot be withdrawn."
            )
        cursor = connection.execute(
            """
            INSERT INTO withdrawals (
                user_id, provider, amount_santim, account_number,
                account_name, submitted_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                user_id,
                provider,
                amount_santim,
                cleaned_number,
                cleaned_name,
                utc_now(),
            ),
        )
        row = cursor.fetchone()
    return dict(row)


def list_withdrawals(
    status: str = "pending", limit: int = 100
) -> list[dict[str, Any]]:
    if status not in {"pending", "approved", "rejected"}:
        raise ValueError("Invalid withdrawal status")
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT w.*, u.telegram_id, u.first_name, u.username,
                   reviewer.telegram_id AS reviewer_telegram_id,
                   reviewer.first_name AS reviewer_name
            FROM withdrawals w
            JOIN users u ON u.id = w.user_id
            LEFT JOIN users reviewer ON reviewer.id = w.reviewed_by_user_id
            WHERE w.status = %s ORDER BY w.id ASC LIMIT %s
            """,
            (status, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_withdrawal(withdrawal_id: int) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT w.*, u.telegram_id, u.first_name, u.username
            FROM withdrawals w JOIN users u ON u.id = w.user_id
            WHERE w.id = %s
            """,
            (withdrawal_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError("Withdrawal request not found")
    return dict(row)


def review_withdrawal(
    withdrawal_id: int,
    admin_user_id: int,
    approve: bool,
    payout_reference: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    status = "approved" if approve else "rejected"
    normalized_reference = None
    cleaned_reference = None
    if approve:
        cleaned_reference = (payout_reference or "").strip()
        if not cleaned_reference:
            raise ValueError("Enter the bank payout transaction reference")
        normalized_reference = normalize_transaction_id(cleaned_reference)

    try:
        with transaction(immediate=True) as connection:
            withdrawal = connection.execute(
                "SELECT * FROM withdrawals WHERE id = %s", (withdrawal_id,)
            ).fetchone()
            if withdrawal is None:
                raise NotFoundError("Withdrawal request not found")
            if withdrawal["status"] != "pending":
                raise ConflictError(
                    f"Withdrawal was already {withdrawal['status']}"
                )
            connection.execute(
                """
                UPDATE withdrawals SET status = %s, reviewed_by_user_id = %s,
                    review_note = %s, payout_reference = %s,
                    payout_reference_normalized = %s, reviewed_at = %s
                WHERE id = %s AND status = 'pending'
                """,
                (
                    status,
                    admin_user_id,
                    note,
                    cleaned_reference,
                    normalized_reference,
                    utc_now(),
                    withdrawal_id,
                ),
            )
            if approve:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_by_user_id, created_at
                    ) VALUES (%s, %s, 'adjustment', 'withdrawal', %s, %s, %s, %s)
                    """,
                    (
                        withdrawal["user_id"],
                        -withdrawal["amount_santim"],
                        withdrawal_id,
                        f"Approved {withdrawal['provider']} withdrawal",
                        admin_user_id,
                        utc_now(),
                    ),
                )
    except psycopg.errors.UniqueViolation as exc:
        raise ConflictError(
            "That payout transaction reference has already been used"
        ) from exc
    return get_withdrawal(withdrawal_id)


def find_user_by_telegram_id(telegram_id: int) -> dict[str, Any] | None:
    return get_user_by_telegram_id(telegram_id)


def submit_transfer(
    sender_user_id: int, recipient_telegram_id: int, amount_santim: int
) -> dict[str, Any]:
    if amount_santim < settings.minimum_transfer_santim:
        raise ValueError(
            f"Minimum transfer is {settings.minimum_transfer_santim / 100:.2f} birr"
        )
    if amount_santim > 100_000_000:
        raise ValueError("Transfer amount is outside the allowed range")

    with transaction(immediate=True) as connection:
        sender = connection.execute(
            "SELECT * FROM users WHERE id = %s", (sender_user_id,)
        ).fetchone()
        if sender is None:
            raise NotFoundError("Sender account not found")
        recipient = connection.execute(
            "SELECT * FROM users WHERE telegram_id = %s", (recipient_telegram_id,)
        ).fetchone()
        if recipient is None:
            raise NotFoundError("No Lucky player with that Telegram ID")
        if recipient["id"] == sender_user_id:
            raise ConflictError("You cannot transfer to yourself")

        available = _withdrawable_balance_in(connection, sender_user_id)
        if amount_santim > available:
            raise ConflictError(
                f"Your transferable balance is {available / 100:.2f} birr. "
                "Welcome bonus money cannot be transferred."
            )

        cursor = connection.execute(
            """
            INSERT INTO transfers (
                sender_user_id, recipient_user_id, amount_santim, submitted_at
            ) VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (sender_user_id, recipient["id"], amount_santim, utc_now()),
        )
        row = cursor.fetchone()
    return dict(row)


def _with_wallet_snapshots_in(
    connection: psycopg.Connection, transfer: dict[str, Any]
) -> dict[str, Any]:
    """Attach before/after balance snapshots for the admin review screen.

    `_wallet_balance_in` already nets out this transfer's own reservation
    (it's one of the pending transfers `_reserved_transfers_in` sums), so
    that figure alone reads as "balance after the transfer clears," not
    "balance right now." Adding the amount back gives the true before figure
    without a second, unreserved balance query.
    """
    amount = transfer["amount_santim"]
    sender_wallet = _wallet_breakdown_in(connection, transfer["sender_user_id"])
    recipient_wallet = _wallet_breakdown_in(connection, transfer["recipient_user_id"])
    transfer["sender_balance_before_santim"] = sender_wallet["balance_santim"] + amount
    transfer["sender_balance_after_santim"] = sender_wallet["balance_santim"]
    transfer["sender_bonus_santim"] = sender_wallet["bonus_santim"]
    transfer["recipient_balance_before_santim"] = recipient_wallet["balance_santim"]
    transfer["recipient_balance_after_santim"] = recipient_wallet["balance_santim"] + amount
    return transfer


def list_transfers(status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
    if status not in {"pending", "approved", "rejected"}:
        raise ValueError("Invalid transfer status")
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT t.*, sender.telegram_id AS sender_telegram_id,
                   sender.first_name AS sender_first_name,
                   sender.username AS sender_username,
                   recipient.telegram_id AS recipient_telegram_id,
                   recipient.first_name AS recipient_first_name,
                   recipient.username AS recipient_username,
                   reviewer.telegram_id AS reviewer_telegram_id,
                   reviewer.first_name AS reviewer_name
            FROM transfers t
            JOIN users sender ON sender.id = t.sender_user_id
            JOIN users recipient ON recipient.id = t.recipient_user_id
            LEFT JOIN users reviewer ON reviewer.id = t.reviewed_by_user_id
            WHERE t.status = %s ORDER BY t.id ASC LIMIT %s
            """,
            (status, limit),
        ).fetchall()
        transfers = [
            _with_wallet_snapshots_in(connection, dict(row)) for row in rows
        ]
    return transfers


def get_transfer(transfer_id: int) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT t.*, sender.telegram_id AS sender_telegram_id,
                   sender.first_name AS sender_first_name,
                   sender.username AS sender_username,
                   recipient.telegram_id AS recipient_telegram_id,
                   recipient.first_name AS recipient_first_name,
                   recipient.username AS recipient_username
            FROM transfers t
            JOIN users sender ON sender.id = t.sender_user_id
            JOIN users recipient ON recipient.id = t.recipient_user_id
            WHERE t.id = %s
            """,
            (transfer_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("Transfer request not found")
        return _with_wallet_snapshots_in(connection, dict(row))


def review_transfer(
    transfer_id: int,
    admin_user_id: int,
    approve: bool,
    note: str | None = None,
) -> dict[str, Any]:
    status = "approved" if approve else "rejected"
    with transaction(immediate=True) as connection:
        transfer = connection.execute(
            "SELECT * FROM transfers WHERE id = %s", (transfer_id,)
        ).fetchone()
        if transfer is None:
            raise NotFoundError("Transfer request not found")
        if transfer["status"] != "pending":
            raise ConflictError(f"Transfer was already {transfer['status']}")

        sender = connection.execute(
            "SELECT telegram_id, first_name FROM users WHERE id = %s",
            (transfer["sender_user_id"],),
        ).fetchone()
        recipient = connection.execute(
            "SELECT telegram_id, first_name FROM users WHERE id = %s",
            (transfer["recipient_user_id"],),
        ).fetchone()

        connection.execute(
            """
            UPDATE transfers SET status = %s, reviewed_by_user_id = %s,
                review_note = %s, reviewed_at = %s
            WHERE id = %s AND status = 'pending'
            """,
            (status, admin_user_id, note, utc_now(), transfer_id),
        )
        if approve:
            connection.execute(
                """
                INSERT INTO wallet_entries (
                    user_id, amount_santim, kind, reference_type,
                    reference_id, description, created_by_user_id, created_at
                ) VALUES (%s, %s, 'transfer_out', 'transfer', %s, %s, %s, %s)
                ON CONFLICT (user_id, kind, reference_type, reference_id) DO NOTHING
                """,
                (
                    transfer["sender_user_id"],
                    -transfer["amount_santim"],
                    transfer_id,
                    f"Transfer sent to {recipient['first_name']} "
                    f"(Telegram {recipient['telegram_id']})",
                    admin_user_id,
                    utc_now(),
                ),
            )
            connection.execute(
                """
                INSERT INTO wallet_entries (
                    user_id, amount_santim, kind, reference_type,
                    reference_id, description, created_by_user_id, created_at
                ) VALUES (%s, %s, 'transfer_in', 'transfer', %s, %s, %s, %s)
                ON CONFLICT (user_id, kind, reference_type, reference_id) DO NOTHING
                """,
                (
                    transfer["recipient_user_id"],
                    transfer["amount_santim"],
                    transfer_id,
                    f"Transfer received from {sender['first_name']} "
                    f"(Telegram {sender['telegram_id']})",
                    admin_user_id,
                    utc_now(),
                ),
            )
    return get_transfer(transfer_id)


def _settlement_totals_in(
    connection: psycopg.Connection, where_clause: str = "", parameters: tuple[Any, ...] = ()
) -> dict[str, int]:
    row = connection.execute(
        f"""
        SELECT COUNT(*) AS settled_rounds,
               COALESCE(SUM(commission_santim), 0) AS commission_santim,
               COALESCE(SUM(transfer_cost_santim), 0) AS transfer_cost_santim,
               COALESCE(SUM(gross_pool_santim), 0) AS gross_pool_santim,
               COALESCE(SUM(payout_santim), 0) AS payout_santim
        FROM settlements {where_clause}
        """,
        parameters,
    ).fetchone()
    return dict(row)


def revenue_summary() -> dict[str, Any]:
    """Commission earned is a reporting figure, not money held anywhere in the
    app: it's simply the gap between what entries collected and what payouts
    released. The real birr already sits in the operator's Telebirr/CBE
    account as deposits and withdrawals settle — nothing here needs a
    separate cash-out step.
    """
    with connect() as connection:
        all_time = _settlement_totals_in(connection)
        today = _settlement_totals_in(
            connection, "WHERE (created_at::timestamptz)::date = CURRENT_DATE"
        )
        last_7_days = _settlement_totals_in(
            connection, "WHERE created_at::timestamptz >= NOW() - INTERVAL '7 days'"
        )
        last_30_days = _settlement_totals_in(
            connection, "WHERE created_at::timestamptz >= NOW() - INTERVAL '30 days'"
        )
        by_tier = connection.execute(
            """
            SELECT r.stake_santim,
                   COUNT(*) AS settled_rounds,
                   COALESCE(SUM(s.commission_santim), 0) AS commission_santim
            FROM settlements s
            JOIN rooms r ON r.id = s.room_id
            GROUP BY r.stake_santim
            ORDER BY r.stake_santim
            """
        ).fetchall()
        dismissed_rounds = connection.execute(
            "SELECT COUNT(*) AS dismissed_rounds FROM rooms WHERE outcome = 'dismissed'"
        ).fetchone()["dismissed_rounds"]
    return {
        "all_time": all_time,
        "today": today,
        "last_7_days": last_7_days,
        "last_30_days": last_30_days,
        "by_tier": [dict(row) for row in by_tier],
        "dismissed_rounds": dismissed_rounds,
    }


def leaderboard(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT u.telegram_id, u.first_name, u.username,
                   COUNT(DISTINCT c.room_id) AS games,
                   COUNT(DISTINCT CASE WHEN r.winner_user_id = u.id THEN r.id END) AS wins
            FROM users u
            LEFT JOIN cards c ON c.user_id = u.id
            LEFT JOIN rooms r ON r.id = c.room_id
            GROUP BY u.id
            ORDER BY wins DESC, games DESC, u.created_at ASC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
