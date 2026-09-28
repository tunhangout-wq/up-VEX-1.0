"""Deterministic local intent router for Vixen AI Chat.

The router never calls the model.  Its job is to keep the prompt small, choose an
appropriate response budget, and prevent expensive deep-context behavior for simple
social messages.  Ambiguous messages stay on the safe conversational route.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class IntentRoute:
    intent: str
    confidence: float
    context_messages: int
    context_chars: int
    max_output_tokens: int
    use_flash: bool = True
    reason: str = "heuristic"


class AIIntentRouter:
    _ACTION_RE = re.compile(
        r"(?:\b(?:join\s+(?:voice|vc)|leave\s+(?:voice|vc)|move\s+to\s+voice|disconnect\s+from\s+voice)\b)"
        r"|(?:\b(?:ban|kick|mute|unmute|warn|timeout|untimeout|unban)\b(?:\s+(?:for|on|to|@)?\s*)(?:<@!?\d+>|@\S+|\d{15,22}|[A-Za-z_][\w.-]{1,31}))"
        r"|(?:\b(?:remove\s+role|add\s+role|give\s+role|take\s+role|create\s+role|delete\s+role|rename\s+role)\b(?:\s+(?:for|on|to|the)?\s*)(?:<@&\d+>|<@!?\d+>|@\S+|role\s+\S+|\S{2,}))"
        r"|(?:\b(?:lock\s+channel|unlock\s+channel|slowmode)\b(?:\s+(?:in|on|to)?\s*)(?:<#\d+>|#?\S{2,}))"
        r"|(?:\b(?:nickname)\b(?:\s+(?:for|on|to)?\s*)(?:<@!?\d+>|@\S+|\S{2,}))"
        r"|(?:\b(?:باند|بان|اطرد|طرد|كيك|ميوت|تايموت|تايم\s*اوت|حذر|حذّر|تحذير|نحي\s+التايموت|فك\s+التايموت|فك\s+البان|انحي\s+البان)\b(?:\s+(?:على|ل|لل)?\s*)(?:<@!?\d+>|@\S+|\d{15,22}|\S{2,}))"
        r"|(?:\b(?:انحي\s+رول|زيد\s+رول)\s+(?:<@&\d+>|@\S+|\S{2,}))"
        r"|(?:\b(?:ادخل\s+فويس|اخرج\s+من\s+فويس)\b)",
        re.I,
    )
    _ACTION_QUESTION_GUARD = re.compile(
        r"[?؟]|\b(?:how|why|what|when|where|can|could|should|is|are|do|does|did|شنوة|شنيا|شنو|علاش|كيفاش|كيف|وقتاش|وين|شكون|تنجم|نجم|هل|واش|command\s+متاع|طريقة|كيفية)\b",
        re.I,
    )
    _ACTION_NON_EXECUTION_RE = re.compile(
        r"\b(?:example|for\s+example|hypothetical|hypothetically|quote|quoted|quoting|i\s+wrote|i\s+said|just\s+an\s+example)\b"
        r"|(?:مثال|كمثال|مثلا|مثلاً|افتراض|افتراضي|اقتباس|مقتبس|كي\s+نقول|لو\s+نقول|اذا\s+نقول|إذا\s+نقول|مجرد\s+مثال)",
        re.I | re.U,
    )
    _CODE_RE = re.compile(
        r"```|\b(?:python|javascript|typescript|discord\.py|aiohttp|traceback|stacktrace|exception|error|bug|fix|code|script|class|async|await|import|json|regex|sql|sqlite|api)\b",
        re.I,
    )
    _CREATIVE_RE = re.compile(
        r"\b(?:idea|ideas|creative|funny|joke|roast|caption|bio|name|nickname|story|poem|poetry|script|design|ui|ux|brand|logo)\b"
        r"|(?:فكرة|افكار|أفكار|مضحك|نكتة|روست|كابشن|بايو|اسم|لقب|قصة|شعر|سكريبت|تصميم|واجهة|شعار)",
        re.I,
    )
    _TASK_RE = re.compile(
        r"\b(?:write|explain|describe|compare|summarize|translate|analyze|create|build|design|draft|make|give me|show me|tell me)\b"
        r"|(?:اكتب|اشرح|فسر|فسّر|حلل|حلّل|قارن|لخص|لخّص|ترجم|اعمل|اعمللي|صمم|صمّم|اعطيني|أعطيني|وريني|فسرهالي|فسرهالي)"
        r"|(?:شنوة نعمل|شنيا نعمل|كيفاش نعمل|عطيني فكرة|اعطيني فكرة)",
        re.I,
    )
    _MOD_RE = re.compile(
        r"\b(?:moderate|moderation|toxic|toxicity|harassment|spam|raid|scam|suspicious|hate|insult|report)\b"
        r"|\b(?:سمّ|سام|سبام|مزعج|تحرش|توكسك|توكسيسيتي|رايد|احتيال|مشبوه|إهانة|تبليغ)\b",
        re.I,
    )
    _QUESTION_RE = re.compile(
        r"[?؟]|\b(?:what|why|how|when|where|who|can|could|should|is|are|do|does|did|can\s+i|"
        r"شنو|شنوة|شنيا|علاش|كيفاش|كيفة|كيف|وقتاش|وين|شكون|تنجم|نجم|هل|واش|pourquoi|comment|quand|où|est-ce)\b",
        re.I | re.X,
    )
    _FOLLOWUP_RE = re.compile(
        r"\b(?:and|also|then|so|because|but|same|that|this|it|he|she|they|another|more|again|"
        r"continue|carry\s+on|keep\s+going|go\s+on|mouch|w|zeda|ama|donc|puis|ca|ça|same\s+thing|"
        r"هاذا|هذا|هذي|هو|هي|هما|و|زادة|أما|بعد|كمل|كمّل|نكمل|نكمّل|زيد|نزيد|نواصل|واصل|كملها|كمّلها|واصلها)\b",
        re.I,
    )
    _THANKS_RE = re.compile(
        r"^(?:thx|tnx|thanks|thank\s+you|ty|merci|chokran|yaatik\s+essa7a|يعطيك\s+الصحة|شكرا|مشكور)[!?.؟\s~:-]*$",
        re.I,
    )
    _ACK_RE = re.compile(
        r"^(?:ok|okay|kk|alright|sure|yep|yup|yes|nope|fine|cool|bet|تمام|تم|مريقل|باهي|اي|إي|ايه|لا|اوكي|حاضر|موافق|صح|صحيت)[!?.؟\s~:-]*$",
        re.I,
    )
    _GREETING_RE = re.compile(
        r"^(?:(?:هههه+|haha+)\s+)?(?:hi|hey|hello|yo|sup|gm|gn|salam|slm|ahlan|bonjour|bonsoir|labes(?:\?)?|chna7wek|"
        r"ahla(?:\s+(?:wink|winek|winak|وينك))?|wink|winek|winak|وينك|وينك\s+يا|"
        r"شنو\s*الأخبار|شنو\s*الأحوال|(?:سلام|عسلامة|هلا|مرحبا)(?:\s+(?:وينك|وين|شنوة|شنيا))?|لاباس(?:\s+(?:وينك|وين|؟|\?))?|يا هلا|هههه+|haha+)"
        r"(?:\s+(?:bro|sahbi|sahbti|ya|guys|everyone))?"
        r"(?:\s+<@!?\d+>)*[!?.؟\s~:-]*$",
        re.I,
    )
    _MICRO_SOCIAL_RE = re.compile(
        r"^(?:ah?la|ahla|hey|hi|yo|salam|slm|hello|wink|winek|winak|وينك|شنوة|شنيا|لاباس|عسلامة|هلا|مرحبا)"
        r"(?:\s+(?:wink|winek|winak|وينك|bro|sahbi|sahbti|ya))?"
        r"(?:\s+(?:يا)?<@!?\d+>)*[!?.؟\s~:-]*$",
        re.I,
    )

    @staticmethod
    def _config_route(speed: str) -> IntentRoute:
        if speed == "deep":
            return IntentRoute("conversation", 0.55, 18, 22_000, 1800, use_flash=False, reason="router-disabled")
        if speed == "balanced":
            return IntentRoute("conversation", 0.55, 12, 15_000, 1000, use_flash=False, reason="router-disabled")
        return IntentRoute("conversation", 0.55, 8, 9_000, 700, use_flash=True, reason="router-disabled")

    def route(self, text: str, config: dict | None = None) -> IntentRoute:
        config = config if isinstance(config, dict) else {}
        value = str(text or "").strip()
        speed = str(config.get("speed_mode", "flash")).strip().lower()
        if speed not in {"flash", "balanced", "deep"}:
            speed = "flash"
        if config.get("intent_router_enabled", True) is False:
            return self._config_route(speed)

        length = len(value)
        words = len(re.findall(r"\S+", value))
        # Discord mentions are metadata, not conversational words. Remove them only
        # for local intent classification so messages like `Ahla wink <@123>` stay
        # on the micro-social path. The original text is preserved for the model.
        classification_value = re.sub(r"<@!?\d+>", " ", value)
        classification_value = re.sub(r"\s+", " ", classification_value).strip()

        # Direct actions must be routed before generic words like "why" or "error",
        # but a question about an action is still a question, not an execution request.
        if (
            self._ACTION_RE.search(value)
            and not self._ACTION_QUESTION_GUARD.search(value)
            and not self._ACTION_NON_EXECUTION_RE.search(value)
        ):
            return IntentRoute("owner_action", 0.99, 8, 10_000, 900, use_flash=speed == "flash", reason="action-pattern")
        if self._CODE_RE.search(value) or length >= 1800:
            return IntentRoute("code_or_complex", 0.94, 24 if speed == "deep" else (20 if speed == "balanced" else 16), 32_000 if speed == "deep" else (28_000 if speed == "balanced" else 22_000), 6500 if speed == "deep" else (5200 if speed == "balanced" else 3200), use_flash=speed == "flash", reason="code-or-complexity")
        if self._MOD_RE.search(value) and not self._QUESTION_RE.search(value):
            return IntentRoute("moderation", 0.88, 10, 12_000, 1200, use_flash=speed == "flash", reason="moderation-pattern")
        if self._THANKS_RE.fullmatch(value):
            return IntentRoute("thanks", 0.99, 3, 4_000, 300, use_flash=True, reason="gratitude-pattern")
        if self._ACK_RE.fullmatch(value):
            return IntentRoute("casual", 0.99, 2, 3_000, 800, use_flash=True, reason="tiny-ack-pattern")
        if self._GREETING_RE.fullmatch(classification_value) or self._MICRO_SOCIAL_RE.fullmatch(classification_value):
            return IntentRoute("greeting", 0.99, 2, 3_000, 300, use_flash=True, reason="micro-greeting-pattern")
        if length <= 100 and self._FOLLOWUP_RE.search(value):
            return IntentRoute("follow_up", 0.88, 18 if speed == "flash" else (28 if speed == "balanced" else 36), 20_000 if speed == "flash" else (32_000 if speed == "balanced" else 50_000), 1600 if speed == "flash" else (3000 if speed == "balanced" else 5000), use_flash=speed == "flash", reason="follow-up-pattern")
        if self._CREATIVE_RE.search(value):
            return IntentRoute("creative", 0.90, 16 if speed == "flash" else (24 if speed == "balanced" else 34), 22_000 if speed == "flash" else (34_000 if speed == "balanced" else 48_000), 2200 if speed == "flash" else (3600 if speed == "balanced" else 5200), use_flash=speed == "flash", reason="creative-pattern")
        if self._QUESTION_RE.search(value):
            return IntentRoute("question", 0.88, 18 if speed == "flash" else (28 if speed == "balanced" else 40), 22_000 if speed == "flash" else (34_000 if speed == "balanced" else 55_000), 2200 if speed == "flash" else (3600 if speed == "balanced" else 6000), use_flash=speed == "flash", reason="question-pattern")
        if self._TASK_RE.search(value):
            return IntentRoute("code_or_complex", 0.90, 18 if speed == "flash" else (26 if speed == "balanced" else 38), 22_000 if speed == "flash" else (34_000 if speed == "balanced" else 52_000), 2200 if speed == "flash" else (3800 if speed == "balanced" else 6000), use_flash=speed == "flash", reason="task-pattern")
        if words <= 8 and length <= 70:
            return IntentRoute("casual", 0.80, 6, 8_000, 800, use_flash=True, reason="short-casual")
        return IntentRoute("conversation", 0.75, 14 if speed == "flash" else (22 if speed == "balanced" else 32), 20_000 if speed == "flash" else (32_000 if speed == "balanced" else 52_000), 1800 if speed == "flash" else (3200 if speed == "balanced" else 5200), use_flash=speed == "flash", reason="default-chat")


ai_intent_router = AIIntentRouter()
