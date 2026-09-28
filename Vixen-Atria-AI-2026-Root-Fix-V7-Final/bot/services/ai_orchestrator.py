"""Local orchestration layer for Vixen's conversational AI.

The orchestrator decides how a turn should be handled before the provider is called.
It never invents provider capabilities: it only combines local routing, language
analysis and persisted settings into a deterministic execution plan.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from bot.services.ai_intent import IntentRoute, ai_intent_router


@dataclass(frozen=True, slots=True)
class ChatPlan:
    profile: str
    intent: str
    confidence: float
    max_output_tokens: int
    context_messages: int
    context_chars: int
    fast_path: bool
    streaming: bool
    quality_guard: bool
    self_repair: bool
    continuation: bool
    humor_level: int
    emoji_level: int
    answer_density: str
    initiative: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class AIOrchestrator:
    """Build a stable per-turn plan without extra network calls."""

    _PROFILES = {
        "flash": {"tokens": 6000, "chars": 50_000, "messages": 36},
        "balanced": {"tokens": 10_000, "chars": 90_000, "messages": 64},
        "deep": {"tokens": 16_000, "chars": 125_000, "messages": 90},
    }

    @staticmethod
    def _int_value(config: dict, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError, OverflowError):
            return default
        return max(minimum, min(maximum, value))

    @classmethod
    def plan(cls, content: str, config: dict) -> ChatPlan:
        config = config if isinstance(config, dict) else {}
        profile = str(config.get("speed_mode", "flash")).strip().lower()
        if profile not in cls._PROFILES:
            profile = "flash"

        route: IntentRoute = ai_intent_router.route(content, config)
        defaults = cls._PROFILES[profile]
        requested_tokens = cls._int_value(config, "max_output_tokens", defaults["tokens"], 256, 65_536)
        # `IntentRoute.max_output_tokens` is a routing hint, not a hidden hard cap.
        # Otherwise a short route such as `question=2200` could silently truncate a
        # technically complete answer even when the configured profile allows more.
        max_output_tokens = min(requested_tokens, defaults["tokens"])
        minimum_by_intent = {"greeting": 300, "thanks": 300, "casual": 800}
        max_output_tokens = max(max_output_tokens, minimum_by_intent.get(route.intent, 0))

        requested_context = cls._int_value(config, "context_messages", defaults["messages"], 12, 100)
        requested_context = min(requested_context, defaults["messages"])
        context_chars = min(defaults["chars"], cls._int_value(config, "context_char_budget", defaults["chars"], 12_000, 125_000))

        humor = cls._int_value(config, "humor_level", 5, 0, 10)
        emoji = cls._int_value(config, "emoji_level", 3, 0, 10)
        density = str(config.get("answer_density", "smart")).strip().lower()
        if density not in {"brief", "smart", "detailed"}:
            density = "smart"
        initiative = str(config.get("initiative", "balanced")).strip().lower()
        if initiative not in {"low", "balanced", "proactive"}:
            initiative = "balanced"

        streaming = bool(config.get("streaming", True)) and config.get("flash_path_enabled", True) is not False
        fast_path = profile == "flash"
        quality_guard = config.get("quality_guard_enabled", True) is not False
        self_repair = config.get("self_repair_enabled", True) is not False
        continuation = config.get("auto_continuation_enabled", True) is not False

        if route.intent in {"greeting", "thanks"}:
            max_output_tokens = max(max_output_tokens, 300)
        elif route.intent == "casual":
            max_output_tokens = max(max_output_tokens, 800)
        elif route.intent == "creative":
            max_output_tokens = max(max_output_tokens, min(requested_tokens, 2600 if profile == "flash" else 4200))
        elif route.intent == "code_or_complex":
            max_output_tokens = max(max_output_tokens, min(requested_tokens, 3200 if profile == "flash" else 5200))

        return ChatPlan(
            profile=profile,
            intent=route.intent,
            confidence=route.confidence,
            max_output_tokens=max(64, max_output_tokens),
            context_messages=requested_context,
            context_chars=context_chars,
            fast_path=fast_path,
            streaming=streaming,
            quality_guard=quality_guard,
            self_repair=self_repair,
            continuation=continuation,
            humor_level=humor,
            emoji_level=emoji,
            answer_density=density,
            initiative=initiative,
            reason=route.reason,
        )


ai_orchestrator = AIOrchestrator()
