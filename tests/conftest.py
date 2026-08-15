"""Pin the process environment before `app.config` is ever imported.

`app.config.Settings` reads `os.environ` (via `load_dotenv()`) exactly once,
at import time, and every module shares that one instance. Without this file,
the test suite silently inherits whatever the developer's local `.env`
happens to contain (e.g. `TEST_SINGLE_PLAYER_START=true` for manual solo
testing), so a green/red run reflects the `.env` on disk as much as it
reflects the code.

`load_dotenv()` never overwrites a variable that is already set (its
`override` defaults to False), so setting known-good values here, before any
test module imports `app.config`, makes them win regardless of `.env`.
Pytest collects `conftest.py` before importing test modules, so this runs
early enough.

`DATABASE_URL` is deliberately NOT pinned here: tests reuse the real Neon
connection string from `.env` (there's nothing local to fall back to), but
every test points `app.db` at an isolated `pytest` schema via `db_schema`
(see `setup_database()` in test_api.py / test_money.py), never the app's own
`public` schema, so this is safe.
"""

import os

_TEST_ENV = {
    "BOT_TOKEN": "",
    "BOT_USERNAME": "",
    "PUBLIC_URL": "http://localhost:8000",
    "APP_SECRET": "test-secret-do-not-use-in-production",
    "ADMIN_KEY": "test-admin-key-do-not-use-in-production",
    "ALLOW_DEV_AUTH": "true",
    "ENABLE_REAL_MONEY": "false",
    "TEST_SINGLE_PLAYER_START": "false",
    "ADMIN_TELEGRAM_IDS": "999000,999001,999002",
    "TELEBIRR_ACCOUNT": "test-telebirr-account",
    "CBE_BIRR_ACCOUNT": "test-cbe-account",
    "TELEBIRR_ACCOUNT_NAME": "Lucky Test",
    "CBE_ACCOUNT_NAME": "Lucky Test",
    "MINIMUM_DEPOSIT_BIRR": "10",
    "MINIMUM_WITHDRAWAL_BIRR": "100",
    "DEFAULT_TRANSFER_COST_BIRR": "0",
    # 0 here so existing money-math tests aren't coupled to the signup bonus;
    # tests that exercise the bonus itself opt in via `replace(settings, ...)`,
    # the same pattern already used for `enable_real_money=True`.
    "SIGNUP_BONUS_BIRR": "0",
    "CALL_INTERVAL_SECONDS": "3",
    "AUTO_START_DELAY_SECONDS": "20",
    "RESULT_CONFIRMATION_SECONDS": "6",
}

for _key, _value in _TEST_ENV.items():
    os.environ[_key] = _value
