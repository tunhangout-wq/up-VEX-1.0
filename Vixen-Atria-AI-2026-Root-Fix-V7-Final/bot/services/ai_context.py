"""High-precision conversation context selection for Vixen AI Chat.

The engine is deliberately relevance-first.  It protects the current speaker from
unrelated public-channel chatter while still retaining enough recent conversation
for natural follow-ups.  No model call is required here, so context selection adds
negligible latency to the chat path.
"""
from __future__ import annotations

import re
from typing import Any

_WORD_RE = re.compile(r"[\w\u0600-\u06ffÀ-ÿ']+", re.UNICODE)
_STOPWORDS = {
    "the", "and", "this", "that", "with", "from", "what", "when", "where", "why", "how",
    "are", "was", "were", "you", "your", "for", "but", "then", "just", "have", "has",
    "شنو", "شنوة", "شنيا", "علاش", "كيفاش", "كيفة", "كيف", "وقتاش", "وين", "شكون",
    "هذا", "هاذا", "هذي", "هاذا", "اللي", "من", "في", "على", "انا", "إنا", "إني",
    "انت", "انتي", "هو", "هي", "هم", "يا", "و", "او", "ولا", "منو", "مع", "كان",
    "pour", "avec", "mais", "quoi", "comment", "pourquoi", "quand", "où", "est", "les", "des",
}


def _tokens(text: str) -> set[str]:
    raw = [token.casefold() for token in _WORD_RE.findall(str(text or ""))]
    return {word for word in raw if len(word) >= 2 and word not in _STOPWORDS}


def _compact(text: str) -> str:
    return re.sub(r"[^\w\u0600-\u06ff]", "", str(text or "").casefold())


def _grams(text: str, n: int = 3, cap: int = 220) -> set[str]:
    compact = _compact(text)
    if len(compact) < n:
        return set()
    return {compact[i : i + n] for i in range(min(len(compact) - n + 1, cap))}


def _similarity(a: str, b: str) -> float:
    aw, bw = _tokens(a), _tokens(b)
    if not aw or not bw:
        return 0.0
    word = len(aw & bw) / max(1, len(aw | bw))
    ag, bg = _grams(a), _grams(b)
    gram = len(ag & bg) / max(1, len(ag | bg)) if ag and bg else 0.0
    return word * 0.82 + gram * 0.18


class SmartContextEngine:
    """Select a small, coherent context instead of dumping channel history."""

    @staticmethod
    def _entry_id(item: dict[str, Any], fallback: int) -> str:
        value = item.get("message_id")
        return str(value) if value is not None else f"idx:{fallback}"

    @staticmethod
    def _speaker_id(item: dict[str, Any]) -> str:
        return str(item.get("user_id") or "")

    @staticmethod
    def _is_assistant(item: dict[str, Any]) -> bool:
        return item.get("role") == "assistant"

    def select(
        self,
        entries: list[dict[str, Any]],
        current_text: str,
        *,
        current_user_id: int | str | None,
        desired: int,
        char_budget: int,
        intent: str = "conversation",
        focus_message_id: int | str | None = None,
    ) -> list[dict[str, Any]]:
        if not entries:
            return []
        desired = max(4, min(100, int(desired)))
        char_budget = max(2000, int(char_budget))
        chronological = list(entries)[-140:]
        current_user = str(current_user_id) if current_user_id is not None else None
        current_words = _tokens(current_text)
        current_compact = _compact(current_text)

        # Remove obvious duplicate/race entries before scoring.  Exact duplicates are
        # a common source of accidental repetition when the listener and persistence
        # path both observe the same live message.
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(chronological):
            eid = self._entry_id(item, index)
            content_key = f"{item.get('role')}:{self._speaker_id(item)}:{_compact(item.get('content', ''))[:500]}"
            if eid in seen or (content_key in seen and content_key):
                continue
            seen.add(eid)
            if content_key:
                seen.add(content_key)
            unique.append(item)
        chronological = unique

        recent_windows = {
            "greeting": 4,
            "moderation": 6,
            "owner_action": 6,
            "conversation": 7,
            "question": 9,
            "code_or_complex": 10,
            "follow_up": 12,
        }
        recent_n = min(desired, recent_windows.get(intent, 7))
        recent_start = max(0, len(chronological) - recent_n)

        def relevance(index: int, item: dict[str, Any]) -> float:
            text = str(item.get("content", ""))
            sim = _similarity(current_text, text)
            words = _tokens(text)
            overlap = len(current_words & words)
            same_user = current_user is not None and self._speaker_id(item) == current_user
            is_assistant = self._is_assistant(item)
            recency = index / max(1, len(chronological) - 1)
            score = sim * 9.0 + min(overlap, 10) * 0.9 + recency * 0.75
            if same_user:
                score += 2.8
            if is_assistant and sim > 0.03:
                score += 1.0
            if index >= recent_start:
                score += 0.35

            # In public channels another human's message is only retained when it is
            # actually part of the current topic.  This is the key anti-contamination
            # rule: recent != relevant.
            if current_user is not None and not same_user and not is_assistant and index >= recent_start:
                if sim < 0.10 and overlap == 0:
                    score -= 5.0
            if focus_message_id is not None and str(item.get("message_id")) == str(focus_message_id):
                score += 8.0
            return score

        selected: set[int] = set()
        # First, keep the recent conversational spine, but filter unrelated human
        # chatter.  Assistant replies are kept because they are part of the AI/user
        # exchange even if word overlap is small.
        for index in range(recent_start, len(chronological)):
            item = chronological[index]
            score = relevance(index, item)
            if self._is_assistant(item) or self._speaker_id(item) == current_user or score >= 2.5:
                selected.add(index)

        # Always recover recent messages from the current speaker and nearby assistant
        # responses.  Follow-ups like "w ba3d?" depend on this local thread.
        if current_user is not None:
            same_user_indices = [
                index for index, item in enumerate(chronological[:recent_start])
                if self._speaker_id(item) == current_user
            ]
            for index in same_user_indices[-5:]:
                selected.add(index)
                if index + 1 < len(chronological) and self._is_assistant(chronological[index + 1]):
                    selected.add(index + 1)

        # Add high-relevance historical context until the message/character budget is
        # reached.  This is topic retrieval, not a chronological history dump.
        candidates = sorted(
            ((relevance(index, item), index) for index, item in enumerate(chronological) if index not in selected),
            key=lambda row: (row[0], row[1]),
            reverse=True,
        )
        for score, index in candidates:
            if score < 2.35:
                break
            selected.add(index)
            if index > 0 and self._is_assistant(chronological[index - 1]) and len(selected) < desired:
                selected.add(index - 1)
            if len(selected) >= desired:
                break

        # Trim to desired count while preserving the current speaker + latest context.
        ordered_indices = sorted(selected)
        protected = set()
        if chronological:
            protected.add(len(chronological) - 1)
        if current_user is not None:
            protected.update(
                index for index, item in enumerate(chronological)
                if self._speaker_id(item) == current_user
            )
        if len(ordered_indices) > desired:
            ranked = sorted(
                ordered_indices,
                key=lambda index: (index in protected, relevance(index, chronological[index]), index),
                reverse=True,
            )[:desired]
            ordered_indices = sorted(ranked)

        # Apply a hard character budget from newest to oldest.  This avoids silently
        # exceeding the provider request size while protecting the current thread.
        chosen: list[dict[str, Any]] = []
        used = 0
        for index in reversed(ordered_indices):
            item = chronological[index]
            content = str(item.get("content", ""))[:4000]
            available = char_budget - used
            if available <= 0:
                break
            if len(content) > available:
                # Preserve both the opening intent and the closing details of a long
                # message. Keeping only the tail could drop the actual question.
                clipped = dict(item)
                if available < 80:
                    clipped_text = content[:available].rstrip()
                else:
                    head = max(32, int(available * 0.58))
                    tail = max(24, available - head - 24)
                    clipped_text = f"{content[:head].rstrip()} …[middle clipped]… {content[-tail:].lstrip()}"
                    clipped_text = clipped_text[:available]
                clipped["content"] = clipped_text.rstrip()
                chosen.append(clipped)
                used += len(clipped["content"])
                break
            chosen.append(item)
            used += len(content)
        chosen.reverse()

        dedup: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(chosen):
            dedup[self._entry_id(item, index)] = item
        return list(dedup.values())


smart_context_engine = SmartContextEngine()
