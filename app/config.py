from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_ids(value: str | None) -> tuple[int, ...]:
    if not value:
        return (999_000, 999_001, 999_002)
    return tuple(int(item.strip()) for item in value.split(",") if item.strip())[:3]


def _birr_to_santim(value: str | None) -> int:
    return int(Decimal(value or "0") * 100)


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    bot_username: str = os.getenv("BOT_USERNAME", "")
    telegram_webhook_secret: str = os.getenv(
        "TELEGRAM_WEBHOOK_SECRET", "dev-only-webhook-secret-change-me"
    )
    public_url: str = os.getenv("PUBLIC_URL", "http://localhost:8000").rstrip("/")
    app_secret: str = os.getenv("APP_SECRET", "dev-only-secret-change-me")
    admin_key: str = os.getenv("ADMIN_KEY", "dev-admin")
    database_url: str = os.getenv("DATABASE_URL", "")
    allow_dev_auth: bool = _as_bool(os.getenv("ALLOW_DEV_AUTH"), True)
    call_interval_seconds: float = float(os.getenv("CALL_INTERVAL_SECONDS", "3"))
    auto_start_delay_seconds: float = float(os.getenv("AUTO_START_DELAY_SECONDS", "20"))
    result_confirmation_seconds: float = float(
        os.getenv("RESULT_CONFIRMATION_SECONDS", "15")
    )
    test_single_player_start: bool = _as_bool(
        os.getenv("TEST_SINGLE_PLAYER_START"), False
    )
    enable_real_money: bool = _as_bool(os.getenv("ENABLE_REAL_MONEY"), False)
    admin_telegram_ids: tuple[int, ...] = _as_ids(os.getenv("ADMIN_TELEGRAM_IDS"))
    telebirr_account: str = os.getenv("TELEBIRR_ACCOUNT", "Not configured")
    cbe_birr_account: str = os.getenv("CBE_BIRR_ACCOUNT", "Not configured")
    cbe_bank_account: str = os.getenv("CBE_BANK_ACCOUNT", "Not configured")
    payment_account_name: str = os.getenv("PAYMENT_ACCOUNT_NAME", "Lucky Bingo")
    minimum_deposit_santim: int = _birr_to_santim(
        os.getenv("MINIMUM_DEPOSIT_BIRR", "10")
    )
    minimum_withdrawal_santim: int = _birr_to_santim(
        os.getenv("MINIMUM_WITHDRAWAL_BIRR", "100")
    )
    minimum_transfer_santim: int = _birr_to_santim(
        os.getenv("MINIMUM_TRANSFER_BIRR", "10")
    )
    default_transfer_cost_santim: int = _birr_to_santim(
        os.getenv("DEFAULT_TRANSFER_COST_BIRR")
    )
    signup_bonus_santim: int = _birr_to_santim(os.getenv("SIGNUP_BONUS_BIRR", "10"))

    def is_admin(self, telegram_id: int) -> bool:
        return telegram_id in self.admin_telegram_ids


settings = Settings()
