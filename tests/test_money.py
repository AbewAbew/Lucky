import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from app import db, repository
from app.auth import TelegramUser
from app.config import settings


def setup_database():
    """Point app.db at an isolated `pytest` schema on the real Neon database
    (never the app's own `public` schema) and wipe it to a clean slate.

    Reuses one shared schema across the whole test session instead of
    creating a fresh one per test (as the old per-test SQLite file did) —
    recreating a Postgres schema every test would be slow over the network
    to Neon, so this truncates tables instead, which is fast and gives the
    same "every test starts from empty, auto-increment IDs reset to 1"
    guarantee the SQLite-file-per-test approach used to provide.
    """
    original_db_settings = db.settings
    db.settings = SimpleNamespace(
        database_url=os.environ["DATABASE_URL"], db_schema="pytest"
    )
    db.init_db()
    with db.transaction() as connection:
        connection.execute(
            "TRUNCATE TABLE users, rooms, cards, draws, claims, wallet_entries, "
            "deposits, withdrawals, settlements, round_winners, round_evidence, "
            "round_disputes, transfers RESTART IDENTITY CASCADE"
        )
    return original_db_settings


def test_prize_calculation_uses_exact_santim() -> None:
    assert repository.calculate_prize(10_000, 500, 200) == {
        "gross_pool_santim": 10_000,
        "commission_santim": 500,
        "transfer_cost_santim": 200,
        "winner_payout_santim": 9_300,
    }


def test_round_evidence_pagination_is_newest_first_and_searches_all_pages() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            room_ids = []
            for index in range(12):
                room = repository.create_room(f"Evidence round {index + 1}")
                room_ids.append(room["id"])
                with db.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        INSERT INTO round_evidence (
                            room_id, final_called_number, winning_sequence,
                            draws_json, winners_json, players_json,
                            room_created_at, room_started_at, created_at
                        ) VALUES (%s, 1, 1, '[]', '[]', '[]', %s, %s, %s)
                        """,
                        (
                            room["id"],
                            room["created_at"],
                            room["created_at"],
                            f"2026-08-10T12:{index:02d}:00+00:00",
                        ),
                    )

            first_page = repository.paginate_evidence_rounds(page=1, page_size=5)
            second_page = repository.paginate_evidence_rounds(page=2, page_size=5)
            searched = repository.paginate_evidence_rounds(
                page=1, page_size=5, search=f"#{room_ids[0]}"
            )

            assert first_page["total"] == 12
            assert first_page["total_pages"] == 3
            assert [item["id"] for item in first_page["items"]] == list(
                reversed(room_ids[-5:])
            )
            assert [item["id"] for item in second_page["items"]] == list(
                reversed(room_ids[2:7])
            )
            assert searched["total"] == 1
            assert searched["items"][0]["id"] == room_ids[0]
        finally:
            db.settings = original_db_settings


def test_single_player_test_mode_requires_five_cartelas() -> None:
    original_settings = repository.settings
    repository.settings = SimpleNamespace(test_single_player_start=True)
    try:
        assert not repository.start_requirement_met(
            {"unique_player_count": 1, "player_count": 4, "auto_start_min_players": 5}
        )
        assert repository.start_requirement_met(
            {"unique_player_count": 1, "player_count": 5, "auto_start_min_players": 5}
        )
    finally:
        repository.settings = original_settings


def test_lucky_creates_three_400_card_tiers() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            repository.ensure_tier_rooms()
            rooms = [
                room for room in repository.list_rooms() if room["state"] == "waiting"
            ]
            assert {room["stake_santim"] for room in rooms} == {200, 500, 1_000}
            assert all(room["card_capacity"] == 400 for room in rooms)
            assert all(room["commission_bps"] == 500 for room in rooms)
            assert all(room["auto_start_min_players"] == 5 for room in rooms)
        finally:
            db.settings = original_db_settings


def test_fifth_sold_cartela_arms_the_twenty_second_lobby_countdown() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            repository.ensure_tier_rooms()
            room = next(
                room
                for room in repository.list_rooms()
                if room["stake_santim"] == 200 and room["state"] == "waiting"
            )
            for card_number in range(1, 5):
                player = repository.upsert_user(
                    TelegramUser(
                        telegram_id=8000 + card_number,
                        first_name=f"Lobby {card_number}",
                    )
                )
                repository.join_room(
                    room["id"], player["id"], card_number=card_number
                )

            unarmed = repository.arm_auto_start(room["id"], 20)
            assert unarmed["auto_start_at"] is None
            assert unarmed["just_armed"] is False
            with pytest.raises(repository.ConflictError, match="At least 5"):
                repository.start_room(room["id"])

            fifth = repository.upsert_user(
                TelegramUser(telegram_id=8005, first_name="Lobby 5")
            )
            repository.join_room(room["id"], fifth["id"], card_number=5)
            armed = repository.arm_auto_start(room["id"], 20)
            seconds_remaining = (
                datetime.fromisoformat(armed["auto_start_at"]) - datetime.now(UTC)
            ).total_seconds()

            assert 18 <= seconds_remaining <= 20
            assert armed["just_armed"] is True
            assert repository.sold_card_numbers(room["id"]) == [1, 2, 3, 4, 5]

            # A second arm call on an already-armed room must not report
            # just_armed again — this is what stops the "room is starting"
            # notification from re-firing on every subsequent join, or every
            # server restart that resumes an already-armed room.
            still_armed = repository.arm_auto_start(room["id"], 20)
            assert still_armed["just_armed"] is False
            assert still_armed["auto_start_at"] == armed["auto_start_at"]

            assert repository.start_room(room["id"])["state"] == "running"
        finally:
            db.settings = original_db_settings


def test_five_cartelas_from_one_person_do_not_start_the_countdown() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            room = repository.create_room(
                "Lucky Unique Players", stake_santim=200, auto_start_min_players=5
            )
            first_player = repository.upsert_user(
                TelegramUser(telegram_id=8101, first_name="Five Cartelas")
            )
            repository.join_cards(
                room["id"], first_player["id"], [1, 2, 3, 4, 5]
            )

            one_person_room = repository.arm_auto_start(room["id"], 20)
            assert one_person_room["player_count"] == 5
            assert one_person_room["unique_player_count"] == 1
            assert one_person_room["auto_start_at"] is None
            with pytest.raises(repository.ConflictError, match="different players"):
                repository.start_room(room["id"])

            for offset in range(2, 6):
                player = repository.upsert_user(
                    TelegramUser(
                        telegram_id=8100 + offset,
                        first_name=f"Unique {offset}",
                    )
                )
                repository.join_room(
                    room["id"], player["id"], card_number=4 + offset
                )

            five_people_room = repository.arm_auto_start(room["id"], 20)
            assert five_people_room["player_count"] == 9
            assert five_people_room["unique_player_count"] == 5
            assert five_people_room["auto_start_at"] is not None
        finally:
            db.settings = original_db_settings


def test_deposit_approval_entry_and_winner_payout_are_atomic() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=101, first_name="Player")
            )
            admin = repository.upsert_user(
                TelegramUser(telegram_id=999_000, first_name="Admin")
            )
            with pytest.raises(ValueError, match="Minimum deposit is 10.00 birr"):
                repository.submit_deposit(
                    player["id"], 999, "TOO-SMALL-1", "telebirr"
                )
            deposit = repository.submit_deposit(
                player["id"], 5_000, "AB-123-XYZ", "telebirr"
            )

            with pytest.raises(repository.ConflictError):
                repository.submit_deposit(player["id"], 5_000, "ab123xyz", "cbe")

            repository.review_deposit(deposit["id"], admin["id"], True)
            assert repository.wallet_balance(player["id"]) == 5_000
            with pytest.raises(repository.ConflictError):
                repository.review_deposit(deposit["id"], admin["id"], True)

            room = repository.create_room(
                "Lucky 5 Birr Test",
                max_players=400,
                auto_start_min_players=0,
                stake_santim=500,
                transfer_cost_santim=100,
            )
            card = repository.join_room(room["id"], player["id"], card_number=77)
            assert card["card_number"] == 77
            assert repository.wallet_balance(player["id"]) == 4_500

            repository.start_room(room["id"])
            first_row = [number for number in card["numbers"][0] if number]
            with db.transaction(immediate=True) as connection:
                for sequence, number in enumerate(first_row, start=1):
                    connection.execute(
                        "INSERT INTO draws (room_id, number, sequence, called_at) VALUES (%s, %s, %s, %s)",
                        (room["id"], number, sequence, db.utc_now()),
                    )
            for number in first_row:
                repository.mark_number(room["id"], player["id"], number)

            assert repository.claim_bingo(room["id"], player["id"])
            assert repository.draw_next(room["id"]) is None
            assert repository.finalize_pending_result(
                room["id"], force=True
            )["type"] == "game_settled"
            completed = repository.get_room(room["id"])
            assert completed["gross_pool_santim"] == 500
            assert completed["commission_santim"] == 25
            assert completed["transfer_cost_santim"] == 100
            assert completed["winner_payout_santim"] == 375
            assert repository.wallet_balance(player["id"]) == 4_875
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_withdrawal_reserves_available_balance_until_admin_review() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=111, first_name="Withdrawal Player")
            )
            admin = repository.upsert_user(
                TelegramUser(telegram_id=999_000, first_name="Admin")
            )
            deposit = repository.submit_deposit(
                player["id"], 15_000, "WITHDRAW-FUND-1", "telebirr"
            )
            repository.review_deposit(deposit["id"], admin["id"], True)

            with pytest.raises(ValueError, match="Minimum withdrawal is 100.00 birr"):
                repository.submit_withdrawal(
                    player["id"], 9_999, "telebirr", "0911000000", "Player"
                )

            pending = repository.submit_withdrawal(
                player["id"], 10_000, "telebirr", "0911000000", "Player"
            )
            wallet = repository.wallet_summary(player["id"])
            assert wallet["ledger_balance_santim"] == 15_000
            assert wallet["reserved_withdrawal_santim"] == 10_000
            assert wallet["balance_santim"] == 5_000
            with pytest.raises(repository.ConflictError, match="withdrawable balance"):
                repository.submit_withdrawal(
                    player["id"], 10_000, "cbe", "1000123456", "Player"
                )

            rejected = repository.review_withdrawal(
                pending["id"], admin["id"], False, note="Account could not be verified"
            )
            assert rejected["status"] == "rejected"
            assert repository.wallet_balance(player["id"]) == 15_000

            approved_request = repository.submit_withdrawal(
                player["id"], 10_000, "cbe", "1000123456", "Player"
            )
            approved = repository.review_withdrawal(
                approved_request["id"],
                admin["id"],
                True,
                payout_reference="CBE-PAYOUT-001",
            )
            assert approved["status"] == "approved"
            final_wallet = repository.wallet_summary(player["id"])
            assert final_wallet["ledger_balance_santim"] == 5_000
            assert final_wallet["reserved_withdrawal_santim"] == 0
            assert final_wallet["balance_santim"] == 5_000
            assert final_wallet["withdrawals"][0]["payout_reference"] == "CBE-PAYOUT-001"
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_two_cartelas_debit_four_birr_and_twenty_birr_pool_pays_nineteen() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            admin = repository.upsert_user(
                TelegramUser(telegram_id=999_000, first_name="Admin")
            )
            winner = repository.upsert_user(
                TelegramUser(telegram_id=120, first_name="Winner")
            )
            deposit = repository.submit_deposit(
                winner["id"], 5_000, "EXAMPLE-DEPOSIT-50", "telebirr"
            )
            repository.review_deposit(deposit["id"], admin["id"], True)
            room = repository.create_room(
                "Lucky 2 Birr Example",
                max_players=400,
                auto_start_min_players=5,
                stake_santim=200,
                transfer_cost_santim=0,
            )
            winner_cards = repository.join_cards(room["id"], winner["id"], [1, 2])
            assert repository.wallet_balance(winner["id"]) == 4_600
            assert repository.game_state(room["id"], winner["id"])["balance_santim"] == 4_600

            all_cards = list(winner_cards)
            for index in range(4):
                player = repository.upsert_user(
                    TelegramUser(
                        telegram_id=121 + index, first_name=f"Player {index}"
                    )
                )
                with db.transaction(immediate=True) as connection:
                    connection.execute(
                        """
                        INSERT INTO wallet_entries (
                            user_id, amount_santim, kind, reference_type,
                            reference_id, description, created_at
                        ) VALUES (%s, 400, 'adjustment', 'test', %s, 'Test funds', %s)
                        """,
                        (player["id"], player["id"], db.utc_now()),
                    )
                all_cards.extend(
                    repository.join_cards(
                        room["id"], player["id"], [3 + index * 2, 4 + index * 2]
                    )
                )

            winning_row = winner_cards[0]["numbers"][0]
            drawn = set(winning_row)
            with db.transaction(immediate=True) as connection:
                for card in all_cards[1:]:
                    safe_numbers = [row[:] for row in card["numbers"]]
                    for column in range(5):
                        used = {row[column] for row in safe_numbers}
                        candidates = range(column * 15 + 1, column * 15 + 16)
                        for row in range(5):
                            if safe_numbers[row][column] not in drawn:
                                continue
                            replacement = next(
                                number
                                for number in candidates
                                if number not in used and number not in drawn
                            )
                            used.add(replacement)
                            safe_numbers[row][column] = replacement
                    connection.execute(
                        "UPDATE cards SET numbers_json = %s WHERE id = %s",
                        (json.dumps(safe_numbers), card["id"]),
                    )

            repository.start_room(room["id"])
            with db.transaction(immediate=True) as connection:
                for sequence, number in enumerate(winning_row, start=1):
                    connection.execute(
                        """
                        INSERT INTO draws (room_id, number, sequence, called_at)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (room["id"], number, sequence, db.utc_now()),
                    )
            repository.process_winner_window(room["id"])
            assert repository.claim_bingo(room["id"], winner["id"], winner_cards[0]["id"])
            assert len(repository.get_round_winners(room["id"])) == 1
            repository.finalize_pending_result(room["id"], force=True)

            assert repository.get_room(room["id"])["gross_pool_santim"] == 2_000
            assert repository.wallet_balance(winner["id"]) == 6_500
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_player_can_preview_and_commit_up_to_five_cartelas() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=202, first_name="Five Cards")
            )
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_at
                    ) VALUES (%s, 2000, 'adjustment', 'test', 202, 'Test credit', %s)
                    """,
                    (player["id"], db.utc_now()),
                )
            room = repository.create_room("Lucky Five", stake_santim=200)
            preview = repository.preview_card(room["id"], player["id"], 41)
            cards = repository.join_cards(
                room["id"], player["id"], [41, 42, 43, 44, 45]
            )

            assert len(cards) == 5
            assert cards[0]["numbers"] == preview["numbers"]
            assert repository.wallet_balance(player["id"]) == 1_000
            assert repository.get_room(room["id"])["player_count"] == 5
            assert repository.get_room(room["id"])["gross_pool_santim"] == 1_000
            with pytest.raises(repository.ConflictError, match="up to 5"):
                repository.join_cards(room["id"], player["id"], [46])
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_wrong_bingo_blocks_only_the_claimed_cartela() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=204, first_name="Wrong Claim")
            )
            room = repository.create_room("Lucky Wrong Claim", stake_santim=200)
            cards = repository.join_cards(room["id"], player["id"], [31, 32])
            repository.start_room(room["id"])

            assert repository.claim_bingo(
                room["id"], player["id"], cards[0]["id"]
            ) is False
            updated = repository.get_cards(room["id"], player["id"])
            assert updated[0]["blocked"] is True
            assert updated[1]["blocked"] is False
            with pytest.raises(repository.ConflictError, match="blocked"):
                repository.mark_number(
                    room["id"], player["id"], cards[0]["numbers"][0][0], cards[0]["id"]
                )

            with db.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE cards SET numbers_json = %s WHERE id = %s",
                    (json.dumps(cards[0]["numbers"]), cards[1]["id"]),
                )
                for sequence, number in enumerate(cards[0]["numbers"][0], start=1):
                    connection.execute(
                        "INSERT INTO draws (room_id, number, sequence, called_at) VALUES (%s, %s, %s, %s)",
                        (room["id"], number, sequence, db.utc_now()),
                    )
            outcome = repository.process_winner_window(room["id"])
            assert outcome["type"] == "bingo_pending"
            # cards[1] is manual (default), so process_winner_window detects it
            # but doesn't pay it out yet — only claim_bingo does that.
            assert outcome["winners"] == []
            assert repository.claim_bingo(room["id"], player["id"], cards[1]["id"])
            assert [
                winner["card_id"] for winner in repository.get_round_winners(room["id"])
            ] == [cards[1]["id"]]
        finally:
            db.settings = original_db_settings


def test_one_player_receives_the_total_for_two_winning_cartelas() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=203, first_name="Double Winner")
            )
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_at
                    ) VALUES (%s, 1000, 'adjustment', 'test', 203, 'Test credit', %s)
                    """,
                    (player["id"], db.utc_now()),
                )
            room = repository.create_room(
                "Lucky Double Winner", stake_santim=500, transfer_cost_santim=0
            )
            cards = repository.join_cards(room["id"], player["id"], [11, 12])
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE cards SET numbers_json = %s WHERE id = %s",
                    (json.dumps(cards[0]["numbers"]), cards[1]["id"]),
                )
            repository.start_room(room["id"])
            with db.transaction(immediate=True) as connection:
                for sequence, number in enumerate(cards[0]["numbers"][0], start=1):
                    connection.execute(
                        "INSERT INTO draws (room_id, number, sequence, called_at) VALUES (%s, %s, %s, %s)",
                        (room["id"], number, sequence, db.utc_now()),
                    )

            pending = repository.process_winner_window(room["id"])
            assert pending["type"] == "bingo_pending"
            assert repository.draw_next(room["id"]) is None
            assert repository.claim_bingo(room["id"], player["id"], cards[0]["id"])
            assert repository.claim_bingo(room["id"], player["id"], cards[1]["id"])
            settled = repository.finalize_pending_result(room["id"], force=True)

            assert settled["type"] == "game_settled"
            assert [winner["payout_santim"] for winner in settled["winners"]] == [
                475,
                475,
            ]
            assert repository.wallet_balance(player["id"]) == 950
            payout_entries = [
                entry
                for entry in repository.wallet_summary(player["id"])["entries"]
                if entry["kind"] == "payout"
            ]
            assert len(payout_entries) == 1
            assert payout_entries[0]["amount_santim"] == 950
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_same_call_winners_split_equally_after_frozen_result_window() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            room = repository.create_room(
                "Lucky Split Test", stake_santim=500, transfer_cost_santim=0
            )
            users = [
                repository.upsert_user(
                    TelegramUser(telegram_id=300 + index, first_name=f"Winner {index}")
                )
                for index in range(2)
            ]
            cards = [
                repository.join_room(room["id"], user["id"], card_number=index + 1)
                for index, user in enumerate(users)
            ]
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE cards SET numbers_json = %s WHERE room_id = %s AND user_id = %s",
                    (json.dumps(cards[0]["numbers"]), room["id"], users[1]["id"]),
                )
            repository.start_room(room["id"])
            first_row = cards[0]["numbers"][0]
            with db.transaction(immediate=True) as connection:
                for sequence, number in enumerate(first_row, start=1):
                    connection.execute(
                        "INSERT INTO draws (room_id, number, sequence, called_at) VALUES (%s, %s, %s, %s)",
                        (room["id"], number, sequence, db.utc_now()),
                    )

            pending = repository.process_winner_window(room["id"])
            assert pending["type"] == "bingo_pending"
            assert "evidence" not in pending
            assert pending["room"]["result_status"] == "pending"
            assert pending["room"]["result_deadline_at"]
            assert pending["room"]["final_called_number"] == first_row[-1]
            assert repository.draw_next(room["id"]) is None
            assert repository.get_draws(room["id"]) == first_row
            evidence = repository.get_round_evidence(room["id"])
            assert evidence["room_id"] == room["id"]
            assert len(evidence["draws"]) == len(first_row)
            assert len(evidence["winners"]) == 2
            assert len(evidence["players"]) == 2

            # Both cartelas are manual (default), so neither is paid until its
            # owner presses BINGO — round_evidence above already reflects both
            # as detected winners, but round_winners (and everything derived
            # from it) only updates once each one is actually claimed.
            assert repository.claim_bingo(room["id"], users[0]["id"], cards[0]["id"])
            assert repository.claim_bingo(room["id"], users[1]["id"], cards[1]["id"])
            assert len(repository.get_round_winners(room["id"])) == 2

            summaries = repository.list_evidence_rounds()
            assert summaries[0]["id"] == room["id"]
            assert summaries[0]["winner_count"] == 2

            settled = repository.finalize_pending_result(room["id"], force=True)
            assert settled["type"] == "game_settled"
            assert [winner["payout_santim"] for winner in settled["winners"]] == [
                475,
                475,
            ]
        finally:
            db.settings = original_db_settings


def test_more_than_four_same_call_winners_dismisses_and_refunds() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            room = repository.create_room("Lucky Dismiss Test", stake_santim=500)
            users = [
                repository.upsert_user(
                    TelegramUser(telegram_id=400 + index, first_name=f"Player {index}")
                )
                for index in range(5)
            ]
            with db.transaction(immediate=True) as connection:
                for index, user in enumerate(users):
                    connection.execute(
                        """
                        INSERT INTO wallet_entries (
                            user_id, amount_santim, kind, reference_type,
                            reference_id, description, created_at
                        ) VALUES (%s, 500, 'adjustment', 'test', %s, 'Test credit', %s)
                        """,
                        (user["id"], index + 1, db.utc_now()),
                    )
            cards = [
                repository.join_room(room["id"], user["id"], card_number=index + 1)
                for index, user in enumerate(users)
            ]
            shared_card_json = json.dumps(cards[0]["numbers"])
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE cards SET numbers_json = %s WHERE room_id = %s",
                    (shared_card_json, room["id"]),
                )
            repository.start_room(room["id"])
            with db.transaction(immediate=True) as connection:
                for sequence, number in enumerate(cards[0]["numbers"][0], start=1):
                    connection.execute(
                        "INSERT INTO draws (room_id, number, sequence, called_at) VALUES (%s, %s, %s, %s)",
                        (room["id"], number, sequence, db.utc_now()),
                    )

            pending = repository.process_winner_window(room["id"])
            assert pending["type"] == "bingo_pending"
            for user, card in zip(users, cards, strict=True):
                assert repository.claim_bingo(room["id"], user["id"], card["id"])
            dismissed = repository.finalize_pending_result(room["id"], force=True)
            assert dismissed["type"] == "game_dismissed"
            assert len(dismissed["winners"]) == 5
            assert dismissed["room"]["outcome"] == "dismissed"
            assert dismissed["room"]["commission_santim"] == 0
            assert dismissed["room"]["refund_santim"] == 2_500
            assert all(repository.wallet_balance(user["id"]) == 500 for user in users)
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_player_dispute_freezes_payment_and_preserves_round_evidence() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=500, first_name="Disputing Player")
            )
            outsider = repository.upsert_user(
                TelegramUser(telegram_id=501, first_name="Outsider")
            )
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_at
                    ) VALUES (%s, 500, 'adjustment', 'test', 500, 'Test credit', %s)
                    """,
                    (player["id"], db.utc_now()),
                )
            room = repository.create_room(
                "Lucky Dispute Test", stake_santim=500, transfer_cost_santim=0
            )
            card = repository.join_room(room["id"], player["id"], card_number=25)
            repository.start_room(room["id"])
            first_row = card["numbers"][0]
            with db.transaction(immediate=True) as connection:
                for sequence, number in enumerate(first_row, start=1):
                    connection.execute(
                        "INSERT INTO draws (room_id, number, sequence, called_at) VALUES (%s, %s, %s, %s)",
                        (room["id"], number, sequence, db.utc_now()),
                    )

            pending = repository.process_winner_window(room["id"])
            assert pending["type"] == "bingo_pending"
            with pytest.raises(repository.ConflictError, match="Only a player"):
                repository.dispute_round(room["id"], outsider["id"], "Not my game")

            disputed = repository.dispute_round(
                room["id"], player["id"], "Please review the final call"
            )
            assert disputed["type"] == "game_disputed"
            assert disputed["room"]["result_status"] == "disputed"
            assert disputed["room"]["state"] == "cancelled"
            assert disputed["room"]["outcome"] is None
            assert repository.finalize_pending_result(room["id"], force=True) is None
            assert repository.wallet_balance(player["id"]) == 0
            evidence = repository.get_round_evidence(room["id"])
            assert evidence["final_called_number"] == first_row[-1]
            assert evidence["disputes"][0]["telegram_id"] == 500
            assert evidence["disputes"][0]["reason"] == "Please review the final call"
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_new_signup_grants_a_one_time_free_bonus_that_is_not_withdrawable() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, signup_bonus_santim=1_000)
        try:
            created = repository.upsert_user(
                TelegramUser(telegram_id=700, first_name="New Player")
            )
            assert created["signup_bonus_granted_santim"] == 1_000
            assert repository.wallet_balance(created["id"]) == 1_000

            wallet = repository.wallet_summary(created["id"])
            assert wallet["balance_santim"] == 1_000
            assert wallet["bonus_santim"] == 1_000
            assert wallet["withdrawable_balance_santim"] == 0

            returning = repository.upsert_user(
                TelegramUser(telegram_id=700, first_name="New Player")
            )
            assert returning["signup_bonus_granted_santim"] == 0
            assert repository.wallet_balance(created["id"]) == 1_000
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_withdrawal_excludes_bonus_money_from_the_withdrawable_balance() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(
            settings, enable_real_money=True, signup_bonus_santim=1_000
        )
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=701, first_name="Bonus Player")
            )
            admin = repository.upsert_user(
                TelegramUser(telegram_id=999_000, first_name="Admin")
            )
            assert repository.wallet_balance(player["id"]) == 1_000

            deposit = repository.submit_deposit(
                player["id"], 9_500, "BONUS-FUND-1", "telebirr"
            )
            repository.review_deposit(deposit["id"], admin["id"], True)

            wallet = repository.wallet_summary(player["id"])
            assert wallet["ledger_balance_santim"] == 10_500
            assert wallet["bonus_santim"] == 1_000
            assert wallet["withdrawable_balance_santim"] == 9_500

            with pytest.raises(repository.ConflictError, match="withdrawable balance"):
                repository.submit_withdrawal(
                    player["id"], 10_000, "telebirr", "0911000000", "Player"
                )

            top_up = repository.submit_deposit(
                player["id"], 1_000, "BONUS-FUND-2", "telebirr"
            )
            repository.review_deposit(top_up["id"], admin["id"], True)
            approved = repository.submit_withdrawal(
                player["id"], 10_000, "telebirr", "0911000000", "Player"
            )
            assert approved["amount_santim"] == 10_000
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_transfer_excludes_bonus_and_moves_balance_only_after_approval() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(
            settings, enable_real_money=True, signup_bonus_santim=1_000
        )
        try:
            sender = repository.upsert_user(
                TelegramUser(telegram_id=800, first_name="Sender")
            )
            recipient = repository.upsert_user(
                TelegramUser(telegram_id=801, first_name="Recipient")
            )
            admin = repository.upsert_user(
                TelegramUser(telegram_id=999_000, first_name="Admin")
            )
            deposit = repository.submit_deposit(
                sender["id"], 5_000, "XFER-FUND-1", "telebirr"
            )
            repository.review_deposit(deposit["id"], admin["id"], True)
            assert repository.wallet_balance(sender["id"]) == 6_000

            with pytest.raises(repository.ConflictError, match="transferable balance"):
                repository.submit_transfer(sender["id"], recipient["telegram_id"], 6_000)

            pending = repository.submit_transfer(
                sender["id"], recipient["telegram_id"], 5_000
            )
            wallet = repository.wallet_summary(sender["id"])
            assert wallet["reserved_transfer_santim"] == 5_000
            assert wallet["balance_santim"] == 1_000

            approved = repository.review_transfer(pending["id"], admin["id"], True)
            assert approved["status"] == "approved"
            assert repository.wallet_balance(sender["id"]) == 1_000
            # Recipient also received the 1,000 santim signup bonus on creation.
            assert repository.wallet_balance(recipient["id"]) == 6_000
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_preview_allows_watching_a_cartela_someone_else_already_bought() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            owner = repository.upsert_user(
                TelegramUser(telegram_id=900, first_name="Owner")
            )
            watcher = repository.upsert_user(
                TelegramUser(telegram_id=901, first_name="Watcher")
            )
            room = repository.create_room("Lucky Watch", stake_santim=200)
            owned = repository.join_cards(room["id"], owner["id"], [77])[0]

            preview = repository.preview_card(room["id"], watcher["id"], 77)
            assert preview["numbers"] == owned["numbers"]
            assert preview["committed"] is False

            owner_preview = repository.preview_card(room["id"], owner["id"], 77)
            assert owner_preview["committed"] is True
        finally:
            db.settings = original_db_settings


def test_rejected_transfer_releases_the_reservation() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            sender = repository.upsert_user(
                TelegramUser(telegram_id=802, first_name="Sender")
            )
            recipient = repository.upsert_user(
                TelegramUser(telegram_id=803, first_name="Recipient")
            )
            admin = repository.upsert_user(
                TelegramUser(telegram_id=999_000, first_name="Admin")
            )
            deposit = repository.submit_deposit(
                sender["id"], 5_000, "XFER-FUND-2", "telebirr"
            )
            repository.review_deposit(deposit["id"], admin["id"], True)

            with pytest.raises(repository.ConflictError, match="cannot transfer to yourself"):
                repository.submit_transfer(sender["id"], sender["telegram_id"], 1_000)
            with pytest.raises(repository.NotFoundError, match="No Lucky player"):
                repository.submit_transfer(sender["id"], 123_456_789, 1_000)

            pending = repository.submit_transfer(
                sender["id"], recipient["telegram_id"], 2_000
            )
            rejected = repository.review_transfer(pending["id"], admin["id"], False)
            assert rejected["status"] == "rejected"
            assert repository.wallet_balance(sender["id"]) == 5_000
            assert repository.wallet_balance(recipient["id"]) == 0
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_revenue_summary_totals_commission_by_period_and_tier() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=904, first_name="Revenue Player")
            )
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_at
                    ) VALUES (%s, 1000, 'adjustment', 'test', 904, 'Test credit', %s)
                    """,
                    (player["id"], db.utc_now()),
                )
            room = repository.create_room(
                "Lucky Revenue", stake_santim=500, transfer_cost_santim=0
            )
            card = repository.join_cards(room["id"], player["id"], [61])[0]
            repository.start_room(room["id"])
            with db.transaction(immediate=True) as connection:
                for sequence, number in enumerate(card["numbers"][0], start=1):
                    connection.execute(
                        "INSERT INTO draws (room_id, number, sequence, called_at) VALUES (%s, %s, %s, %s)",
                        (room["id"], number, sequence, db.utc_now()),
                    )
            repository.process_winner_window(room["id"])
            assert repository.claim_bingo(room["id"], player["id"], card["id"])
            settled = repository.finalize_pending_result(room["id"], force=True)
            assert settled["type"] == "game_settled"

            dismissed_room = repository.create_room(
                "Lucky Dismissed", stake_santim=200, transfer_cost_santim=0
            )
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE rooms SET outcome = 'dismissed' WHERE id = %s",
                    (dismissed_room["id"],),
                )

            revenue = repository.revenue_summary()
            assert revenue["all_time"]["settled_rounds"] == 1
            assert revenue["all_time"]["commission_santim"] == 25
            assert revenue["all_time"]["gross_pool_santim"] == 500
            assert revenue["all_time"]["payout_santim"] == 475
            assert revenue["today"]["commission_santim"] == 25
            assert revenue["last_7_days"]["commission_santim"] == 25
            assert revenue["last_30_days"]["commission_santim"] == 25
            assert revenue["by_tier"] == [
                {"stake_santim": 500, "settled_rounds": 1, "commission_santim": 25}
            ]
            assert revenue["dismissed_rounds"] == 1
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_revenue_summary_excludes_settlements_just_outside_the_period() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            user = repository.upsert_user(
                TelegramUser(telegram_id=905, first_name="Old Settlement")
            )
            room = repository.create_room("Lucky Old", stake_santim=200)
            # 7 days and 1 hour ago: outside the 7-day window, but its ISO
            # "T"-separated, UTC-offset-suffixed timestamp sorts as text
            # *after* SQLite's plain `datetime('now', '-7 days')` on the
            # same calendar day unless both sides are parsed with datetime().
            just_outside = (
                datetime.now(UTC) - timedelta(days=7, hours=1)
            ).isoformat()
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO settlements (
                        room_id, winner_user_id, gross_pool_santim,
                        commission_santim, transfer_cost_santim, payout_santim,
                        created_at
                    ) VALUES (%s, %s, 1000, 50, 0, 950, %s)
                    """,
                    (room["id"], user["id"], just_outside),
                )

            revenue = repository.revenue_summary()
            assert revenue["all_time"]["commission_santim"] == 50
            assert revenue["last_7_days"]["commission_santim"] == 0
            assert revenue["last_7_days"]["settled_rounds"] == 0
        finally:
            db.settings = original_db_settings


def test_cancel_one_cartela_refunds_and_frees_the_number() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=906, first_name="Cancel Player")
            )
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_at
                    ) VALUES (%s, 1000, 'adjustment', 'test', 906, 'Test credit', %s)
                    """,
                    (player["id"], db.utc_now()),
                )
            room = repository.create_room("Lucky Cancel", stake_santim=200)
            cards = repository.join_cards(room["id"], player["id"], [10, 11])
            assert repository.wallet_balance(player["id"]) == 600
            assert repository.sold_card_numbers(room["id"]) == [10, 11]

            remaining = repository.cancel_cards(room["id"], player["id"], [cards[0]["id"]])
            assert [card["card_number"] for card in remaining] == [11]
            assert repository.wallet_balance(player["id"]) == 800
            assert repository.sold_card_numbers(room["id"]) == [11]
            assert 10 in repository.available_card_numbers(room["id"])

            other = repository.upsert_user(
                TelegramUser(telegram_id=907, first_name="Other Player")
            )
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_at
                    ) VALUES (%s, 1000, 'adjustment', 'test', 907, 'Test credit', %s)
                    """,
                    (other["id"], db.utc_now()),
                )
            reclaimed = repository.join_cards(room["id"], other["id"], [10])
            assert reclaimed[0]["card_number"] == 10
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_cancel_all_cartelas_and_reject_after_start() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            player = repository.upsert_user(
                TelegramUser(telegram_id=908, first_name="Cancel All Player")
            )
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO wallet_entries (
                        user_id, amount_santim, kind, reference_type,
                        reference_id, description, created_at
                    ) VALUES (%s, 1000, 'adjustment', 'test', 908, 'Test credit', %s)
                    """,
                    (player["id"], db.utc_now()),
                )
            room = repository.create_room(
                "Lucky Cancel All", stake_santim=200, auto_start_min_players=0
            )
            repository.join_cards(room["id"], player["id"], [1, 2, 3])
            assert repository.wallet_balance(player["id"]) == 400

            remaining = repository.cancel_cards(room["id"], player["id"])
            assert remaining == []
            assert repository.wallet_balance(player["id"]) == 1_000
            assert repository.sold_card_numbers(room["id"]) == []

            with pytest.raises(repository.NotFoundError, match="don't have any cartelas"):
                repository.cancel_cards(room["id"], player["id"])

            repository.join_cards(room["id"], player["id"], [4])
            repository.start_room(room["id"])
            with pytest.raises(repository.ConflictError, match="before the game starts"):
                repository.cancel_cards(room["id"], player["id"])
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_cancelling_a_cartela_can_unarm_the_auto_start_countdown() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            room = repository.create_room(
                "Lucky Unarm", stake_santim=200, auto_start_min_players=5
            )
            players = [
                repository.upsert_user(
                    TelegramUser(telegram_id=9100 + index, first_name=f"Player {index}")
                )
                for index in range(5)
            ]
            cards = [
                repository.join_room(room["id"], player["id"], card_number=index + 1)
                for index, player in enumerate(players)
            ]
            armed = repository.arm_auto_start(room["id"], 20)
            assert armed["auto_start_at"] is not None
            assert armed["just_armed"] is True

            repository.cancel_cards(room["id"], players[0]["id"], [cards[0]["id"]])
            unarmed = repository.arm_auto_start(room["id"], 20)
            assert unarmed["auto_start_at"] is None
            assert unarmed["just_armed"] is False

            # Rejoining after the cancel is a genuinely new "ready to start"
            # moment, so just_armed should fire again — this is what makes
            # a second "room is starting" notification correct here, not a
            # duplicate of the first one.
            sixth = repository.upsert_user(
                TelegramUser(telegram_id=9200, first_name="Sixth Player")
            )
            repository.join_room(room["id"], sixth["id"], card_number=6)
            rearmed = repository.arm_auto_start(room["id"], 20)
            assert rearmed["auto_start_at"] is not None
            assert rearmed["just_armed"] is True
        finally:
            db.settings = original_db_settings


def test_transfer_admin_view_shows_balance_before_and_after_not_only_after() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        original_repository_settings = repository.settings
        repository.settings = replace(settings, enable_real_money=True)
        try:
            sender = repository.upsert_user(
                TelegramUser(telegram_id=909, first_name="Before After Sender")
            )
            recipient = repository.upsert_user(
                TelegramUser(telegram_id=910, first_name="Before After Recipient")
            )
            admin = repository.upsert_user(
                TelegramUser(telegram_id=999_000, first_name="Admin")
            )
            for telegram_id, user_id, amount, reference in (
                (909, sender["id"], 3_000, "BA-SENDER"),
                (910, recipient["id"], 2_000, "BA-RECIPIENT"),
            ):
                deposit = repository.submit_deposit(user_id, amount, reference, "telebirr")
                repository.review_deposit(deposit["id"], admin["id"], True)
            assert repository.wallet_balance(sender["id"]) == 3_000
            assert repository.wallet_balance(recipient["id"]) == 2_000

            pending = repository.submit_transfer(
                sender["id"], recipient["telegram_id"], 1_000
            )
            view = repository.get_transfer(pending["id"])
            # The bug: sender_balance previously always showed the *post*
            # reservation figure (2,000) under a plain "balance" label, with
            # no way to tell it apart from the pre-transfer figure.
            assert view["sender_balance_before_santim"] == 3_000
            assert view["sender_balance_after_santim"] == 2_000
            assert view["recipient_balance_before_santim"] == 2_000
            assert view["recipient_balance_after_santim"] == 3_000

            listed = repository.list_transfers("pending")
            assert listed[0]["sender_balance_before_santim"] == 3_000
            assert listed[0]["sender_balance_after_santim"] == 2_000
        finally:
            repository.settings = original_repository_settings
            db.settings = original_db_settings


def test_room_participant_telegram_ids_are_distinct_per_player_not_per_card() -> None:
    with TemporaryDirectory() as directory:
        original_db_settings = setup_database()
        try:
            room = repository.create_room("Lucky Notify", stake_santim=200)
            first = repository.upsert_user(
                TelegramUser(telegram_id=9300, first_name="First")
            )
            second = repository.upsert_user(
                TelegramUser(telegram_id=9301, first_name="Second")
            )
            # First player buys two cartelas; should still only appear once.
            repository.join_cards(room["id"], first["id"], [1, 2])
            repository.join_cards(room["id"], second["id"], [3])

            telegram_ids = repository.get_room_participant_telegram_ids(room["id"])
            assert sorted(telegram_ids) == [9300, 9301]

            empty_room = repository.create_room("Lucky Empty", stake_santim=200)
            assert repository.get_room_participant_telegram_ids(empty_room["id"]) == []
        finally:
            db.settings = original_db_settings
