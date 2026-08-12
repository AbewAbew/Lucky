import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app import db, repository
import app.main as main_module
from app.config import settings
from app.main import app, manager


def setup_database():
    """See tests/test_money.py::setup_database for the full rationale: points
    app.db at an isolated `pytest` Postgres schema (never the app's real
    `public` schema) and truncates it to a clean slate.
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


@pytest.fixture(autouse=True)
def enable_development_auth_for_api_tests(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module, "settings", replace(settings, allow_dev_auth=True)
    )


def test_player_can_join_mark_and_win() -> None:
    asyncio.run(_player_can_join_mark_and_win())


async def _player_can_join_mark_and_win() -> None:
    original_settings = setup_database()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        auth = await client.post(
            "/api/auth", json={"dev_user_id": 1001, "dev_first_name": "Test Player"}
        )
        assert auth.status_code == 200
        headers = {"Authorization": f"Bearer {auth.json()['token']}"}

        created = await client.post(
            "/api/admin/rooms",
            headers={"X-Admin-Key": settings.admin_key},
            json={
                "name": "API Test",
                "max_players": 10,
                "auto_start_min_players": 0,
                "call_interval_seconds": 60,
            },
        )
        assert created.status_code == 200
        room_id = created.json()["id"]

        joined = await client.post(
            f"/api/rooms/{room_id}/join", headers=headers, json={}
        )
        assert joined.status_code == 200
        card = joined.json()["card"]
        first_row = [number for number in card["numbers"][0] if number]

        repository.start_room(room_id)
        late_auth = await client.post(
            "/api/auth", json={"dev_user_id": 1002, "dev_first_name": "Late Player"}
        )
        late_headers = {"Authorization": f"Bearer {late_auth.json()['token']}"}
        late_join = await client.post(
            f"/api/rooms/{room_id}/join", headers=late_headers, json={}
        )
        assert late_join.status_code == 409

        with db.transaction(immediate=True) as connection:
            for sequence, number in enumerate(first_row, start=1):
                connection.execute(
                    "INSERT INTO draws (room_id, number, sequence, called_at) VALUES (%s, %s, %s, %s)",
                    (room_id, number, sequence, db.utc_now()),
                )

        for number in first_row:
            marked = await client.post(
                f"/api/rooms/{room_id}/mark", headers=headers, json={"number": number}
            )
            assert marked.status_code == 200

        claim = await client.post(
            f"/api/rooms/{room_id}/claim", headers=headers, json={}
        )
        assert claim.status_code == 200
        assert claim.json()["accepted"] is True
        assert claim.json()["result_status"] == "pending"
        assert claim.json()["result_deadline_at"]
        assert repository.draw_next(room_id) is None
        assert len(repository.get_draws(room_id)) == len(first_row)
        outcome = repository.finalize_pending_result(room_id, force=True)
        assert outcome["type"] == "game_settled"
        assert repository.get_room(room_id)["winner_name"] == "Test Player"

    await manager.close()
    db.settings = original_settings


def test_protected_routes_require_a_session() -> None:
    asyncio.run(_protected_routes_require_a_session())


async def _protected_routes_require_a_session() -> None:
    original_settings = setup_database()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/rooms")
        player_auth = await client.post(
            "/api/auth", json={"dev_user_id": 2001, "dev_first_name": "Player"}
        )
        player_headers = {"Authorization": f"Bearer {player_auth.json()['token']}"}
        forbidden = await client.get("/api/control/dashboard", headers=player_headers)
        forbidden_evidence = await client.get(
            "/api/control/evidence", headers=player_headers
        )
        admin_auth = await client.post(
            "/api/auth",
            json={
                "dev_user_id": settings.admin_telegram_ids[0],
                "dev_first_name": "Admin",
            },
        )
        admin_headers = {"Authorization": f"Bearer {admin_auth.json()['token']}"}
        dashboard = await client.get("/api/control/dashboard", headers=admin_headers)
        evidence = await client.get(
            "/api/control/evidence?page=1&page_size=10", headers=admin_headers
        )
    assert response.status_code == 401
    assert forbidden.status_code == 403
    assert forbidden_evidence.status_code == 403
    assert dashboard.status_code == 200
    assert evidence.status_code == 200
    assert evidence.json()["page"] == 1
    assert evidence.json()["total_pages"] == 1
    db.settings = original_settings


def test_api_previews_and_joins_multiple_cartelas() -> None:
    asyncio.run(_api_previews_and_joins_multiple_cartelas())


async def _api_previews_and_joins_multiple_cartelas() -> None:
    original_settings = setup_database()
    repository.ensure_tier_rooms()
    room = next(room for room in repository.list_rooms() if room["stake_santim"] == 200)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        auth = await client.post(
            "/api/auth", json={"dev_user_id": 3001, "dev_first_name": "Multi"}
        )
        headers = {"Authorization": f"Bearer {auth.json()['token']}"}
        preview = await client.get(
            f"/api/rooms/{room['id']}/cards/20/preview", headers=headers
        )
        joined = await client.post(
            f"/api/rooms/{room['id']}/join",
            headers=headers,
            json={"card_numbers": [20, 21, 22]},
        )
        available = await client.get(
            f"/api/rooms/{room['id']}/available-cards", headers=headers
        )

    assert preview.status_code == 200
    assert joined.status_code == 200
    assert [card["card_number"] for card in joined.json()["cards"]] == [20, 21, 22]
    assert joined.json()["cards"][0]["numbers"] == preview.json()["numbers"]
    assert available.json()["owned"] == [20, 21, 22]
    await manager.close()
    db.settings = original_settings
