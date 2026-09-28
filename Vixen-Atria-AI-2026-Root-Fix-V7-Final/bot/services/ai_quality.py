"""Deterministic response-quality checks used before optional AI self-repair.

This layer is local-only: it never calls the provider and never pretends to know
whether an answer is factually correct. It catches structural defects that are
cheap and high-confidence to detect (truncation, repetition, protocol leakage,
and broken formatting).
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class QualityReport:
    score: float
    issues: tuple[str, ...]

    @property
    def needs_repair(self) -> bool:
        return bool(self.issues) and self.score < 0.86


class AIQualityEvaluator:
    _TRAILING_CONNECTIVE = re.compile(
        r"(?:\b(?:and|or|but|because|then|so|while|with|to|of|for|و|ولا|أما|لكن|خاطر|وبعد|مع|باش|على)\s*)$",
        re.IGNORECASE,
    )
    _INTERNAL_MARKERS = (
        "[PAST MESSAGE",
        "[CURRENT TURN",
        "VERIFIED_OWNER=",
        "INTENT ROUTER:",
        "CONTEXT FIREWALL:",
        "RETRY-AFTER",
        "rate_limit_recoveries",
    )

    @staticmethod
    def _norm_lines(text: str) -> list[str]:
        return [re.sub(r"\s+", " ", line).strip().casefold() for line in text.splitlines() if line.strip()]

    @classmethod
    def evaluate(cls, answer: str, prompt: str = "", *, finish_reason=None, recovered=False, intent=None) -> QualityReport:
        text = str(answer or "").strip()
        if not text:
            return QualityReport(0.0, ("empty_answer",))

        score = 1.0
        issues: list[str] = []
        lowered = text.casefold()

        if any(marker.casefold() in lowered for marker in cls._INTERNAL_MARKERS):
            issues.append("internal_context_leak")
            score -= 0.40

        fences = text.count("```")
        if fences % 2:
            issues.append("unbalanced_code_fence")
            score -= 0.22

        lines = cls._norm_lines(text)
        if len(lines) >= 2:
            counts = {}
            for line in lines:
                if len(line) >= 24:
                    counts[line] = counts.get(line, 0) + 1
            if any(count >= 2 for count in counts.values()):
                issues.append("repeated_block")
                score -= 0.28

        tail = text.rstrip("`*_ ").casefold()
        if finish_reason == "length" or recovered:
            connective_tokens = {
                "and", "or", "but", "because", "then", "so", "while", "with", "to", "of", "for",
                "و", "ولا", "أما", "لكن", "خاطر", "وبعد", "مع", "باش", "على",
            }
            last_token = re.findall(r"[\w\u0600-\u06ff]+", tail)
            if len(text) >= 12 and not re.search(r"[.!?؟:؛،]$", text) and ((last_token and last_token[-1] in connective_tokens) or cls._TRAILING_CONNECTIVE.search(tail)):
                issues.append("truncated_tail")
                score -= 0.24

        try:
            prompt_len = len(str(prompt or "").strip())
        except Exception:
            prompt_len = 0
        if intent in {"greeting", "thanks", "casual"} and prompt_len <= 80 and len(text) > 1800:
            issues.append("simple_prompt_overlong")
            score -= 0.16

        if "…" == text[-1:] and len(text) < 1900:
            # An ellipsis can be intentional, so this is only a low-weight signal.
            issues.append("ellipsis_tail")
            score -= 0.05

        return QualityReport(max(0.0, round(score, 3)), tuple(dict.fromkeys(issues)))


ai_quality_evaluator = AIQualityEvaluator()
