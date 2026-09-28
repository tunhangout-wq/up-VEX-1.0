"""Low-latency, stateful moderation preflight for Vixen.

The engine handles high-signal abuse locally so normal messages do not wait on an
LLM. State is bounded and expired by monotonic TTL rather than arbitrary key removal.
"""
from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from threading import RLock
from typing import Iterable

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_SPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^\w\s]", re.UNICODE)
_DISCORD_INVITE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", re.I)
_SHORTENER = re.compile(r"(?:https?://)?(?:bit\.ly|tinyurl\.com|t\.co|is\.gd|cutt\.ly|rb\.gy|shorturl\.at)/\S+", re.I)
_URL = re.compile(r"https?://[^\s>]+", re.I)
_EVERYONE = re.compile(r"<@&?everyone>|@everyone|@here", re.I)
_LAUGHTER = re.compile(
    r"^(?:(?:h|a|ha|he|هه|ههه|هههه|خخ|lol|lmao|lmfao|rofl|mdr|😂|🤣|😭|💀|\s|!|\.|_|-)+)$",
    re.I,
)


@dataclass(frozen=True)
class ModerationSignal:
    classification: str
    confidence: float
    reason: str
    event: str
    action: str = "allow"
    local: bool = True


class ModerationEngine:
    def __init__(self, max_users: int = 10000, state_ttl_seconds: float = 600.0):
        self._lock = RLock()
        self._messages: dict[tuple[int, int], deque[tuple[float, str]]] = defaultdict(deque)
        self._recent: dict[tuple[object, ...], float] = {}
        self._joins: dict[int, deque[tuple[float, int]]] = defaultdict(deque)
        self._max_users = max(100, int(max_users))
        self._state_ttl = max(60.0, float(state_ttl_seconds))
        self._last_cleanup = 0.0

    @staticmethod
    def normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", str(text or ""))
        text = _ZERO_WIDTH.sub("", text).casefold()
        text = _SPACE.sub(" ", text).strip()
        return text

    @classmethod
    def compact(cls, text: str) -> str:
        normalized = cls.normalize(text)
        return _NON_ALNUM.sub("", normalized)

    @classmethod
    def _is_laughter_or_emote(cls, normalized: str) -> bool:
        value = normalized.replace(" ", "")
        if not value or len(value) < 2:
            return False
        return bool(_LAUGHTER.fullmatch(value))

    def _cleanup_locked(self, now: float) -> None:
        if now - self._last_cleanup < 15.0 and len(self._messages) <= self._max_users and len(self._recent) <= self._max_users * 3:
            return
        cutoff = now - self._state_ttl
        stale_message_keys = []
        for key, bucket in self._messages.items():
            while bucket and bucket[0][0] < cutoff:
                bucket.popleft()
            if not bucket:
                stale_message_keys.append(key)
        for key in stale_message_keys:
            self._messages.pop(key, None)

        stale_recent = [key for key, ts in self._recent.items() if ts < cutoff]
        for key in stale_recent:
            self._recent.pop(key, None)

        for guild_id, joins in self._joins.items():
            while joins and joins[0][0] < cutoff:
                joins.popleft()
        self._joins = defaultdict(deque, {gid: q for gid, q in self._joins.items() if q})

        if len(self._messages) > self._max_users:
            # Expired entries are removed first. Only then evict the least recently
            # updated active bucket, never the arbitrary first dictionary key.
            candidates = sorted(self._messages.items(), key=lambda item: item[1][-1][0] if item[1] else 0.0)
            for key, _ in candidates[: max(0, len(self._messages) - self._max_users)]:
                self._messages.pop(key, None)
        if len(self._recent) > self._max_users * 3:
            candidates = sorted(self._recent.items(), key=lambda item: item[1])
            for key, _ in candidates[: max(0, len(self._recent) - self._max_users * 3)]:
                self._recent.pop(key, None)
        self._last_cleanup = now

    def inspect(
        self,
        guild_id: int,
        user_id: int,
        content: str,
        *,
        window_seconds: float = 8.0,
        repeat_threshold: int = 5,
        flood_threshold: int = 8,
        mention_threshold: int = 8,
        attachment_names: Iterable[str] | None = None,
        embed_urls: Iterable[str] | None = None,
        sticker_count: int = 0,
        mention_everyone: bool = False,
        can_mention_everyone: bool = False,
        allow_server_invites: bool = False,
    ) -> list[ModerationSignal]:
        now = time.monotonic()
        normalized = self.normalize(content)
        compact = self.compact(content)
        names = [str(v)[:255] for v in (attachment_names or ()) if isinstance(v, str)]
        embed_links = [str(v)[:500] for v in (embed_urls or ()) if isinstance(v, str)]
        has_evidence = bool(normalized or names or embed_links or int(sticker_count or 0) > 0)
        if not has_evidence:
            return []
        key = (int(guild_id), int(user_id))
        window = max(2.0, float(window_seconds))
        repeat_limit = max(2, int(repeat_threshold))
        flood_limit = max(3, int(flood_threshold))
        mention_limit = max(3, int(mention_threshold))

        with self._lock:
            self._cleanup_locked(now)
            bucket = self._messages[key]
            if normalized:
                bucket.append((now, normalized))
            cutoff = now - window
            while bucket and bucket[0][0] < cutoff:
                bucket.popleft()

            signals: list[ModerationSignal] = []
            same = sum(1 for _, value in bucket if value == normalized and normalized)
            if same >= repeat_limit:
                effective_repeat = same if len(normalized) > 5 else same * 0.5
                if effective_repeat >= repeat_limit:
                    signals.append(ModerationSignal(
                        "repeated_messages", min(0.99, 0.90 + 0.025 * (same - repeat_limit)),
                        f"نفس الرسالة تكررت {same} مرات خلال {int(window)} ثوانٍ.", "repeat_burst"
                    ))

            # Very short conversational replies are deliberately weighted down.
            weighted_count = sum(1.0 if len(v) > 8 else (0.5 if len(v) > 3 else 0.25) for _, v in bucket)
            if weighted_count >= flood_limit:
                signals.append(ModerationSignal(
                    "flood", min(0.99, 0.90 + 0.015 * (weighted_count - flood_limit)),
                    f"نشاط رسائل متسارع ({len(bucket)} رسالة موزونة) خلال {int(window)} ثوانٍ.", "message_flood"
                ))

            mentions = len(re.findall(r"<@!?\d+>", content)) + len(re.findall(r"<@&\d+>", content)) + len(re.findall(r"<#\d+>", content))
            if mentions >= mention_limit:
                signals.append(ModerationSignal(
                    "spam", 0.98, f"عدد كبير من المنشنات في رسالة واحدة ({mentions}).", "mention_spam"
                ))

            if (_EVERYONE.search(content) or mention_everyone) and not can_mention_everyone:
                signals.append(ModerationSignal(
                    "mention_abuse", 0.98, "استعمال @everyone/@here بدون صلاحية المنشن العام.", "unauthorized_mass_mention"
                ))

            invite_hits = _DISCORD_INVITE.findall(content)
            if invite_hits and not allow_server_invites:
                signals.append(ModerationSignal(
                    "invite_link", 0.96, "رابط دعوة Discord غير مسموح به حسب سياسة السيرفر.", "blocked_invite"
                ))

            shortener_hits = _SHORTENER.findall(content)
            urls = _URL.findall(content) + embed_links
            normalized_urls = [u.rstrip(".,)>]").casefold() for u in urls]
            duplicate_urls = len(normalized_urls) - len(set(normalized_urls))
            if shortener_hits or duplicate_urls >= 1:
                signals.append(ModerationSignal(
                    "suspicious_links", 0.94,
                    "روابط مختصرة أو مكررة داخل الرسالة/Embed.", "suspicious_link_pattern"
                ))

            if len(normalized) > 2000:
                signals.append(ModerationSignal(
                    "long_content", 0.91, f"محتوى طويل جدًا ({len(normalized)} حرف).", "long_content"
                ))

            if names and any(_DISCORD_INVITE.search(name) or _SHORTENER.search(name) for name in names):
                signals.append(ModerationSignal(
                    "suspicious_links", 0.95, "اسم ملف مرفق يحتوي على رابط مختصر/دعوة مشبوهة.", "suspicious_attachment_name"
                ))

            if sticker_count >= 12 or len(names) >= 12:
                signals.append(ModerationSignal(
                    "spam", 0.93,
                    f"عدد كبير من المرفقات/الملصقات ({len(names) + int(sticker_count)}).", "attachment_spam"
                ))

            if len(compact) >= 40 and not self._is_laughter_or_emote(normalized):
                most_common = max((compact.count(ch) for ch in set(compact)), default=0)
                repetition_ratio = most_common / max(1, len(compact))
                if repetition_ratio >= 0.82:
                    signals.append(ModerationSignal(
                        "spam", 0.94, "نمط تكرار حرفي طويل وغير ذي معنى.", "character_spam"
                    ))
            return signals

    def inspect_member_join(self, guild_id: int, user_id: int, *, window_seconds: float = 20.0, join_threshold: int = 8) -> list[ModerationSignal]:
        now = time.monotonic()
        window = max(5.0, float(window_seconds))
        threshold = max(3, int(join_threshold))
        with self._lock:
            self._cleanup_locked(now)
            bucket = self._joins[int(guild_id)]
            bucket.append((now, int(user_id)))
            cutoff = now - window
            while bucket and bucket[0][0] < cutoff:
                bucket.popleft()
            if len(bucket) < threshold:
                return []
            return [ModerationSignal(
                "raid_like", min(0.99, 0.90 + 0.01 * (len(bucket) - threshold)),
                f"{len(bucket)} حسابات انضمت خلال {int(window)} ثوانٍ.", "join_burst"
            )]

    def should_deduplicate(self, guild_id: int, user_id: int, content: str, ttl: float = 2.0, event_id=None) -> bool:
        normalized = self.normalize(content)
        key = ("event", int(guild_id), int(user_id), str(event_id)) if event_id is not None else ("content", int(guild_id), int(user_id), normalized)
        now = time.monotonic()
        with self._lock:
            self._cleanup_locked(now)
            previous = self._recent.get(key)
            self._recent[key] = now
            return bool(previous is not None and now - previous < max(0.1, float(ttl)))



moderation_engine = ModerationEngine()
