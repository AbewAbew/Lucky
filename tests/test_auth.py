import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from app.auth import (
    AuthenticationError,
    create_session_token,
    read_session_token,
    validate_init_data,
)


def make_init_data(
    bot_token: str,
    auth_date: int = 1_800_000_000,
    *,
    include_signature: bool = False,
) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(
            {"id": 42, "first_name": "Ada", "username": "ada"}, separators=(",", ":")
        ),
    }
    if include_signature:
        fields["signature"] = "telegram-ed25519-signature"
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_validates_telegram_init_data() -> None:
    user = validate_init_data(make_init_data("123:abc"), "123:abc", now=1_800_000_100)
    assert user.telegram_id == 42
    assert user.first_name == "Ada"


def test_validates_current_init_data_with_third_party_signature() -> None:
    user = validate_init_data(
        make_init_data("123:abc", include_signature=True),
        "123:abc",
        now=1_800_000_100,
    )
    assert user.telegram_id == 42


def test_rejects_tampered_telegram_init_data() -> None:
    tampered = make_init_data("123:abc").replace("Ada", "Eve")
    with pytest.raises(AuthenticationError):
        validate_init_data(tampered, "123:abc", now=1_800_000_100)


def test_session_tokens_are_signed_and_expire() -> None:
    token = create_session_token(42, "secret", ttl_seconds=60)
    assert read_session_token(token, "secret") == 42
    with pytest.raises(AuthenticationError):
        read_session_token(token, "wrong-secret")
    with pytest.raises(AuthenticationError):
        read_session_token(token, "secret", now=9_000_000_000)
