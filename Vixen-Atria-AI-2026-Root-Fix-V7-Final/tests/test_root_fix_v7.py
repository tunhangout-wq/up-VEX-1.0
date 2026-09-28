import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from bot.services.ai_moderation_engine import ModerationEngine


def _install_discord_stub(monkeypatch):
    discord = types.ModuleType("discord")
    class HTTPException(Exception):
        pass
    class Forbidden(HTTPException):
        pass
    class NotFound(HTTPException):
        pass
    discord.HTTPException = HTTPException
    discord.Forbidden = Forbidden
    discord.NotFound = NotFound
    discord.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    discord.utils = SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc))
    discord.Object = type("Object", (), {})

    commands = types.ModuleType("discord.ext.commands")
    class Cog:
        @classmethod
        def listener(cls):
            return lambda fn: fn
    class _CommandDecorator:
        def __call__(self, fn):
            fn.command = lambda **kwargs: (lambda sub: sub)
            return fn
    commands.Cog = Cog
    commands.Context = type("Context", (), {})
    commands.hybrid_group = lambda **kwargs: _CommandDecorator()
    ext = types.ModuleType("discord.ext")
    ext.commands = commands
    app_commands = types.ModuleType("discord.app_commands")
    app_commands.describe = lambda **kwargs: (lambda fn: fn)
    discord.app_commands = app_commands
    monkeypatch.setitem(sys.modules, "discord", discord)
    monkeypatch.setitem(sys.modules, "discord.ext", ext)
    monkeypatch.setitem(sys.modules, "discord.ext.commands", commands)
    monkeypatch.setitem(sys.modules, "discord.app_commands", app_commands)
    return discord


class FakePerms:
    def __init__(self, **kwargs):
        self.send_messages = kwargs.get("send_messages", True)
        self.manage_messages = kwargs.get("manage_messages", True)
        self.moderate_members = kwargs.get("moderate_members", True)
        self.ban_members = kwargs.get("ban_members", True)
        self.manage_guild = kwargs.get("manage_guild", False)
        self.mention_everyone = kwargs.get("mention_everyone", False)
        self.administrator = kwargs.get("administrator", False)


class FakeRole:
    def __init__(self, role_id, position=1, **perms):
        self.id = role_id
        self.position = position
        self.permissions = FakePerms(**perms)
    def __ge__(self, other):
        return self.position >= getattr(other, "position", 0)


class FakeMember:
    def __init__(self, member_id=22, *, top_position=1, guild=None, perms=None, mention="<@22>"):
        self.id = member_id
        self.mention = mention
        self.guild = guild
        self.roles = []
        self.top_role = FakeRole(member_id + 1000, top_position)
        self.guild_permissions = perms or FakePerms(moderate_members=False, manage_messages=False, manage_guild=False, administrator=False)
        self.timeout_calls = []
        self.deleted = False
    async def timeout(self, until, reason=None):
        self.timeout_calls.append((until, reason))
    async def send(self, *_args, **_kwargs):
        return None
    def __repr__(self):
        return f"Member({self.id})"


class FakeMessage:
    def __init__(self, guild, author, channel, content="spam spam"):
        self.guild = guild
        self.author = author
        self.channel = channel
        self.content = content
        self.id = 99
        self.created_at = datetime.now(timezone.utc)
        self.attachments = []
        self.embeds = []
        self.stickers = []
        self.mention_everyone = False
        self.deleted = False
    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, messages=None):
        self.messages = messages or []
        self.sent = []
    def permissions_for(self, _member):
        return FakePerms(manage_messages=True, send_messages=True)
    def history(self, **_kwargs):
        async def generator():
            for item in self.messages:
                yield item
        return generator()
    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))
        return None


class FakeGuild:
    def __init__(self, owner_id=999):
        self.id = 1
        self.owner_id = owner_id
        self.me = SimpleNamespace(id=500, top_role=FakeRole(500, 5), guild_permissions=FakePerms())
    def get_channel(self, _id):
        return None


class FakeStore:
    def __init__(self, incidents=0):
        self.incidents = incidents
    async def _noop(self):
        return True
    def reserve_moderation_action(self, *_args):
        return True
    def count_recent_moderation_actions(self, *_args):
        return self.incidents


@pytest.mark.parametrize("text", [
    "هههههههههههههههههههههههههه",
    "hahahahahahahahahahahahahahaha",
    "😂😂😂😂😂😂😂😂😂😂😂😂",
    "lol lol lol lol lol lol",
])
def test_laughter_is_not_character_spam(text):
    engine = ModerationEngine()
    assert not any(signal.event == "character_spam" for signal in engine.inspect(1, 2, text))


def test_eight_identical_messages_trigger_repeat_and_flood():
    engine = ModerationEngine()
    signals = []
    for _ in range(8):
        signals = engine.inspect(1, 2, "BUY NOW CLICK LINK NOW", repeat_threshold=5, flood_threshold=8)
    events = {signal.event for signal in signals}
    assert "repeat_burst" in events
    assert "message_flood" in events


def test_idle_state_expires_without_evicting_active_bucket(monkeypatch):
    engine = ModerationEngine(max_users=100, state_ttl_seconds=600)
    clock = [1000.0]
    monkeypatch.setattr("bot.services.ai_moderation_engine.time.monotonic", lambda: clock[0])
    engine.inspect(1, 10, "active")
    clock[0] += 601
    engine.inspect(1, 11, "fresh")
    assert (1, 10) not in engine._messages
    clock[0] += 1
    engine.inspect(1, 11, "fresh")
    assert (1, 11) in engine._messages


def test_local_security_signals_cover_invite_mass_mention_and_long_content():
    engine = ModerationEngine()
    signals = engine.inspect(
        1, 2, "https://discord.gg/example " + ("A" * 2050) + " @everyone",
        mention_everyone=True,
        can_mention_everyone=False,
        allow_server_invites=False,
    )
    classifications = {signal.classification for signal in signals}
    assert {"invite_link", "mention_abuse", "long_content"}.issubset(classifications)


def test_raid_join_burst_signal():
    engine = ModerationEngine()
    signals = []
    for user_id in range(8):
        signals = engine.inspect_member_join(1, user_id, window_seconds=20, join_threshold=8)
    assert signals and signals[-1].event == "join_burst"
    assert signals[-1].classification == "raid_like"


@pytest.mark.asyncio
async def test_spam_second_incident_deletes_messages_and_timeouts(monkeypatch):
    _install_discord_stub(monkeypatch)
    import importlib
    tools_module = importlib.import_module("bot.services.moderation_tools")
    from bot.services.moderation_tools import ModerationToolLayer
    guild = FakeGuild()
    author = FakeMember(guild=guild)
    messages = [FakeMessage(guild, author, None, "spam") for _ in range(8)]
    channel = FakeChannel(messages)
    for msg in messages:
        msg.channel = channel
    message = messages[-1]
    bot = SimpleNamespace(user=SimpleNamespace(id=500))
    layer = ModerationToolLayer(FakeStore(incidents=1))
    monkeypatch.setattr(tools_module.ranks, "has_permission", lambda *_args: False)
    monkeypatch.setattr(tools_module.ranks, "get_owner_id", lambda: None)
    monkeypatch.setattr(tools_module, "issue_warning", AsyncMock())
    result = await layer.execute(
        bot, message, "timeout", "spam flood", {
            "enabled": True, "automatic_actions": ["warn", "timeout"], "max_actions_per_hour": 20,
            "delete_offending_messages": True,
        }, classification="flood", window_seconds=8,
    )
    assert result["executed"] is True
    assert result["effective_action"] == "timeout"
    assert result["duration_minutes"] == 5
    assert result["deleted_messages"] == 8


@pytest.mark.asyncio
async def test_trusted_member_is_skipped_before_any_action(monkeypatch):
    _install_discord_stub(monkeypatch)
    from bot.services.moderation_tools import ModerationToolLayer
    guild = FakeGuild(owner_id=22)
    author = FakeMember(guild=guild)
    channel = FakeChannel()
    message = FakeMessage(guild, author, channel)
    layer = ModerationToolLayer(FakeStore())
    with patch("bot.services.moderation_tools.ranks.get_owner_id", return_value=None):
        result = await layer.execute(bot=SimpleNamespace(user=SimpleNamespace(id=500)), message=message, action="timeout", reason="spam", policy={"enabled": True, "automatic_actions": ["timeout"]})
    assert result["result"] == "skipped_trusted"
    assert not author.timeout_calls


@pytest.mark.asyncio
async def test_escalation_first_warn_second_short_third_long(monkeypatch):
    _install_discord_stub(monkeypatch)
    import bot.services.moderation_tools as tools_module
    from bot.services.moderation_tools import ModerationToolLayer
    guild = FakeGuild()
    policy = {"enabled": True, "automatic_actions": ["warn", "timeout"], "max_actions_per_hour": 20}
    bot = SimpleNamespace(user=SimpleNamespace(id=500))
    for incidents, expected_action, expected_minutes in ((0, "warn", None), (1, "timeout", 5), (2, "timeout", 15)):
        author = FakeMember(guild=guild)
        channel = FakeChannel()
        message = FakeMessage(guild, author, channel)
        layer = ModerationToolLayer(FakeStore(incidents))
        monkeypatch.setattr(tools_module.ranks, "get_owner_id", lambda: None)
        monkeypatch.setattr(tools_module.ranks, "has_permission", lambda *_args: False)
        monkeypatch.setattr(tools_module, "issue_warning", AsyncMock(return_value={"warning_id": 1}))
        result = await layer.execute(bot, message, "timeout", "spam", policy, classification="spam")
        assert result["effective_action"] == expected_action
        if expected_minutes is not None:
            assert result["duration_minutes"] == expected_minutes


def test_strict_classifier_contract_is_present_and_ban_is_gated():
    source = Path("bot/cogs/atria.py").read_text()
    assert 'set(decision) != required' in source
    assert 'confidence < 0.98' in source
    assert '<message>{content}</message>' in source


@pytest.mark.asyncio
async def test_invalid_or_injected_ai_json_defaults_to_allow(monkeypatch):
    _install_discord_stub(monkeypatch)
    import bot.cogs.atria as atria_module
    from bot.cogs.atria import Atria
    cog = Atria.__new__(Atria)
    cog.bot = SimpleNamespace()
    cog._metrics = {"moderation_ai_timeouts": 0, "moderation_ai_invalid": 0, "moderation_actions": 0}
    cog._moderation_sem = asyncio.Semaphore(8)
    guild = FakeGuild()
    author = FakeMember(guild=guild)
    msg = FakeMessage(guild, author, FakeChannel(), "hello")
    policy = {"enabled": True, "detection_types": ["spam"], "automatic_actions": ["ban"], "allow_ai_ban": True}
    monkeypatch.setattr(atria_module, "atria_chat", AsyncMock(return_value=json.dumps({"action": "ban"})))
    monkeypatch.setattr(atria_module.moderation_tools, "execute", AsyncMock())
    result = await cog._run_moderation(msg, policy, {})
    assert result["result"] == "invalid_decision"
    atria_module.moderation_tools.execute.assert_not_awaited()


def test_message_edit_path_is_bound_to_moderation_pipeline():
    source = Path("bot/cogs/atria.py").read_text()
    assert 'async def on_message_edit(self, before, after):' in source
    assert 'await self._run_moderation(after, moderation_policy, legacy_cfg)' in source


def test_manual_moderation_requires_staff_or_manage_guild_and_chat_has_cooldown():
    source = Path("bot/cogs/atria.py").read_text()
    assert '"manage_guild", "moderate_members", "manage_messages"' in source
    assert 'ranks.has_permission(ctx.author.id, "moderate_members")' in source
    assert 'if now - last < 5.0:' in source
