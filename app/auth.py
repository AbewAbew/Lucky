from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramUser:
    telegram_id: int
    first_name: str
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None
    language_code: str | None = None


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86_400,
    now: int | None = None,
) -> TelegramUser:
    if not init_data or not bot_token:
        raise AuthenticationError("Telegram authentication data is unavailable")

    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise AuthenticationError("Telegram authentication hash is missing")

    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise AuthenticationError("Telegram authentication signature is invalid")

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError(
            "Telegram authentication timestamp is invalid"
        ) from exc
    current_time = int(time.time()) if now is None else now
    if auth_date > current_time + 30 or current_time - auth_date > max_age_seconds:
        raise AuthenticationError("Telegram authentication data has expired")

    try:
        raw_user = json.loads(fields["user"])
        return TelegramUser(
            telegram_id=int(raw_user["id"]),
            first_name=str(raw_user["first_name"]),
            last_name=raw_user.get("last_name"),
            username=raw_user.get("username"),
            photo_url=raw_user.get("photo_url"),
            language_code=raw_user.get("language_code"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("Telegram user data is invalid") from exc


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session_token(
    telegram_id: int, secret: str, ttl_seconds: int = 86_400
) -> str:
    payload = json.dumps(
        {"telegram_id": telegram_id, "exp": int(time.time()) + ttl_seconds},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _b64encode(payload)
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def read_session_token(token: str, secret: str, *, now: int | None = None) -> int:
    try:
        encoded, provided_signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64decode(provided_signature)):
            raise AuthenticationError("Session signature is invalid")
        payload = json.loads(_b64decode(encoded))
        current_time = int(time.time()) if now is None else now
        if int(payload["exp"]) < current_time:
            raise AuthenticationError("Session has expired")
        return int(payload["telegram_id"])
    except AuthenticationError:
        raise
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("Session token is invalid") from exc
