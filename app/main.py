from __future__ import annotations

import asyncio
import hmac
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import repository
from app.auth import (
    AuthenticationError,
    TelegramUser,
    create_session_token,
    read_session_token,
    validate_init_data,
)
from app.bot import TelegramBot, versioned_web_app_url
from app.config import settings
from app.db import connect, init_db
from app.game import label_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lucky.web")


class AuthRequest(BaseModel):
    init_data: str = ""
    referral_telegram_id: int | None = None
    dev_user_id: int | None = None
    dev_first_name: str = "Demo Player"


class RoomCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    max_players: int = Field(default=400, ge=2, le=400)
    auto_start_min_players: int = Field(default=0, ge=0, le=400)
    call_interval_seconds: float = Field(default=3, ge=0.5, le=60)
    stake_santim: int = Field(default=200, ge=0, le=100_000)
    transfer_cost_santim: int = Field(default=0, ge=0, le=100_000)


class JoinRequest(BaseModel):
    card_number: int | None = Field(default=None, ge=1, le=400)
    card_numbers: list[int] = Field(default_factory=list, max_length=5)


class CancelCardsRequest(BaseModel):
    card_ids: list[int] | None = Field(default=None, max_length=5)


class DepositRequest(BaseModel):
    amount_santim: int = Field(
        ge=settings.minimum_deposit_santim, le=100_000_000
    )
    transaction_id: str = Field(min_length=4, max_length=120)
    provider: str = Field(default="manual", pattern="^(telebirr|cbe|cbe_account|manual)$")
    receipt_file_id: str | None = Field(default=None, max_length=300)


class DepositReviewRequest(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=300)


class WithdrawalRequest(BaseModel):
    amount_santim: int = Field(
        ge=settings.minimum_withdrawal_santim, le=100_000_000
    )
    provider: str = Field(pattern="^(telebirr|cbe|cbe_account)$")
    account_number: str = Field(min_length=5, max_length=40)
    account_name: str = Field(min_length=2, max_length=100)


class WithdrawalReviewRequest(BaseModel):
    approve: bool
    payout_reference: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=300)


class TransferRequest(BaseModel):
    recipient_telegram_id: int
    amount_santim: int = Field(
        ge=settings.minimum_transfer_santim, le=100_000_000
    )


class TransferReviewRequest(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=300)


class AutoMarkRequest(BaseModel):
    enabled: bool


class MarkRequest(BaseModel):
    number: int = Field(ge=1, le=75)
    card_id: int | None = Field(default=None, gt=0)


class ClaimRequest(BaseModel):
    card_id: int | None = Field(default=None, gt=0)


class DisputeRequest(BaseModel):
    reason: str = Field(default="Player requested a result review", max_length=300)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "telegram_id": user["telegram_id"],
        "username": user["username"],
        "first_name": user["first_name"],
        "last_name": user["last_name"],
        "photo_url": user["photo_url"],
    }


class GameManager:
    def __init__(self) -> None:
        self.subscribers: dict[int, set[WebSocket]] = defaultdict(set)
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.start_tasks: dict[int, asyncio.Task[None]] = {}
        self.result_tasks: dict[int, asyncio.Task[None]] = {}

    async def broadcast(self, room_id: int, event: dict[str, Any]) -> None:
        stale: list[WebSocket] = []
        for socket in tuple(self.subscribers[room_id]):
            try:
                await socket.send_json(event)
            except (RuntimeError, WebSocketDisconnect):
                stale.append(socket)
        for socket in stale:
            self.subscribers[room_id].discard(socket)

    async def subscribe(self, room_id: int, socket: WebSocket) -> None:
        self.subscribers[room_id].add(socket)

    def unsubscribe(self, room_id: int, socket: WebSocket) -> None:
        self.subscribers[room_id].discard(socket)

    def schedule_auto_start(self, room_id: int) -> dict[str, Any]:
        room = repository.arm_auto_start(
            room_id, settings.auto_start_delay_seconds
        )
        if room.get("just_armed"):
            asyncio.create_task(self._notify_room_starting_soon(room_id, room))
        if (
            room["state"] == "waiting"
            and room.get("auto_start_at")
            and room_id not in self.start_tasks
            and room_id not in self.tasks
        ):
            self.start_tasks[room_id] = asyncio.create_task(
                self._delayed_start(room_id)
            )
        return room

    async def _notify_room_starting_soon(
        self, room_id: int, room: dict[str, Any]
    ) -> None:
        """Proactively ping every player who joined this room, including
        those who closed the Mini App, the moment the countdown arms —
        not just players still watching the lobby live.
        """
        if not settings.bot_token:
            return
        telegram_ids = repository.get_room_participant_telegram_ids(room_id)
        if not telegram_ids:
            return
        delay = int(settings.auto_start_delay_seconds)
        text = (
            f"🎱 {room['name']} is ready to start!\n"
            f"Calls begin in {delay} seconds — don't miss it."
        )
        reply_markup = None
        if settings.public_url.startswith("https://"):
            reply_markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "Open Lucky",
                            "web_app": {
                                "url": versioned_web_app_url(
                                    f"{settings.public_url}/?startapp=room_{room_id}"
                                )
                            },
                        }
                    ]
                ]
            }
        for telegram_id in telegram_ids:
            await notify_telegram(telegram_id, text, reply_markup)

    async def _delayed_start(self, room_id: int) -> None:
        try:
            room = repository.get_room(room_id)
            starts_at = room.get("auto_start_at")
            if not starts_at:
                return
            remaining = (
                datetime.fromisoformat(starts_at) - datetime.now(UTC)
            ).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
            room = repository.get_room(room_id)
            threshold = room["auto_start_min_players"]
            if (
                room["state"] == "waiting"
                and threshold
                and repository.start_requirement_met(room)
            ):
                await self.start(room_id)
        finally:
            self.start_tasks.pop(room_id, None)

    async def start(self, room_id: int) -> None:
        repository.start_room(room_id)
        await self.broadcast(
            room_id,
            {
                "type": "game_started",
                "state": "running",
                "room": repository.get_room(room_id),
            },
        )
        if room_id not in self.tasks or self.tasks[room_id].done():
            self.tasks[room_id] = asyncio.create_task(self._draw_loop(room_id))

    async def _draw_loop(self, room_id: int) -> None:
        try:
            while True:
                room = repository.get_room(room_id)
                if room["result_status"] == "pending":
                    self.schedule_result_resolution(room_id)
                    break
                if room["state"] != "running":
                    break
                await asyncio.sleep(float(room["call_interval_seconds"]))
                number = repository.draw_next(room_id)
                if number is None:
                    room = repository.get_room(room_id)
                    if room["result_status"] == "pending":
                        self.schedule_result_resolution(room_id)
                        break
                    await self.broadcast(
                        room_id,
                        {"type": "game_finished", "room": repository.get_room(room_id)},
                    )
                    repository.ensure_quick_room()
                    break
                await self.broadcast(
                    room_id,
                    {
                        "type": "number_called",
                        "number": number,
                        "label": label_for(number),
                        "draws": repository.get_draws(room_id),
                    },
                )
                outcome = repository.process_winner_window(room_id)
                if outcome:
                    await self.broadcast(room_id, outcome)
                    if outcome["type"] == "bingo_pending":
                        self.schedule_result_resolution(room_id)
                    break
        finally:
            self.tasks.pop(room_id, None)

    def schedule_result_resolution(self, room_id: int) -> None:
        if room_id in self.result_tasks and not self.result_tasks[room_id].done():
            return
        self.result_tasks[room_id] = asyncio.create_task(
            self._resolve_pending_result(room_id)
        )

    async def _resolve_pending_result(self, room_id: int) -> None:
        try:
            room = repository.get_room(room_id)
            if room["result_status"] != "pending":
                return
            deadline = datetime.fromisoformat(room["result_deadline_at"])
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining > 0:
                await asyncio.sleep(remaining)
            outcome = repository.finalize_pending_result(room_id)
            if outcome:
                repository.ensure_tier_rooms()
                await self.broadcast(room_id, outcome)
        finally:
            self.result_tasks.pop(room_id, None)

    async def resume_games(self) -> None:
        for room in repository.list_rooms():
            if room["result_status"] == "pending":
                self.schedule_result_resolution(room["id"])
            elif room["state"] == "running":
                outcome = repository.process_winner_window(room["id"])
                if outcome:
                    await self.broadcast(room["id"], outcome)
                    self.schedule_result_resolution(room["id"])
                else:
                    self.tasks[room["id"]] = asyncio.create_task(
                        self._draw_loop(room["id"])
                    )
            elif room["state"] == "waiting" and room["auto_start_min_players"]:
                self.schedule_auto_start(room["id"])

    async def close(self) -> None:
        tasks = [
            *self.tasks.values(),
            *self.start_tasks.values(),
            *self.result_tasks.values(),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


manager = GameManager()
telegram_bot: TelegramBot | None = None


async def notify_telegram(
    chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
) -> None:
    if telegram_bot is None:
        return
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        await telegram_bot.call("sendMessage", payload)
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning("Could not notify Telegram chat %s: %s", chat_id, exc)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global telegram_bot
    if settings.enable_real_money and len(settings.admin_telegram_ids) != 3:
        raise RuntimeError(
            "ENABLE_REAL_MONEY requires exactly three ADMIN_TELEGRAM_IDS"
        )
    init_db()
    repository.ensure_quick_room()
    await manager.resume_games()
    if settings.bot_token:
        telegram_bot = TelegramBot(settings.bot_token)
        try:
            await telegram_bot.configure()
            if settings.public_url.startswith("https://"):
                await telegram_bot.set_webhook(
                    f"{settings.public_url}/telegram/webhook",
                    settings.telegram_webhook_secret,
                )
                logger.info(
                    "Telegram webhook registered at %s/telegram/webhook",
                    settings.public_url,
                )
            else:
                logger.warning(
                    "Telegram webhook not registered: PUBLIC_URL must be "
                    "https:// (currently %s).",
                    settings.public_url,
                )
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning("Could not configure Telegram bot: %s", exc)
    yield
    await manager.close()
    if telegram_bot is not None:
        await telegram_bot.client.aclose()


app = FastAPI(title="Lucky Bingo", version="0.2.0", lifespan=lifespan)


@app.exception_handler(repository.NotFoundError)
async def not_found_handler(_: Request, exc: repository.NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(repository.ConflictError)
async def conflict_handler(_: Request, exc: repository.ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    if telegram_bot is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    update = await request.json()
    try:
        await telegram_bot.handle_update(update)
    except Exception as exc:  # noqa: BLE001 - never let a bad update retry-storm Telegram
        logger.warning("Error handling Telegram update: %s", exc)
    return {"ok": True}


async def authenticated_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        telegram_id = read_session_token(authorization[7:], settings.app_secret)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user = repository.get_user_by_telegram_id(telegram_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Player account not found")
    return user


async def authenticated_admin(
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    if not settings.is_admin(user["telegram_id"]):
        raise HTTPException(
            status_code=403, detail="Lucky administrator access required"
        )
    return user


async def require_admin(x_admin_key: Annotated[str | None, Header()] = None) -> None:
    if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_key):
        raise HTTPException(status_code=403, detail="Valid X-Admin-Key header required")


@app.get("/health")
async def health() -> dict[str, str]:
    with connect() as connection:
        connection.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return {
        "allow_dev_auth": settings.allow_dev_auth,
        "bot_username": settings.bot_username,
        "public_url": settings.public_url,
        "brand_name": "Lucky",
        "real_money_enabled": settings.enable_real_money,
        "auto_start_delay_seconds": settings.auto_start_delay_seconds,
        "result_confirmation_seconds": settings.result_confirmation_seconds,
        "test_single_player_start": settings.test_single_player_start,
        "minimum_deposit_santim": settings.minimum_deposit_santim,
        "minimum_withdrawal_santim": settings.minimum_withdrawal_santim,
        "minimum_transfer_santim": settings.minimum_transfer_santim,
        "signup_bonus_santim": settings.signup_bonus_santim,
    }


@app.post("/api/auth")
async def authenticate(payload: AuthRequest) -> dict[str, Any]:
    if payload.init_data:
        try:
            telegram_user = validate_init_data(payload.init_data, settings.bot_token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    elif settings.allow_dev_auth:
        telegram_user = TelegramUser(
            telegram_id=payload.dev_user_id or 999_000,
            first_name=payload.dev_first_name.strip() or "Demo Player",
            username="demo_player",
        )
    else:
        raise HTTPException(
            status_code=401, detail="Open this application inside Telegram"
        )

    user = repository.upsert_user(telegram_user, payload.referral_telegram_id)
    return {
        "token": create_session_token(user["telegram_id"], settings.app_secret),
        "user": public_user(user),
        "signup_bonus_santim": user["signup_bonus_granted_santim"],
    }


@app.get("/api/me")
async def me(
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    return public_user(user)


@app.get("/api/rooms")
async def rooms(
    _: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> list[dict[str, Any]]:
    return repository.list_rooms()


@app.post("/api/rooms/{room_id}/join")
async def join_room(
    room_id: int,
    payload: JoinRequest,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    requested = payload.card_numbers
    if not requested and payload.card_number is not None:
        requested = [payload.card_number]
    repository.join_cards(room_id, user["id"], requested or None)
    room = repository.get_room(room_id)
    if room["state"] == "waiting" and room["auto_start_min_players"]:
        room = manager.schedule_auto_start(room_id)
    await manager.broadcast(
        room_id,
        {
            "type": "player_joined",
            "player_count": room["player_count"],
            "unique_player_count": room["unique_player_count"],
            "sold_card_numbers": repository.sold_card_numbers(room_id),
            "room": room,
        },
    )
    return repository.game_state(room_id, user["id"])


@app.post("/api/rooms/{room_id}/cards/cancel")
async def cancel_cards(
    room_id: int,
    payload: CancelCardsRequest,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    repository.cancel_cards(room_id, user["id"], payload.card_ids)
    room = repository.get_room(room_id)
    if room["state"] == "waiting" and room["auto_start_min_players"]:
        room = manager.schedule_auto_start(room_id)
    await manager.broadcast(
        room_id,
        {
            "type": "cards_cancelled",
            "player_count": room["player_count"],
            "unique_player_count": room["unique_player_count"],
            "sold_card_numbers": repository.sold_card_numbers(room_id),
            "room": room,
        },
    )
    return repository.game_state(room_id, user["id"])


@app.get("/api/rooms/{room_id}/available-cards")
async def room_available_cards(
    room_id: int, user: Annotated[dict[str, Any], Depends(authenticated_user)]
) -> dict[str, Any]:
    return {
        "available": repository.available_card_numbers(room_id),
        "owned": [
            card["card_number"] for card in repository.get_cards(room_id, user["id"])
        ],
        "maximum": repository.MAX_CARDS_PER_PLAYER,
    }


@app.get("/api/rooms/{room_id}/cards/{card_number}/preview")
async def room_card_preview(
    room_id: int,
    card_number: int,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    return repository.preview_card(room_id, user["id"], card_number)


@app.get("/api/rooms/{room_id}/game")
async def room_game(
    room_id: int, user: Annotated[dict[str, Any], Depends(authenticated_user)]
) -> dict[str, Any]:
    return repository.game_state(room_id, user["id"])


@app.post("/api/rooms/{room_id}/mode")
async def card_mode(
    room_id: int,
    payload: AutoMarkRequest,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> list[dict[str, Any]]:
    return repository.set_auto_mark(room_id, user["id"], payload.enabled)


@app.post("/api/rooms/{room_id}/mark")
async def mark_card(
    room_id: int,
    payload: MarkRequest,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    return repository.mark_number(
        room_id, user["id"], payload.number, payload.card_id
    )


@app.post("/api/rooms/{room_id}/claim")
async def claim(
    room_id: int,
    payload: ClaimRequest,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    accepted = repository.claim_bingo(room_id, user["id"], payload.card_id)
    room = repository.get_room(room_id)
    cards = repository.get_cards(room_id, user["id"])
    claimed_card = next(
        (card for card in cards if card["id"] == payload.card_id),
        cards[0] if cards else None,
    )
    if room["result_status"] == "pending":
        manager.schedule_result_resolution(room_id)
        await manager.broadcast(
            room_id,
            {
                "type": "bingo_pending",
                "room": room,
                "winners": repository.get_round_winners(room_id),
            },
        )
    return {
        "accepted": accepted,
        "card": claimed_card,
        "outcome": room["outcome"],
        "winner_count": room["winner_count"],
        "result_status": room["result_status"],
        "result_deadline_at": room["result_deadline_at"],
    }


@app.post("/api/rooms/{room_id}/dispute")
async def dispute_result(
    room_id: int,
    payload: DisputeRequest,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    outcome = repository.dispute_round(room_id, user["id"], payload.reason)
    outcome["disputed_by_me"] = True
    repository.ensure_tier_rooms()
    await manager.broadcast(room_id, outcome)
    return outcome


@app.get("/api/leaderboard")
async def get_leaderboard(
    _: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> list[dict[str, Any]]:
    return repository.leaderboard()


@app.get("/api/wallet")
async def get_wallet(
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    return repository.wallet_summary(user["id"])


@app.get("/api/payment-instructions")
async def payment_instructions(
    _: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    return {
        "telebirr_account": settings.telebirr_account,
        "cbe_birr_account": settings.cbe_birr_account,
        "cbe_bank_account": settings.cbe_bank_account,
        "telebirr_account_name": settings.telebirr_account_name,
        "cbe_account_name": settings.cbe_account_name,
        "minimum_deposit_santim": settings.minimum_deposit_santim,
        "minimum_withdrawal_santim": settings.minimum_withdrawal_santim,
    }


@app.post("/api/deposits")
async def create_deposit(
    payload: DepositRequest,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    submitted = repository.submit_deposit(
        user["id"],
        payload.amount_santim,
        payload.transaction_id,
        payload.provider,
        payload.receipt_file_id,
    )
    deposit = repository.get_deposit(submitted["id"])
    if settings.bot_token:
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Approve",
                        "callback_data": f"dep:approve:{deposit['id']}",
                    },
                    {
                        "text": "❌ Reject",
                        "callback_data": f"dep:reject:{deposit['id']}",
                    },
                ]
            ]
        }
        text = (
            "💳 New Lucky deposit request\n\n"
            f"Player: {deposit['first_name']}\n"
            f"Telegram ID: {deposit['telegram_id']}\n"
            f"Provider: {repository.provider_label(deposit['provider'])}\n"
            f"Amount: {deposit['amount_santim'] / 100:.2f} birr\n"
            f"Transaction ID: {deposit['transaction_id']}\n"
            f"Request: #{deposit['id']}\n\n"
            "Verify this reference in the banking application before approval."
        )
        for admin_id in settings.admin_telegram_ids:
            await notify_telegram(admin_id, text, keyboard)
    return deposit


@app.post("/api/withdrawals")
async def create_withdrawal(
    payload: WithdrawalRequest,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    submitted = repository.submit_withdrawal(
        user["id"],
        payload.amount_santim,
        payload.provider,
        payload.account_number,
        payload.account_name,
    )
    withdrawal = repository.get_withdrawal(submitted["id"])
    if settings.bot_token:
        text = (
            ("🧪 TEST MODE — DO NOT SEND MONEY\n\n" if not settings.enable_real_money else "")
            + "💸 New Lucky withdrawal request\n\n"
            f"Player: {withdrawal['first_name']}\n"
            f"Telegram ID: {withdrawal['telegram_id']}\n"
            f"Amount: {withdrawal['amount_santim'] / 100:.2f} birr\n"
            f"Provider: {repository.provider_label(withdrawal['provider'])}\n"
            f"Account: {withdrawal['account_number']}\n"
            f"Account name: {withdrawal['account_name']}\n"
            f"Request: #{withdrawal['id']}\n\n"
            "Send the money first, then enter the payout transaction reference "
            "and approve it in Lucky Admin."
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "Review in Lucky Admin",
                        "web_app": {"url": f"{settings.public_url}/admin"},
                    }
                ]
            ]
        }
        for admin_id in settings.admin_telegram_ids:
            await notify_telegram(admin_id, text, keyboard)
    return withdrawal


@app.get("/api/users/lookup/{telegram_id}")
async def lookup_user(
    telegram_id: int,
    _: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    found = repository.find_user_by_telegram_id(telegram_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No Lucky player with that Telegram ID")
    return public_user(found)


@app.post("/api/transfers")
async def create_transfer(
    payload: TransferRequest,
    user: Annotated[dict[str, Any], Depends(authenticated_user)],
) -> dict[str, Any]:
    submitted = repository.submit_transfer(
        user["id"], payload.recipient_telegram_id, payload.amount_santim
    )
    transfer = repository.get_transfer(submitted["id"])
    if settings.bot_token:
        text = (
            "🔁 New Lucky transfer request\n\n"
            f"From: {transfer['sender_first_name']} (Telegram {transfer['sender_telegram_id']})\n"
            f"To: {transfer['recipient_first_name']} (Telegram {transfer['recipient_telegram_id']})\n"
            f"Amount: {transfer['amount_santim'] / 100:.2f} birr\n"
            f"Request: #{transfer['id']}\n\n"
            "Review and approve or reject it in Lucky Admin."
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "Review in Lucky Admin",
                        "web_app": {"url": f"{settings.public_url}/admin"},
                    }
                ]
            ]
        }
        for admin_id in settings.admin_telegram_ids:
            await notify_telegram(admin_id, text, keyboard)
    return transfer


@app.post("/api/admin/rooms", dependencies=[Depends(require_admin)])
async def admin_create_room(payload: RoomCreateRequest) -> dict[str, Any]:
    return repository.create_room(
        payload.name,
        max_players=payload.max_players,
        auto_start_min_players=payload.auto_start_min_players,
        call_interval_seconds=payload.call_interval_seconds,
        stake_santim=payload.stake_santim,
        transfer_cost_santim=payload.transfer_cost_santim,
        card_capacity=400,
    )


@app.post("/api/admin/rooms/{room_id}/start", dependencies=[Depends(require_admin)])
async def admin_start_room(room_id: int) -> dict[str, Any]:
    await manager.start(room_id)
    return repository.get_room(room_id)


@app.get("/api/control/dashboard")
async def control_dashboard(
    _: Annotated[dict[str, Any], Depends(authenticated_admin)],
) -> dict[str, Any]:
    return {
        "rooms": repository.list_rooms(),
        "real_money_enabled": settings.enable_real_money,
        "pending_deposits": repository.list_deposits("pending"),
        "pending_withdrawals": repository.list_withdrawals("pending"),
        "pending_transfers": repository.list_transfers("pending"),
        "revenue": repository.revenue_summary(),
    }


@app.get("/api/control/evidence")
async def control_evidence_rounds(
    _: Annotated[dict[str, Any], Depends(authenticated_admin)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 10,
    search: Annotated[str, Query(max_length=40)] = "",
) -> dict[str, Any]:
    return repository.paginate_evidence_rounds(page, page_size, search)


@app.get("/api/control/rooms/{room_id}/sold-cards")
async def control_sold_cards(
    room_id: int,
    _: Annotated[dict[str, Any], Depends(authenticated_admin)],
) -> dict[str, list[int]]:
    return {"sold_card_numbers": repository.sold_card_numbers(room_id)}


@app.get("/api/control/rooms/{room_id}/evidence")
async def control_round_evidence(
    room_id: int,
    _: Annotated[dict[str, Any], Depends(authenticated_admin)],
) -> dict[str, Any]:
    evidence = repository.get_round_evidence(room_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="No result evidence exists yet")
    return evidence


@app.post("/api/control/rooms/{room_id}/start")
async def control_start_room(
    room_id: int,
    _: Annotated[dict[str, Any], Depends(authenticated_admin)],
) -> dict[str, Any]:
    await manager.start(room_id)
    return repository.get_room(room_id)


@app.post("/api/control/deposits/{deposit_id}/review")
async def control_review_deposit(
    deposit_id: int,
    payload: DepositReviewRequest,
    admin: Annotated[dict[str, Any], Depends(authenticated_admin)],
) -> dict[str, Any]:
    return repository.review_deposit(
        deposit_id, admin["id"], payload.approve, payload.note
    )


@app.post("/api/control/withdrawals/{withdrawal_id}/review")
async def control_review_withdrawal(
    withdrawal_id: int,
    payload: WithdrawalReviewRequest,
    admin: Annotated[dict[str, Any], Depends(authenticated_admin)],
) -> dict[str, Any]:
    if payload.approve and not settings.enable_real_money:
        raise HTTPException(
            status_code=409,
            detail="Withdrawal approval is disabled while test mode is active",
        )
    withdrawal = repository.review_withdrawal(
        withdrawal_id,
        admin["id"],
        payload.approve,
        payload.payout_reference,
        payload.note,
    )
    if settings.bot_token:
        status = withdrawal["status"]
        text = f"Your Lucky withdrawal #{withdrawal['id']} was {status}."
        if status == "approved":
            text += (
                f" {withdrawal['amount_santim'] / 100:.2f} birr was sent to "
                f"your {repository.provider_label(withdrawal['provider'])} account."
            )
        else:
            text += " The reserved amount is available in your Lucky balance again."
        await notify_telegram(withdrawal["telegram_id"], text)
    return withdrawal


@app.post("/api/control/transfers/{transfer_id}/review")
async def control_review_transfer(
    transfer_id: int,
    payload: TransferReviewRequest,
    admin: Annotated[dict[str, Any], Depends(authenticated_admin)],
) -> dict[str, Any]:
    transfer = repository.review_transfer(
        transfer_id, admin["id"], payload.approve, payload.note
    )
    if settings.bot_token:
        status = transfer["status"]
        sender_text = f"Your Lucky transfer #{transfer['id']} was {status}."
        if status == "approved":
            sender_text += (
                f" {transfer['amount_santim'] / 100:.2f} birr was sent to "
                f"{transfer['recipient_first_name']}."
            )
        else:
            sender_text += " The reserved amount is available in your Lucky balance again."
        await notify_telegram(transfer["sender_telegram_id"], sender_text)
        if status == "approved":
            recipient_text = (
                f"💰 {transfer['amount_santim'] / 100:.2f} birr was added to your "
                f"Lucky balance by {transfer['sender_first_name']}."
            )
            await notify_telegram(transfer["recipient_telegram_id"], recipient_text)
    return transfer


@app.websocket("/ws/rooms/{room_id}")
async def room_socket(websocket: WebSocket, room_id: int, token: str) -> None:
    try:
        telegram_id = read_session_token(token, settings.app_secret)
        user = repository.get_user_by_telegram_id(telegram_id)
        if user is None:
            raise AuthenticationError("Player account not found")
        repository.get_room(room_id)
    except (AuthenticationError, repository.NotFoundError):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    await manager.subscribe(room_id, websocket)
    await websocket.send_json(
        {"type": "connected", "state": repository.game_state(room_id, user["id"])}
    )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.unsubscribe(room_id, websocket)


STATIC_DIRECTORY = __import__("pathlib").Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIRECTORY), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        STATIC_DIRECTORY / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/privacy", include_in_schema=False)
async def privacy() -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "privacy.html")


@app.get("/admin", include_in_schema=False)
async def admin_index() -> FileResponse:
    return FileResponse(STATIC_DIRECTORY / "admin.html")
