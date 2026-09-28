from pathlib import Path
import unittest


class AIFastProfileContractTests(unittest.TestCase):
    def test_discord_chat_uses_fast_profile_and_quality_context(self):
        source = Path("bot/services/ai_chat.py").read_text()
        self.assertIn("provider.stream", source)
        self.assertIn("_context_settings", source)
        self.assertIn("speed_mode", source)
        self.assertIn("per-user context selection inside shared channels", source)
        self.assertIn("LANGUAGE LOCK", source)
        self.assertIn("Resolve pronouns, short follow-ups, jokes, callbacks", source)
        self.assertIn("Do not output chain-of-thought", source)
        self.assertIn("allowed_mentions=discord.AllowedMentions.none()", source)

    def test_provider_fast_profile_is_unthrottled_and_no_reasoning_field(self):
        source = Path("bot/services/ai_provider.py").read_text()
        self.assertIn("application-side RPM cooldown", source)
        self.assertIn("Bound concurrent outbound requests", source)
        self.assertIn("_request_gate", source)
        self.assertIn("client_rate_limit", source)
        self.assertNotIn('"reasoning"', source)

    def test_startup_reports_atria_runtime(self):
        source = Path("bot/main.py").read_text()
        self.assertIn('bot.get_cog("Atria")', source)
        self.assertIn("Message Content Intent", source)

    def test_discord_listener_is_registered(self):
        source = Path("bot/cogs/atria.py").read_text()
        self.assertIn("@commands.Cog.listener()\n    async def on_message", source)

    def test_personality_is_coherent_tunisian_and_playfully_toxic(self):
        source = Path("bot/services/ai_chat.py").read_text()
        self.assertIn("default smart Tunisian personality", source)
        self.assertIn("lightly toxic", source)
        self.assertIn("Roast the situation", source)
        self.assertIn("Do not invent Discord facts, permissions, users", source)


if __name__ == "__main__":
    unittest.main()
