from bot.services.ai_context import SmartContextEngine
from bot.services.ai_intent import AIIntentRouter


def test_intent_router_is_deterministic_and_fast_path_budgeted():
    router = AIIntentRouter()
    greeting = router.route("slm bro", {"speed_mode": "flash", "intent_router_enabled": True})
    assert greeting.intent == "greeting"
    assert greeting.max_output_tokens <= 400
    assert greeting.context_messages <= 8

    follow = router.route("w ba3d?", {"speed_mode": "flash", "intent_router_enabled": True})
    assert follow.intent == "follow_up"
    assert follow.context_messages >= greeting.context_messages

    code = router.route("traceback: TypeError async await code fix", {"speed_mode": "flash"})
    assert code.intent == "code_or_complex"
    assert code.max_output_tokens >= 1000


def test_smart_context_keeps_recent_and_relevant_not_random_old_speakers():
    engine = SmartContextEngine()
    entries = [
        {"message_id": 1, "user_id": 10, "role": "user", "content": "pizza recipe tomorrow", "timestamp": 1},
        {"message_id": 2, "user_id": 99, "role": "assistant", "content": "Sure pizza recipe", "timestamp": 2},
        {"message_id": 3, "user_id": 20, "role": "user", "content": "random gaming chat", "timestamp": 3},
        {"message_id": 4, "user_id": 99, "role": "assistant", "content": "gaming sounds fun", "timestamp": 4},
        {"message_id": 5, "user_id": 10, "role": "user", "content": "python traceback aiohttp timeout", "timestamp": 5},
        {"message_id": 6, "user_id": 99, "role": "assistant", "content": "We can inspect the timeout", "timestamp": 6},
        {"message_id": 7, "user_id": 30, "role": "user", "content": "completely unrelated football", "timestamp": 7},
        {"message_id": 8, "user_id": 99, "role": "assistant", "content": "football is separate topic", "timestamp": 8},
    ]
    selected = engine.select(
        entries,
        "why is aiohttp timeout happening?",
        current_user_id=10,
        desired=6,
        char_budget=5000,
        intent="question",
    )
    ids = [item["message_id"] for item in selected]
    assert 8 in ids  # newest context anchor
    assert 5 in ids or 6 in ids  # relevant current-speaker topic survives
    assert ids == sorted(ids)
    assert len(ids) <= 6


def test_smart_context_character_budget_is_hard():
    engine = SmartContextEngine()
    entries = [
        {"message_id": i, "user_id": 1, "role": "user", "content": "x" * 1000, "timestamp": i}
        for i in range(1, 20)
    ]
    selected = engine.select(entries, "hello", current_user_id=1, desired=18, char_budget=2500, intent="greeting")
    assert sum(len(x["content"]) for x in selected) <= 2500
