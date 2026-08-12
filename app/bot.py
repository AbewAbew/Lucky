from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from app import repository
from app.auth import TelegramUser
from app.config import settings

logger = logging.getLogger("lucky.bot")
# httpx logs complete request URLs at INFO level. Telegram embeds the bot token in
# that URL, so keep the transport logger quiet to prevent credential disclosure.
# This module is now imported into the FastAPI process (app.main), which owns
# the actual logging.basicConfig() setup — this file only configures its own
# scoped loggers, never the root logger.
logging.getLogger("httpx").setLevel(logging.WARNING)

MENU_COMMANDS = {
    "🎱 Play Bingo": "/play",
    "💰 Deposit": "/deposit",
    "💸 Withdraw": "/withdraw",
    "🏦 Transfer": "/transfer",
    "💳 Balance": "/balance",
    "📜 Transactions": "/transactions",
    "ℹ️ Info": "/help",
    "🎁 Invite": "/invite",
    "🛡 Admin": "/admin",
}
MINI_APP_VERSION = "20260810-game-balance-v26"


def versioned_web_app_url(url: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={MINI_APP_VERSION}"


class TelegramBot:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=40)

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        response = await self.client.post(
            f"{self.base_url}/{method}", json=payload or {}
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", "Telegram request failed"))
        return body.get("result")

    async def set_webhook(self, url: str, secret_token: str) -> None:
        await self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "drop_pending_updates": False,
            },
        )

    async def configure(self) -> None:
        await self.call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Open Lucky"},
                    {"command": "play", "description": "Find a game"},
                    {"command": "balance", "description": "View Lucky balance"},
                    {"command": "deposit", "description": "Deposit instructions"},
                    {"command": "pay", "description": "Submit a payment reference"},
                    {"command": "withdraw", "description": "Request a withdrawal"},
                    {"command": "transactions", "description": "Recent transactions"},
                    {"command": "menu", "description": "Show the Lucky menu"},
                    {"command": "myid", "description": "Show your Telegram ID"},
                    {"command": "admin", "description": "Administrator board"},
                    {"command": "help", "description": "How to play"},
                    {"command": "paysupport", "description": "Payment support"},
                ]
            },
        )
        if settings.public_url.startswith("https://"):
            await self.call(
                "setChatMenuButton",
                {
                    "menu_button": {
                        "type": "web_app",
                        "text": "Play Lucky",
                        "web_app": {"url": versioned_web_app_url(settings.public_url)},
                    }
                },
            )

    @staticmethod
    def menu_keyboard(
        telegram_id: int, web_app_url: str | None = None
    ) -> dict[str, Any]:
        play_button: dict[str, Any] = {"text": "🎱 Play Bingo"}
        launch_url = web_app_url or settings.public_url
        if launch_url.startswith("https://"):
            play_button["web_app"] = {"url": versioned_web_app_url(launch_url)}
        rows: list[list[dict[str, Any]]] = [
            [play_button],
            [
                {"text": "💰 Deposit"},
                {"text": "💸 Withdraw"},
                {"text": "🏦 Transfer"},
            ],
            [{"text": "💳 Balance"}, {"text": "📜 Transactions"}],
            [{"text": "ℹ️ Info"}, {"text": "🎁 Invite"}],
        ]
        if settings.is_admin(telegram_id):
            rows.append([{"text": "🛡 Admin"}])
        return {
            "keyboard": rows,
            "resize_keyboard": True,
            "is_persistent": True,
            "input_field_placeholder": "Choose a Lucky action",
        }

    async def send_launcher(
        self,
        chat_id: int,
        first_name: str,
        telegram_id: int,
        payload: str = "",
    ) -> None:
        url = settings.public_url
        if payload.startswith(("room_", "ref_")):
            url = f"{url}/?startapp={quote(payload)}"
        bonus_line = (
            f"🎁 New players get a free {settings.signup_bonus_santim / 100:g} birr "
            "bonus to play with! It can't be withdrawn, but any winnings from it can.\n\n"
            if settings.signup_bonus_santim > 0
            else ""
        )
        if not url.startswith("https://"):
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        f"Hi {first_name}! 🎱\n\n"
                        f"{bonus_line}"
                        "Lucky is connected to this bot. The Mini App launch button "
                        "will appear after its public HTTPS address is configured."
                    ),
                    "reply_markup": self.menu_keyboard(telegram_id, url),
                },
            )
            return
        await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"Hi {first_name}! 🎱\n\n"
                    f"{bonus_line}"
                    "Your Lucky card is waiting. Choose a tier, follow the live calls, "
                    "and tap BINGO when you complete a line. Use the menu below anytime."
                ),
                "reply_markup": self.menu_keyboard(telegram_id, url),
            },
        )

    @staticmethod
    def register_user(raw_user: dict[str, Any]) -> dict[str, Any]:
        return repository.upsert_user(
            TelegramUser(
                telegram_id=int(raw_user["id"]),
                first_name=raw_user.get("first_name") or "Player",
                last_name=raw_user.get("last_name"),
                username=raw_user.get("username"),
                language_code=raw_user.get("language_code"),
            )
        )

    async def notify_admins(self, deposit: dict[str, Any]) -> None:
        username = f" (@{deposit['username']})" if deposit.get("username") else ""
        text = (
            "💳 New Lucky deposit request\n\n"
            f"Player: {deposit['first_name']}{username}\n"
            f"Telegram ID: {deposit['telegram_id']}\n"
            f"Provider: {repository.provider_label(deposit['provider'])}\n"
            f"Amount: {deposit['amount_santim'] / 100:.2f} birr\n"
            f"Transaction ID: {deposit['transaction_id']}\n"
            f"Request: #{deposit['id']}\n\n"
            "Verify this reference in the banking application before approval."
        )
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
        for admin_id in settings.admin_telegram_ids:
            try:
                await self.call(
                    "sendMessage",
                    {"chat_id": admin_id, "text": text, "reply_markup": keyboard},
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                logger.warning("Could not notify admin %s: %s", admin_id, exc)

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        raw_user = callback.get("from", {})
        telegram_id = int(raw_user.get("id", 0))
        if not settings.is_admin(telegram_id):
            await self.call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": "Administrator access required",
                    "show_alert": True,
                },
            )
            return

        parts = callback.get("data", "").split(":")
        if (
            len(parts) != 3
            or parts[0] != "dep"
            or parts[1] not in {"approve", "reject"}
        ):
            await self.call("answerCallbackQuery", {"callback_query_id": callback_id})
            return

        admin = self.register_user(raw_user)
        try:
            deposit = repository.review_deposit(
                int(parts[2]), admin["id"], parts[1] == "approve"
            )
            status = deposit["status"]
            await self.call(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": f"Deposit {status}"},
            )
            message = callback.get("message")
            if message:
                await self.call(
                    "editMessageReplyMarkup",
                    {
                        "chat_id": message["chat"]["id"],
                        "message_id": message["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
            result_text = f"Your deposit #{deposit['id']} was {status}."
            if status == "approved":
                result_text += (
                    f" {deposit['amount_santim'] / 100:.2f} birr was added to "
                    "your Lucky balance."
                )
            else:
                result_text += " Contact support if you believe this is a mistake."
            await self.call(
                "sendMessage",
                {"chat_id": deposit["telegram_id"], "text": result_text},
            )
        except (repository.NotFoundError, repository.ConflictError) as exc:
            await self.call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_id,
                    "text": str(exc),
                    "show_alert": True,
                },
            )

    @staticmethod
    def parse_amount(value: str) -> int:
        amount = Decimal(value)
        if amount.as_tuple().exponent < -2:
            raise ValueError("Use no more than two decimal places")
        return int(amount * 100)

    async def handle_message(self, message: dict[str, Any]) -> None:
        if "text" not in message:
            return
        text = message["text"].strip()
        chat_id = message["chat"]["id"]
        raw_user = message.get("from", {})
        first_name = raw_user.get("first_name", "there")
        user = self.register_user(raw_user)
        command, _, payload = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        command = MENU_COMMANDS.get(text, command)

        if command in {"/start", "/play", "/menu"}:
            await self.send_launcher(
                chat_id, first_name, user["telegram_id"], payload.strip()
            )
        elif command == "/balance":
            wallet = repository.wallet_summary(user["id"], limit=5)
            reserved = wallet["reserved_withdrawal_santim"]
            reserved_copy = (
                f"\nReserved withdrawals: {reserved / 100:.2f} birr"
                if reserved
                else ""
            )
            bonus_copy = (
                f"\nIncludes {wallet['bonus_santim'] / 100:.2f} birr welcome bonus "
                f"(not withdrawable). Withdrawable: {wallet['withdrawable_balance_santim'] / 100:.2f} birr"
                if wallet["bonus_santim"]
                else ""
            )
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        f"💰 Available Lucky balance: "
                        f"{wallet['balance_santim'] / 100:.2f} birr{reserved_copy}{bonus_copy}"
                    ),
                },
            )
        elif command == "/deposit":
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        "Deposit to one of these accounts:\n\n"
                        f"Telebirr: {settings.telebirr_account}\n"
                        f"CBE Birr: {settings.cbe_birr_account}\n"
                        f"CBE Bank Account: {settings.cbe_bank_account}\n"
                        f"Account name: {settings.payment_account_name}\n\n"
                        f"Minimum deposit: {settings.minimum_deposit_santim / 100:.2f} birr\n\n"
                        "After sending, submit:\n"
                        "/pay amount transactionID provider\n\n"
                        "Example: /pay 50 ABC123456 telebirr\n"
                        "Provider is telebirr, cbe, or cbe_account, and defaults to telebirr."
                    ),
                },
            )
        elif command == "/pay":
            parts = payload.split()
            if len(parts) not in {2, 3}:
                await self.call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": "Use: /pay amount transactionID [telebirr|cbe]",
                    },
                )
                return
            try:
                amount_santim = self.parse_amount(parts[0])
                provider = parts[2].lower() if len(parts) == 3 else "telebirr"
                submitted = repository.submit_deposit(
                    user["id"], amount_santim, parts[1], provider
                )
                full_deposit = repository.get_deposit(submitted["id"])
                await self.notify_admins(full_deposit)
                await self.call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": (
                            f"Deposit #{submitted['id']} submitted for review. "
                            "Your balance will update after an administrator verifies it."
                        ),
                    },
                )
            except (InvalidOperation, ValueError, repository.ConflictError) as exc:
                await self.call("sendMessage", {"chat_id": chat_id, "text": str(exc)})
        elif command == "/transactions":
            wallet = repository.wallet_summary(user["id"], limit=10)
            lines = [
                f"{entry['amount_santim'] / 100:+.2f} birr · {entry['description']}"
                for entry in wallet["entries"]
            ]
            lines.extend(
                f"-{item['amount_santim'] / 100:.2f} birr · "
                f"{repository.provider_label(item['provider'])} withdrawal {item['status']}"
                for item in wallet["withdrawals"]
            )
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Recent Lucky transactions:\n"
                    + ("\n".join(lines) if lines else "No transactions yet."),
                },
            )
        elif command == "/withdraw":
            wallet = repository.wallet_summary(user["id"], limit=5)
            withdrawal_url = versioned_web_app_url(
                f"{settings.public_url}/?startapp=withdraw"
            )
            button: dict[str, Any] = {"text": "💸 Open withdrawal form"}
            if settings.public_url.startswith("https://"):
                button["web_app"] = {"url": withdrawal_url}
                reply_markup = {"inline_keyboard": [[button]]}
            else:
                reply_markup = self.menu_keyboard(user["telegram_id"])
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        f"Withdrawable balance: {wallet['withdrawable_balance_santim'] / 100:.2f} birr\n"
                        f"Minimum withdrawal: {settings.minimum_withdrawal_santim / 100:.2f} birr\n\n"
                        "Pending withdrawal money is reserved until an administrator "
                        "approves or rejects the request."
                    ),
                    "reply_markup": reply_markup,
                },
            )
        elif command == "/transfer":
            wallet = repository.wallet_summary(user["id"], limit=5)
            transfer_url = versioned_web_app_url(
                f"{settings.public_url}/?startapp=transfer"
            )
            button = {"text": "🏦 Open transfer form"}
            if settings.public_url.startswith("https://"):
                button["web_app"] = {"url": transfer_url}
                reply_markup = {"inline_keyboard": [[button]]}
            else:
                reply_markup = self.menu_keyboard(user["telegram_id"])
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        f"Transferable balance: {wallet['withdrawable_balance_santim'] / 100:.2f} birr\n"
                        f"Minimum transfer: {settings.minimum_transfer_santim / 100:.2f} birr\n\n"
                        "Welcome bonus money cannot be transferred. Transfers are reviewed "
                        "by an administrator before the recipient's balance updates."
                    ),
                    "reply_markup": reply_markup,
                },
            )
        elif command == "/invite":
            username = settings.bot_username.lstrip("@")
            invite_url = f"https://t.me/{username}?start=ref_{user['telegram_id']}"
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": f"Invite friends to Lucky with your personal link:\n{invite_url}",
                    "reply_markup": self.menu_keyboard(user["telegram_id"]),
                },
            )
        elif command == "/myid":
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": f"Your Telegram numeric ID is: {user['telegram_id']}",
                },
            )
        elif command == "/admin":
            if not settings.is_admin(user["telegram_id"]):
                await self.call(
                    "sendMessage",
                    {"chat_id": chat_id, "text": "Administrator access required."},
                )
                return
            if not settings.public_url.startswith("https://"):
                await self.call(
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": (
                            "You are authorized as a Lucky administrator. The admin "
                            "board will become available after the public HTTPS address "
                            "is configured."
                        ),
                    },
                )
                return
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Open the protected Lucky live board:",
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "🛡 Lucky Admin",
                                    "web_app": {"url": f"{settings.public_url}/admin"},
                                }
                            ]
                        ]
                    },
                },
            )
        elif command == "/help":
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        "How to play:\n"
                        "1. Open Lucky and choose a 2, 5, or 10 birr room.\n"
                        "2. Select one of 400 card numbers.\n"
                        "3. Mark each called number, or enable Auto.\n"
                        "4. Complete a row, column, or diagonal and tap BINGO."
                    ),
                },
            )
        elif command == "/paysupport":
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": (
                        "Send your deposit request number and transaction ID to a "
                        "Lucky administrator for payment assistance."
                    ),
                },
            )

    async def handle_update(self, update: dict[str, Any]) -> None:
        if callback := update.get("callback_query"):
            await self.handle_callback(callback)
        elif message := update.get("message"):
            await self.handle_message(message)


if __name__ == "__main__":
    # The bot no longer polls Telegram as a standalone process. It now runs
    # inside the FastAPI app (app.main) as a webhook handler at
    # POST /telegram/webhook, registered automatically on startup via
    # TelegramBot.set_webhook(). Start the app with uvicorn instead:
    #   uvicorn app.main:app --host 0.0.0.0 --port 8000
    raise SystemExit(
        "python -m app.bot is no longer used. The Telegram bot now runs inside "
        "the FastAPI process via a webhook — start the app with "
        "'uvicorn app.main:app' instead. See DEPLOYMENT_MIGRATION.md."
    )
