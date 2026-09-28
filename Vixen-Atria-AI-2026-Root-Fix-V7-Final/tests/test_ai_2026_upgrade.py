import json
from pathlib import Path
import unittest

from bot.services.ai_orchestrator import ai_orchestrator

ROOT = Path(__file__).resolve().parents[1]


class AI2026UpgradeTests(unittest.TestCase):
    def test_orchestrator_scales_output_and_controls(self):
        plan = ai_orchestrator.plan(
            "اكتبلي شرح مفصل مع مثال",
            {
                "speed_mode": "balanced",
                "max_output_tokens": 8000,
                "context_char_budget": 100000,
                "humor_level": 8,
                "emoji_level": 6,
                "answer_density": "detailed",
                "initiative": "proactive",
                "auto_continuation_enabled": True,
                "self_repair_enabled": True,
            },
        )
        self.assertEqual(plan.profile, "balanced")
        self.assertEqual(plan.intent, "code_or_complex")
        self.assertEqual(plan.max_output_tokens, 8000)
        self.assertEqual(plan.context_chars, 90000)
        self.assertEqual(plan.humor_level, 8)
        self.assertEqual(plan.emoji_level, 6)
        self.assertEqual(plan.answer_density, "detailed")
        self.assertTrue(plan.self_repair)
        self.assertTrue(plan.continuation)

    def test_user_requested_banner_is_in_main(self):
        main = (ROOT / "bot/main.py").read_text(errors="ignore")
        self.assertIn(".::::::::::::::::.", main)
        self.assertIn("'::::::::::::'", main)

    def test_settings_include_real_chat_controls(self):
        data = json.loads((ROOT / "bot/data/settings.json").read_text())
        chat = data["ai"]["chat"]
        for key in (
            "smart_context_enabled", "intent_router_enabled", "quality_guard_enabled",
            "self_repair_enabled", "auto_continuation_enabled", "humor_level",
            "emoji_level", "answer_density", "initiative", "context_char_budget",
        ):
            self.assertIn(key, chat)
        self.assertTrue(chat["auto_continuation_enabled"])
        self.assertEqual(chat["answer_density"], "smart")
        self.assertEqual(chat["max_output_tokens"], 5200)

    def test_forbidden_old_chat_error_phrases_are_not_user_facing(self):
        text = "\n".join(
            p.read_text(errors="ignore")
            for p in [ROOT / "bot/services/ai_provider.py", ROOT / "bot/services/ai_chat.py", ROOT / "bot/web/server.py"]
        )
        self.assertNotIn("جرّب بعد 12ث", text)
        self.assertNotIn("جرّب بعد 18ث", text)
        self.assertNotIn("رسالة أقصر", text)
        self.assertNotIn("قصّر رسالتك", text)

    def test_concurrency_setting_is_documented(self):
        env = (ROOT / ".env.example").read_text()
        self.assertIn("ATRIA_MAX_CONCURRENCY=6", env)

    def test_complete_continuation_and_quality_evaluator_are_wired(self):
        provider = (ROOT / "bot/services/ai_provider.py").read_text()
        chat = (ROOT / "bot/services/ai_chat.py").read_text()
        self.assertIn("auto_continue=True", provider)
        self.assertIn("_combine_continuation", provider)
        self.assertIn("ai_quality_evaluator", chat)
        self.assertIn("quality_score_avg", chat)

    def test_dashboard_exposes_quality_health(self):
        html = (ROOT / "dashboard/index.html").read_text()
        js = (ROOT / "dashboard/assets/app.js").read_text()
        self.assertIn("aiChatQualityScore", html)
        self.assertIn("quality_score_avg", js)

    def test_provider_observability_fields_exist(self):
        source = (ROOT / "bot/services/ai_provider.py").read_text()
        for field in ("request_id", "rate_limit_recoveries", "continuations", "partial_preserved"):
            self.assertIn(field, source)


if __name__ == "__main__":
    unittest.main()
