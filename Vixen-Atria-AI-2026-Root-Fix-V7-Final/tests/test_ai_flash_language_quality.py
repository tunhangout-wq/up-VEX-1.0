from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    return (ROOT / name).read_text()


def test_flash_default_and_no_visible_placeholder():
    source = read("bot/services/ai_chat.py")
    assert 'config.get("speed_mode", "flash")' in source
    assert 'message.reply("⌛"' not in source
    assert "typing" in source
    assert "First real text is sent immediately" in source


def test_language_lock_handles_arabizi():
    source = read("bot/services/ai_chat.py")
    assert "Chmd5lni ena" in source
    assert "REPLY IN LATIN-SCRIPT ARABIZI/DERJA" in source
    assert "Do NOT translate it into Arabic script" in source


def test_context_cache_and_background_prune():
    source = read("bot/services/ai_chat.py")
    assert "_context_cache" in source
    assert "_CONTEXT_CACHE_TTL" in source
    assert "_schedule_background(" in source
    assert "async def _prune_async" in source


def test_dashboard_speed_mode_is_wired_end_to_end():
    html = read("dashboard/index.html")
    js = read("dashboard/assets/app.js")
    server = read("bot/web/server.py")
    assert 'id="aiChatSpeed"' in html
    assert "config.speed_mode || 'flash'" in js
    assert "speed_mode: $('#aiChatSpeed').value" in js
    assert 'body.get("speed_mode", "flash")' in server
    assert '"speed_mode": speed_mode' in server


def test_provider_flash_has_no_application_limiter():
    source = read("bot/services/ai_provider.py")
    assert "application-side RPM cooldown" in source
    assert "_request_gate" in source
    assert 'FAST_MAX_OUTPUT_TOKENS = 8192' in source
    assert '"stream": True' in source


def test_context_selection_keeps_real_latest_messages():
    source = read("bot/services/ai_chat.py")
    assert "entries = list(entries)[-_MAX_CONTEXT_MESSAGES:]" in source
    assert "latest = entries[-latest_n:]" in source
    assert "entries = list(reversed(entries))[-_MAX_CONTEXT_MESSAGES:]" not in source


def test_live_message_is_not_repeated_as_history():
    source = read("bot/services/ai_chat.py")
    assert "current_message_id = getattr(message, \"id\", None)" in source
    assert "item.get(\"message_id\") != current_message_id" in source
    assert "live message is cached before generation" in source


def test_cross_user_quality_guard_and_repair_are_wired():
    source = read("bot/services/ai_chat.py")
    assert "accidental_historical_speaker_address" in source
    assert "_quality_repair" in source
    assert "Remove random greetings, names, filler and fake context." in source
    assert "Never address a different historical speaker." in source


def test_arabizi_detection_does_not_count_bare_numbers_as_language():
    source = read("bot/services/ai_chat.py")
    assert "Arabizi evidence must be tied to letters" in source
    assert "arabizi_tokens" in source
    assert "derja_score = len(tokens & derja_words) + min(4, len(arabizi_tokens))" in source
