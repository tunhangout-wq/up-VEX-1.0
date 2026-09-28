from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def test_owner_action_module_requires_verified_owner():
    source = (ROOT / "bot/services/ai_actions.py").read_text()
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "is_owner":
                calls.append(node)
    assert calls, "owner action executor must call ranks.is_owner"


def test_ai_chat_supports_max_context_and_streaming():
    source = (ROOT / "bot/services/ai_chat.py").read_text()
    assert "provider.stream" in source
    assert "_context_settings" in source
    assert "speed_mode" in source
    assert "_context_settings" in source
    assert "LANGUAGE LOCK" in source


def test_dashboard_stream_route_is_wired():
    server = (ROOT / "bot/web/server.py").read_text()
    js = (ROOT / "dashboard/assets/app.js").read_text()
    assert '"/api/ai/chat/stream"' in server
    assert "api_ai_chat_stream" in server
    assert "streamPost('/api/ai/chat/stream'" in js
    assert "streamPost" in js
