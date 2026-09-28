import unittest
from bot.services.ai_moderation_engine import ModerationEngine


class ModerationEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = ModerationEngine()

    def test_unicode_and_zero_width_normalization(self):
        self.assertEqual(self.engine.normalize(" A\u200b   B "), "a b")

    def test_repeated_messages_are_detected(self):
        signals = []
        for _ in range(3):
            signals = self.engine.inspect(1, 2, "same message", repeat_threshold=3)
        self.assertTrue(any(s.classification == "repeated_messages" for s in signals))

    def test_flood_is_detected(self):
        signals = []
        for i in range(6):
            signals = self.engine.inspect(1, 2, f"message {i}", flood_threshold=6)
        self.assertTrue(any(s.classification == "flood" for s in signals))

    def test_mass_mentions_are_detected(self):
        content = " ".join("<@123456789>" for _ in range(8))
        signals = self.engine.inspect(1, 2, content, mention_threshold=8)
        self.assertTrue(any(s.classification == "spam" for s in signals))

    def test_deduplication_window(self):
        self.assertFalse(self.engine.should_deduplicate(1, 2, "hello"))
        self.assertTrue(self.engine.should_deduplicate(1, 2, "hello"))

    def test_event_id_deduplicates_retries_without_hiding_real_repeats(self):
        self.assertFalse(self.engine.should_deduplicate(2, 3, 'hello', event_id=101))
        self.assertTrue(self.engine.should_deduplicate(2, 3, 'hello', event_id=101))
        self.assertFalse(self.engine.should_deduplicate(2, 3, 'hello', event_id=102))



if __name__ == "__main__":
    unittest.main()
