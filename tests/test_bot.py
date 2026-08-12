from app.bot import MENU_COMMANDS, TelegramBot
from app.config import settings


def _labels(keyboard: dict) -> list[str]:
    return [button["text"] for row in keyboard["keyboard"] for button in row]


def test_player_menu_has_launcher_and_expected_actions() -> None:
    keyboard = TelegramBot.menu_keyboard(42, "https://lucky.example")
    labels = _labels(keyboard)
    assert keyboard["is_persistent"] is True
    assert keyboard["keyboard"][0][0]["web_app"]["url"] == (
        "https://lucky.example?v=20260810-game-balance-v26"
    )
    assert "💰 Deposit" in labels
    assert "💳 Balance" in labels
    assert "🎁 Invite" in labels
    assert "🛡 Admin" not in labels
    assert MENU_COMMANDS["💸 Withdraw"] == "/withdraw"


def test_authorized_admin_receives_admin_menu_button() -> None:
    keyboard = TelegramBot.menu_keyboard(settings.admin_telegram_ids[0])
    assert "🛡 Admin" in _labels(keyboard)
