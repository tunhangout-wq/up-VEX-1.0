import unittest

from bot.services.ai_quality import ai_quality_evaluator


class AIQualityEvaluatorTests(unittest.TestCase):
    def test_detects_structural_truncation(self):
        report = ai_quality_evaluator.evaluate("هاني نفسرلك كيفاش تصلحها و", "اشرحلي المشكلة", finish_reason="length")
        self.assertIn("truncated_tail", report.issues)
        self.assertTrue(report.needs_repair)

    def test_detects_repeated_block(self):
        answer = "This is the same useful paragraph repeated.\nThis is the same useful paragraph repeated."
        report = ai_quality_evaluator.evaluate(answer, "explain")
        self.assertIn("repeated_block", report.issues)

    def test_clean_answer_is_not_flagged(self):
        report = ai_quality_evaluator.evaluate("Ey, فهمتك. نعطيك الخطوات وحدة بوحدة.", "شنوة نعمل؟")
        self.assertGreaterEqual(report.score, 0.86)
        self.assertFalse(report.needs_repair)

    def test_internal_markers_are_not_allowed(self):
        report = ai_quality_evaluator.evaluate("INTENT ROUTER: question\nالجواب", "سؤال")
        self.assertIn("internal_context_leak", report.issues)
        self.assertTrue(report.needs_repair)
