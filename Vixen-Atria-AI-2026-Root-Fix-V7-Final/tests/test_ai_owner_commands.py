import sys
import types
from pathlib import Path
from types import SimpleNamespace

from bot.services.ai_intent import ai_intent_router

ROOT = Path(__file__).resolve().parents[1]


def _owner_actions():
    """Load ai_actions with a minimal test-only Discord import surface."""
    if "bot.services.ai_actions" in sys.modules:
        return sys.modules["bot.services.ai_actions"].owner_ai_actions
    try:
        from bot.services.ai_actions import owner_ai_actions
        return owner_ai_actions
    except ModuleNotFoundError as exc:
        if exc.name != "discord":
            raise
    stub = types.ModuleType("discord")
    class DiscordError(Exception):
        pass
    class NotFound(DiscordError):
        pass
    class HTTPException(DiscordError):
        pass
    class Forbidden(DiscordError):
        pass
    stub.NotFound = NotFound
    stub.HTTPException = HTTPException
    stub.Forbidden = Forbidden
    stub.Guild = type("Guild", (), {})
    stub.ClientUser = type("ClientUser", (), {})
    stub.Member = type("Member", (), {})
    stub.Role = type("Role", (), {})
    stub.Object = type("Object", (), {})
    stub.utils = SimpleNamespace(utcnow=lambda: None)
    previous = sys.modules.get("discord")
    sys.modules["discord"] = stub
    try:
        from bot.services.ai_actions import owner_ai_actions
        return owner_ai_actions
    finally:
        if previous is None:
            sys.modules.pop("discord", None)
        else:
            sys.modules["discord"] = previous


def _message_for(text, mentions=True):
    member = SimpleNamespace(id=123456789012345678)
    return SimpleNamespace(mentions=[member] if mentions else [])


def test_action_router_accepts_core_moderation_commands():
    cases = [
        "kick <@123456789012345678>",
        "ban <@123456789012345678>",
        "timeout <@123456789012345678> 10m",
        "warn <@123456789012345678>",
        "untimeout <@123456789012345678>",
        "mute <@123456789012345678>",
        "ميوت <@123456789012345678>",
        "تايموت <@123456789012345678> 1h",
        "اطرد <@123456789012345678>",
        "باند <@123456789012345678>",
        "حذر <@123456789012345678>",
        "فك التايموت <@123456789012345678>",
        "unban 123456789012345678",
    ]
    for text in cases:
        route = ai_intent_router.route(text, {"speed_mode": "flash"})
        assert route.intent == "owner_action", text
        assert route.confidence >= 0.99


def test_action_router_rejects_questions_and_examples():
    cases = [
        "how do I kick someone?",
        "كيفاش نعمل kick لفلان؟",
        "شنوة command متاع timeout؟",
        "I wrote \"kick @user\" as an example",
        "مثال: kick <@123456789012345678>",
        "لو نقول kick <@123456789012345678> شنو يصير؟",
        "timeout في aiohttp",
    ]
    for text in cases:
        assert ai_intent_router.route(text, {"speed_mode": "flash"}).intent != "owner_action", text


def test_direct_parser_builds_safe_member_payload_and_duration():
    actions = _owner_actions()
    payload = actions.parse_direct_command(
        "تايموت <@123456789012345678> 1h بسبب spam",
        _message_for("x"),
    )
    assert payload == {
        "execute": True,
        "action": "timeout",
        "target": "<@123456789012345678>",
        "reason": "spam",
        "minutes": 60,
    }


def test_direct_parser_supports_ban_kick_warn_and_unban():
    actions = _owner_actions()
    msg = _message_for("x")
    assert actions.parse_direct_command("kick <@123456789012345678>", msg)["action"] == "kick"
    assert actions.parse_direct_command("باند <@123456789012345678>", msg)["action"] == "ban"
    assert actions.parse_direct_command("ميوت <@123456789012345678>", msg)["action"] == "timeout"
    assert actions.parse_direct_command("حذر <@123456789012345678>", msg)["action"] == "warn"
    assert actions.parse_direct_command("unban 123456789012345678", _message_for("x", mentions=False))["action"] == "unban"


def test_direct_parser_never_executes_question_or_example():
    actions = _owner_actions()
    msg = _message_for("x")
    for text in (
        "how do I kick someone?",
        "كيفاش نعمل kick لفلان؟",
        "I wrote \"kick @user\" as an example",
        "مثال: kick <@123456789012345678>",
    ):
        assert actions.parse_direct_command(text, msg) is None


def test_action_marker_can_be_pure_machine_output():
    actions = _owner_actions()
    visible, payload = actions.parse(
        '<VIXEN_ACTION>{"execute":true,"action":"kick","target":"<@123456789012345678>"}</VIXEN_ACTION>'
    )
    assert visible == ""
    assert payload["action"] == "kick"


def test_direct_action_result_send_failure_does_not_fall_back_to_model(monkeypatch):
    import asyncio
    import importlib
    from unittest.mock import AsyncMock, patch

    # Import AIChatService against the same test-only minimal Discord surface.
    discord = sys.modules.get("discord")
    if discord is None:
        discord = types.ModuleType("discord")
        class AllowedMentions:
            @staticmethod
            def none():
                return None
        discord.AllowedMentions = AllowedMentions
        discord.HTTPException = type("HTTPException", (Exception,), {})
        sys.modules["discord"] = discord
    from bot.services.ai_chat import AIChatService
    from bot.services.ai_actions import owner_ai_actions, AIActionResult

    service = AIChatService()
    service._record_turn = AsyncMock()
    message = SimpleNamespace(
        id=88,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("Discord send failed"))),
        author=SimpleNamespace(id=42),
        mentions=[SimpleNamespace(id=123456789012345678)],
    )
    bot = SimpleNamespace()

    async def scenario():
        with patch("bot.utils.ranks.is_owner", return_value=True), patch.object(
            owner_ai_actions,
            "execute",
            new=AsyncMock(return_value=AIActionResult(True, "✅ done", action="kick")),
        ) as execute:
            handled = await service._try_direct_owner_action(
                bot, message, "kick <@123456789012345678>", {"owner_actions_enabled": True}
            )
            return handled, execute

    handled, execute = asyncio.run(scenario())
    assert handled is True
    execute.assert_awaited_once()
    service._record_turn.assert_awaited_once()


def test_ai_chat_allows_action_only_model_output_without_ellipsis_reply():
    source = (ROOT / "bot/services/ai_chat.py").read_text()
    assert 'if not clean_full and action is None:' in source
    assert 'if clean_full:' in source
    assert '"content": clean_full or ""' in source
