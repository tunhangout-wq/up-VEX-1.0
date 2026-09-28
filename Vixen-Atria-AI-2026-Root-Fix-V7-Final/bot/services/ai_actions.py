"""Owner-only natural-language Discord actions for Vixen AI Chat.

The model can suggest one action in a private machine-readable marker. The
application, not the model, decides whether to execute it. Every action is
re-checked against the configured OWNER_ID, Discord permissions, and Discord
role hierarchy before the API call is made.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
from typing import Any

import discord

from bot.services.warnings import issue_warning
from bot.utils import ranks
from bot.utils.ai_store import ai_store

logger = logging.getLogger(__name__)

ACTION_OPEN = "<VIXEN_ACTION>"
ACTION_CLOSE = "</VIXEN_ACTION>"
_ACTION_RE = re.compile(re.escape(ACTION_OPEN) + r"\s*(.*?)\s*" + re.escape(ACTION_CLOSE), re.DOTALL)


_ACTION_DIRECTIVE_RE = re.compile(
    r"(?:\b(?:please|pls|can\s+you|could\s+you|do|make|apply|give|remove|take|set)\b\s+)?"
    r"(?P<action>"
    r"kick|ban|timeout|time\s*out|warn|untimeout|unmute|mute|unban|"
    r"تايموت|تايم\s*اوت|تايم\s*آوت|كيك|اطرد|طرد|باند|بان|ميوت|ميوتة|حذر|حذّر|تحذير|"
    r"نحي\s+التايموت|انحي\s+التايموت|فك\s+التايموت|نحي\s+التايم\s*اوت|فك\s+البان|انحي\s+البان"
    r")\b",
    re.IGNORECASE,
)
_ACTION_QUESTION_RE = re.compile(
    r"(?:[?؟]|\b(?:how|why|what|when|where|can|could|should|is|are|شنوة|شنيا|شنو|علاش|كيفاش|كيف|وقتاش|وين|شكون|تنجم|نجم|هل|واش|command\s+متاع|طريقة|كيفية)\b)",
    re.IGNORECASE,
)
_NON_EXECUTION_CONTEXT_RE = re.compile(
    r"(?:\b(?:example|examples|for\s+example|hypothetical|hypothetically|quote|quoted|quoting|i\s+wrote|i\s+said|i\s+mean|just\s+an\s+example)\b"
    r"|(?:مثال|كمثال|مثلا|مثلاً|افتراض|افتراضي|اقتباس|مقتبس|نقتبس|قلت|نقول|كي\s+نقول|لو\s+نقول|اذا\s+نقول|إذا\s+نقول|مجرد\s+مثال))",
    re.IGNORECASE | re.UNICODE,
)

_DURATION_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|ث|ثواني?|د|دق|دقايق?|دقيقة|س|ساعة|ساعات|يوم|أيام)",
    re.IGNORECASE,
)

_DIRECT_ACTION_ALIASES = {
    "kick": {"kick", "كيك", "اطرد", "طرد"},
    "ban": {"ban", "باند", "بان"},
    "timeout": {"timeout", "time out", "mute", "تايموت", "تايم اوت", "تايم آوت", "ميوت", "ميوتة"},
    "warn": {"warn", "حذر", "حذّر", "تحذير"},
    "untimeout": {"untimeout", "unmute", "نحي التايموت", "انحي التايموت", "فك التايموت", "نحي التايم اوت", "فك التايم اوت"},
    "unban": {"unban", "فك البان", "انحي البان"},
}

SUPPORTED = {
    "kick", "ban", "timeout", "untimeout", "warn", "nickname",
    "add_role", "remove_role", "create_role", "delete_role", "rename_role",
    "lock_channel", "unlock_channel", "slowmode", "unban",
}


class AIActionResult:
    def __init__(self, success: bool, message: str, *, action: str = ""):
        self.success = bool(success)
        self.message = str(message)
        self.action = str(action or "")

    def as_dict(self) -> dict[str, Any]:
        return {"success": self.success, "message": self.message, "action": self.action}


class OwnerAIActionExecutor:
    """Strict executor for natural-language admin actions."""

    def parse_direct_command(self, text: str, message=None) -> dict[str, Any] | None:
        """Parse only a high-confidence direct owner command; never parse questions.

        Direct parsing is intentionally conservative: a single explicit member mention
        or numeric target is required for member moderation actions. This keeps natural
        conversation such as "how do I kick someone?" away from the executor while
        making clear owner commands independent of model marker formatting.
        """
        value = re.sub(r"\s+", " ", str(text or "").strip())
        if not value or self._question_like(value):
            return None

        match = _ACTION_DIRECTIVE_RE.search(value)
        if not match:
            return None
        raw_action = str(match.group("action") or "").strip().lower()
        action = None
        normalized_action = re.sub(r"\s+", " ", raw_action)
        for candidate, aliases in _DIRECT_ACTION_ALIASES.items():
            if normalized_action in {re.sub(r"\s+", " ", a.casefold()) for a in aliases}:
                action = candidate
                break
        if action is None:
            return None

        mentioned = list(getattr(message, "mentions", []) or []) if message is not None else []
        target = None
        if len(mentioned) == 1 and action != "unban":
            target = f"<@{getattr(mentioned[0], 'id', 0)}>"
        else:
            ids = re.findall(r"(?<!\d)(\d{15,22})(?!\d)", value)
            if action == "unban" and ids:
                return {
                    "execute": True, "action": "unban", "user_id": ids[-1],
                    "reason": self._direct_reason(value),
                }
            if ids and action != "unban":
                target = ids[-1]

        if not target:
            return None

        payload: dict[str, Any] = {
            "execute": True,
            "action": action,
            "target": target,
            "reason": self._direct_reason(value),
        }
        if action == "timeout":
            duration = _DURATION_RE.search(value[match.end():])
            if duration:
                minutes = self._duration_to_minutes(duration)
                if minutes is not None:
                    payload["minutes"] = minutes
        return payload

    @staticmethod
    def _question_like(value: str) -> bool:
        lowered = str(value or "").strip().casefold()
        if "?" in lowered or "؟" in lowered:
            return True
        if _NON_EXECUTION_CONTEXT_RE.search(lowered):
            return True
        return bool(_ACTION_QUESTION_RE.search(lowered))

    @staticmethod
    def _duration_to_minutes(match) -> int | None:
        try:
            value = float(match.group("num"))
        except (TypeError, ValueError, OverflowError):
            return None
        if value <= 0:
            return None
        unit = str(match.group("unit") or "m").casefold()
        if unit.startswith(("s", "ث")):
            value /= 60.0
        elif unit.startswith(("h", "س")):
            value *= 60.0
        elif unit.startswith(("d", "ي")):
            value *= 1440.0
        return max(1, min(40320, int(round(value))))

    @staticmethod
    def _direct_reason(value: str) -> str:
        match = re.search(r"(?:\b(?:because|for|reason)\b|(?:علاش|بسبب|على خاطر))\s+(.+)$", value, re.IGNORECASE)
        if match:
            return str(match.group(1)).strip()[:512]
        return "Vixen AI direct owner action"

    def parse(self, answer: str) -> tuple[str, dict[str, Any] | None]:
        text = str(answer or "")
        match = _ACTION_RE.search(text)
        if not match:
            return text, None
        visible = _ACTION_RE.sub("", text).strip()
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError):
            return visible, None
        if not isinstance(payload, dict) or payload.get("execute") is not True:
            return visible, None
        action = str(payload.get("action", "")).strip().lower()
        if action not in SUPPORTED:
            return visible, None
        return visible, payload

    def stream_visible(self, full_answer: str) -> str:
        text = str(full_answer or "")
        marker = text.find(ACTION_OPEN)
        if marker >= 0:
            text = text[:marker]
        # Never leak an incomplete marker or internal action fields to users.
        text = re.sub(r"<VIXEN_ACTION>.*$", "", text, flags=re.DOTALL)
        return text.strip()

    @staticmethod
    async def _member_candidates(guild: discord.Guild, query: str, message=None):
        q = str(query or "").strip()
        if not q:
            return []
        q = re.sub(r"^<@!?(\d+)>$", r"\1", q)
        if message is not None:
            mentioned = list(getattr(message, "mentions", []) or [])
            if q.isdigit():
                exact_id = [m for m in mentioned if str(getattr(m, "id", "")) == q]
                if exact_id:
                    return exact_id
            exact = [m for m in mentioned if str(getattr(m, "display_name", "")).casefold() == q.casefold()
                     or str(getattr(m, "name", "")).casefold() == q.casefold()]
            if exact:
                return exact
        if q.isdigit():
            member = guild.get_member(int(q))
            if member is not None:
                return [member]
            try:
                member = await guild.fetch_member(int(q))
            except (discord.NotFound, discord.HTTPException):
                member = None
            return [member] if member else []
        candidates = []
        key = q.casefold()
        for member in getattr(guild, "members", []) or []:
            if str(getattr(member, "display_name", "")).casefold() == key or str(getattr(member, "name", "")).casefold() == key:
                candidates.append(member)
        return candidates

    @staticmethod
    def _role_candidates(guild: discord.Guild, query: str, message=None):
        q = str(query or "").strip()
        if not q:
            return []
        q = re.sub(r"^<@&(\d+)>$", r"\1", q)
        if message is not None:
            mentioned = list(getattr(message, "role_mentions", []) or [])
            if q.isdigit():
                exact_id = [r for r in mentioned if str(getattr(r, "id", "")) == q]
                if exact_id:
                    return exact_id
            exact = [r for r in mentioned if str(getattr(r, "name", "")).casefold() == q.casefold()]
            if exact:
                return exact
        if q.isdigit():
            role = guild.get_role(int(q))
            return [role] if role else []
        key = q.casefold()
        return [role for role in getattr(guild, "roles", []) or [] if str(getattr(role, "name", "")).casefold() == key]

    @staticmethod
    def _channel_candidates(guild: discord.Guild, query: str, message=None):
        q = str(query or "").strip()
        if not q:
            return []
        q = re.sub(r"^<#(\d+)>$", r"\1", q)
        if message is not None:
            mentioned = list(getattr(message, "channel_mentions", []) or [])
            if q.isdigit():
                exact_id = [c for c in mentioned if str(getattr(c, "id", "")) == q]
                if exact_id:
                    return exact_id
        if q.isdigit():
            channel = guild.get_channel(int(q))
            return [channel] if channel else []
        key = q.lstrip("#").casefold()
        return [c for c in getattr(guild, "text_channels", []) or [] if str(getattr(c, "name", "")).casefold() == key]

    @staticmethod
    def _member_error(guild: discord.Guild, bot: discord.ClientUser, member: discord.Member):
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            return "ما نجمتش نتحقق من رتبة البوت في السيرفر."
        if member.id in {guild.owner_id, getattr(bot, "id", None)}:
            return "ما نجمش نطبّقها على مالك السيرفر أو البوت."
        if member.top_role >= bot_member.top_role:
            return "رتبة العضو أعلى من رتبة البوت أو مساوية لها."
        return None

    @staticmethod
    def _role_error(guild: discord.Guild, role: discord.Role):
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            return "ما نجمتش نتحقق من رتبة البوت."
        if role.is_default() or role.managed:
            return "ما نجمش نعدّل الرتبة هاذي."
        if role >= bot_member.top_role:
            return "رتبة البوت لازم تكون أعلى من الرتبة المستهدفة."
        return None

    @staticmethod
    def _permission_error(guild: discord.Guild, action: str):
        bot_member = getattr(guild, "me", None)
        if bot_member is None:
            return "البوت موش متاح في السيرفر."
        permission_map = {
            "kick": "kick_members", "ban": "ban_members", "timeout": "moderate_members",
            "untimeout": "moderate_members", "warn": "send_messages", "nickname": "manage_nicknames",
            "add_role": "manage_roles", "remove_role": "manage_roles", "create_role": "manage_roles",
            "delete_role": "manage_roles", "rename_role": "manage_roles", "lock_channel": "manage_channels",
            "unlock_channel": "manage_channels", "slowmode": "manage_channels", "unban": "ban_members",
        }
        perm = permission_map.get(action)
        if perm and not getattr(bot_member.guild_permissions, perm, False):
            return f"البوت ما عندوش صلاحية Discord المطلوبة: {perm}."
        return None

    @staticmethod
    def _audit(message, action: str, target: str, result: str, detail: str):
        try:
            ranks.log_action(
                int(message.author.id), str(message.author), f"ai_owner_{action}",
                f"guild={message.guild.id} target={target} result={result} detail={detail[:400]}",
                "discord_ai",
            )
        except Exception:
            logger.exception("Failed to write AI owner-action audit")

    async def execute(self, bot, message, payload: dict[str, Any]) -> AIActionResult:
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        if guild is None or author is None:
            return AIActionResult(False, "الإجراء الإداري يخدم داخل السيرفر فقط.")
        if not ranks.is_owner(author.id):
            return AIActionResult(False, "الإجراءات الإدارية بالـAI مسموحة للـOwner الموثّق بالـID فقط.")

        action = str(payload.get("action", "")).strip().lower()
        if action not in SUPPORTED:
            return AIActionResult(False, "الإجراء المطلوب غير مدعوم.", action=action)
        perm_error = self._permission_error(guild, action)
        if perm_error:
            self._audit(message, action, "?", "blocked_permission", perm_error)
            return AIActionResult(False, f"❌ {perm_error}", action=action)

        reason = str(payload.get("reason") or "Vixen AI action requested by the verified owner").strip()[:512]
        bot_user = getattr(bot, "user", None)

        try:
            if action in {"kick", "ban", "timeout", "untimeout", "warn", "nickname", "add_role", "remove_role"}:
                candidates = await self._member_candidates(guild, payload.get("target") or payload.get("target_id"), message)
                if len(candidates) != 1:
                    text = "ما لقيتش عضو واحد واضح بالاسم/ID. ابعث mention أو الـID باش ما نضربش الغلط."
                    self._audit(message, action, str(payload.get("target") or "?"), "needs_clarification", text)
                    return AIActionResult(False, f"❓ {text}", action=action)
                member = candidates[0]

            if action in {"kick", "ban", "timeout", "untimeout", "warn", "nickname", "add_role", "remove_role"}:
                hierarchy = self._member_error(guild, bot_user, member)
                if hierarchy:
                    self._audit(message, action, str(member.id), "blocked_hierarchy", hierarchy)
                    return AIActionResult(False, f"❌ {hierarchy}", action=action)

            if action == "kick":
                await member.kick(reason=reason)
                result = f"✅ تطرد {member.mention} بنجاح."
            elif action == "ban":
                await guild.ban(member, reason=reason, delete_message_seconds=0)
                result = f"✅ تعمل ban لـ{member.mention} بنجاح."
            elif action == "timeout":
                minutes = max(1, min(40320, int(payload.get("minutes", 10))))
                await member.timeout(discord.utils.utcnow() + datetime.timedelta(minutes=minutes), reason=reason)
                result = f"✅ عطيت {member.mention} timeout لمدة {minutes} دقيقة."
            elif action == "untimeout":
                await member.timeout(None, reason=reason)
                result = f"✅ نحّيت timeout على {member.mention}."
            elif action == "warn":
                warning = await issue_warning(
                    guild, member, author.id, str(author), reason,
                    threshold=10, timeout_minutes=60,
                    source="discord_ai_owner",
                )
                result = f"✅ عملت warning لـ{member.mention} (#{warning['warning_id']})."
            elif action == "nickname":
                nickname = payload.get("nickname")
                nickname = None if nickname is None or str(nickname).strip().lower() in {"none", "reset", "clear", "null"} else str(nickname).strip()
                if nickname is not None and len(nickname) > 32:
                    return AIActionResult(False, "❌ الـnickname ما يفوتش 32 حرف.", action=action)
                await member.edit(nick=nickname, reason=reason)
                result = f"✅ تبدّل nickname متاع {member.mention}."
            elif action in {"add_role", "remove_role"}:
                role_candidates = self._role_candidates(guild, payload.get("role") or payload.get("role_id"), message)
                if len(role_candidates) != 1:
                    text = "❓ يلزمني role واضحة بالاسم أو mention/ID."
                    self._audit(message, action, str(payload.get("role") or "?"), "needs_clarification", text)
                    return AIActionResult(False, text, action=action)
                role = role_candidates[0]
                role_error = self._role_error(guild, role)
                if role_error:
                    self._audit(message, action, str(role.id), "blocked_hierarchy", role_error)
                    return AIActionResult(False, f"❌ {role_error}", action=action)
                if action == "add_role":
                    await member.add_roles(role, reason=reason)
                    result = f"✅ عطيت {role.mention} لـ{member.mention}."
                else:
                    await member.remove_roles(role, reason=reason)
                    result = f"✅ نحّيت {role.mention} من {member.mention}."
            elif action == "create_role":
                name = str(payload.get("name") or "New Role").strip()[:100]
                role = await guild.create_role(name=name, reason=reason)
                result = f"✅ خلقت role: {role.mention}."
            elif action in {"delete_role", "rename_role"}:
                role_candidates = self._role_candidates(guild, payload.get("role") or payload.get("role_id"), message)
                if len(role_candidates) != 1:
                    return AIActionResult(False, "❓ يلزمني role واضحة بالاسم أو ID.", action=action)
                role = role_candidates[0]
                role_error = self._role_error(guild, role)
                if role_error:
                    return AIActionResult(False, f"❌ {role_error}", action=action)
                if action == "delete_role":
                    await role.delete(reason=reason)
                    result = f"✅ تحذفت role `{role.name}`."
                else:
                    name = str(payload.get("name") or "").strip()[:100]
                    if not name:
                        return AIActionResult(False, "❌ ابعث الاسم الجديد للrole.", action=action)
                    old = role.name
                    await role.edit(name=name, reason=reason)
                    result = f"✅ تبدّل اسم role من `{old}` إلى `{name}`."
            elif action in {"lock_channel", "unlock_channel", "slowmode"}:
                candidates = self._channel_candidates(guild, payload.get("channel") or payload.get("channel_id"), message)
                if len(candidates) != 1:
                    candidates = [getattr(message, "channel", None)] if not (payload.get("channel") or payload.get("channel_id")) else candidates
                if len(candidates) != 1 or candidates[0] is None:
                    return AIActionResult(False, "❓ يلزمني channel واضحة بالاسم أو mention/ID.", action=action)
                channel = candidates[0]
                if action == "lock_channel":
                    await channel.set_permissions(guild.default_role, send_messages=False, reason=reason)
                    result = f"✅ تقفلت {channel.mention}."
                elif action == "unlock_channel":
                    await channel.set_permissions(guild.default_role, send_messages=None, reason=reason)
                    result = f"✅ تحلّت الكتابة في {channel.mention}."
                else:
                    seconds = max(0, min(21600, int(payload.get("seconds", 0))))
                    await channel.edit(slowmode_delay=seconds, reason=reason)
                    result = f"✅ slowmode في {channel.mention}: {seconds} ثانية."
            elif action == "unban":
                user_id = str(payload.get("user_id") or payload.get("target_id") or "").strip()
                if not user_id.isdigit():
                    return AIActionResult(False, "❓ فك البان يحتاج User ID رقمي واضح.", action=action)
                await guild.unban(discord.Object(id=int(user_id)), reason=reason)
                result = f"✅ فكيت البان على `{user_id}`."
            else:
                return AIActionResult(False, "❌ الإجراء غير معروف.", action=action)
        except (discord.Forbidden, discord.HTTPException) as exc:
            result = f"❌ Discord رفض العملية: {type(exc).__name__}."
            self._audit(message, action, str(payload.get("target") or payload.get("role") or payload.get("channel") or "?"), "discord_error", str(exc))
            return AIActionResult(False, result, action=action)
        except (TypeError, ValueError, OverflowError) as exc:
            self._audit(message, action, "?", "invalid_input", str(exc))
            return AIActionResult(False, "❌ الطلب فيه قيمة غير صالحة.", action=action)
        except Exception:
            logger.exception("Unexpected owner AI action failure action=%s", action)
            self._audit(message, action, "?", "internal_error", "unexpected internal exception")
            return AIActionResult(False, "❌ صار خطأ داخلي، وما باش نقولك نجحت العملية وهي ما نجحتش.", action=action)

        self._audit(message, action, str(payload.get("target") or payload.get("role") or payload.get("channel") or "?"), "success", reason)
        return AIActionResult(True, result, action=action)


owner_ai_actions = OwnerAIActionExecutor()
