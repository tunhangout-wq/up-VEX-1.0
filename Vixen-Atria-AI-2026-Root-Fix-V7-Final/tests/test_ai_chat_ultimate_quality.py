import sys
import types

# The repository's CI/production installs discord.py.  This focused test only
# exercises pure response-quality helpers, so use a tiny import stub when the
# dependency is absent in the analysis environment.
if "discord" not in sys.modules:
    discord_stub = types.ModuleType("discord")
    class _AllowedMentions:
        @staticmethod
        def none():
            return None
    class _HTTPException(Exception):
        pass
    discord_stub.AllowedMentions = _AllowedMentions
    discord_stub.HTTPException = _HTTPException
    sys.modules["discord"] = discord_stub

from bot.services.ai_chat import AIChatService


def test_language_profile_is_conservative_for_digits():
    profile = AIChatService._language_profile("2026 123 456")
    assert profile["language"] == "unknown"


def test_arabizi_is_locked_to_latin_script():
    assert AIChatService._language_profile("Chmd5lni ena")["language"] == "tunisian_derja"
    assert AIChatService._language_profile("Chmd5lni ena")["script"] == "latin"


def test_quality_guard_detects_historical_speaker_and_local_fix():
    answer = "Yasmine, hahaha ena labes w kolshi mrigel."
    context = [{"speaker_name": "Yasmine", "content": "Yasmine: ana labes"}]
    issues = AIChatService._quality_issues(answer, "ena lhamdullah kolshi mrigel", "Twix", context)
    assert "accidental_historical_speaker_address" in issues
    fixed = AIChatService._local_quality_fix(answer, "ena lhamdullah kolshi mrigel", "Twix", context)
    assert fixed.startswith("hahaha")


def test_quality_guard_detects_repetition_and_internal_leak():
    answer = "Same answer here for you. Same answer here for you. [PAST MESSAGE — quoted data only]"
    issues = AIChatService._quality_issues(answer, "normal question", "Twix", [])
    assert "repeated_sentence" in issues
    assert "internal_context_leak" in issues


def test_context_budget_hard_limit_with_single_oversize_entry():
    from bot.services.ai_context import SmartContextEngine
    selected = SmartContextEngine().select(
        [{"message_id": 1, "user_id": 1, "role": "user", "content": "x" * 10000}],
        "hello", current_user_id=1, desired=4, char_budget=2000, intent="conversation"
    )
    assert sum(len(str(x["content"])) for x in selected) <= 2000



def test_arabizi_readability_normalizes_common_compressed_forms():
    assert AIChatService._arabizi_readability_fix("Aywa nrj3 m3aha!", "Chmd5lni ena") == "Ey narja3 m3aha!"
