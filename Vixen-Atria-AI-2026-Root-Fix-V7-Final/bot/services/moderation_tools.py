"""Policy-checked Discord tools available to automatic AI moderation."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time

import discord

from bot.services.warnings import issue_warning
from bot.utils import ranks
from bot.utils.ai_store import ai_store

logger = logging.getLogger(__name__)

_ABUSE_ESCALATION_CLASSES = {"spam", "flood", "scam", "repeated_messages"}


class ModerationToolLayer:
    def __init__(self, store=ai_store):
        self.store = store

    @staticmethod
    def _record(bot, guild_id, target_id, requested, result, reason, *, details=""):
        try:
            actor = getattr(bot, "user", None)
            ranks.log_action(
                getattr(actor, "id", 0),
                str(actor or "Vixen AI"),
                "ai_moderation",
                f"guild={guild_id} target={target_id} requested={requested} result={result} reason={reason[:300]} {details[:500]}",
                "ai",
            )
        except Exception:
            logger.exception("Could not persist AI moderation audit event")

    @staticmethod
    def _id_in(values, target_id):
        if not isinstance(values, (list, tuple, set)):
            return False
        target = str(int(target_id))
        return any(str(value).strip() == target for value in values)

    @staticmethod
    def _trusted_reason(guild, target, policy):
        if target.id == getattr(guild, "owner_id", None):
            return "guild_owner"
        owner_id = ranks.get_owner_id()
        if owner_id is not None and target.id == owner_id:
            return "configured_owner"
        if ModerationToolLayer._id_in(policy.get("whitelist_users", []), target.id):
            return "whitelist_user"
        try:
            if any(ranks.has_permission(target.id, permission) for permission in ("moderate_members", "manage_messages", "manage_guild")):
                return "staff_permission"
        except Exception:
            logger.exception("Could not resolve Vixen staff permissions target=%s", target.id)
        target_permissions = getattr(target, "guild_permissions", None)
        if target_permissions is not None and any(
            bool(getattr(target_permissions, permission, False))
            for permission in ("administrator", "manage_messages", "moderate_members", "manage_guild")
        ):
            return "discord_permission"
        roles = getattr(target, "roles", ()) or ()
        whitelisted_roles = policy.get("whitelist_roles", [])
        for role in roles:
            if ModerationToolLayer._id_in(whitelisted_roles, getattr(role, "id", 0)):
                return "whitelist_role"
            perms = getattr(role, "permissions", None)
            if perms is not None and any(
                bool(getattr(perms, permission, False))
                for permission in ("administrator", "manage_messages", "moderate_members", "manage_guild")
            ):
                return "role_permission"
        return None

    async def _reserve_action(self, guild_id, maximum):
        return await asyncio.to_thread(self.store.reserve_moderation_action, guild_id, maximum)

    async def _send_mod_log(self, bot, guild, policy, target, *, action, result, reason, state_id, classification, deleted_count=0):
        raw_channel = policy.get("mod_log_channel") or policy.get("mod_log_channel_id")
        if not str(raw_channel).isdigit():
            return
        channel_id = int(raw_channel)
        channel = getattr(bot, "get_channel", lambda _id: None)(channel_id)
        if channel is None:
            channel = getattr(guild, "get_channel", lambda _id: None)(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        content = (
            f"🛡️ AI Moderation | {state_id}\n"
            f"member={getattr(target, 'id', 0)} | classification={classification or 'unknown'} | "
            f"action={action} | result={result}\nreason={str(reason)[:500]}\n"
            f"deleted_messages={int(deleted_count)}"
        )
        try:
            await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not send AI moderation log guild=%s channel=%s", guild.id, channel_id)
        except Exception:
            logger.exception("Unexpected AI moderation log failure guild=%s", guild.id)

    async def _delete_offending_messages(self, message, *, window_seconds=8.0, limit=50):
        guild = getattr(message, "guild", None)
        channel = getattr(message, "channel", None)
        bot_member = getattr(guild, "me", None)
        if guild is None or channel is None or bot_member is None:
            return 0, "missing_context"
        permissions = getattr(channel, "permissions_for", lambda _member: None)(bot_member)
        if permissions is None or not getattr(permissions, "manage_messages", False):
            return 0, "blocked_by_permissions"
        candidates = []
        author_id = getattr(getattr(message, "author", None), "id", None)
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(seconds=max(2.0, float(window_seconds)))
        try:
            if hasattr(channel, "history"):
                async for item in channel.history(limit=max(10, min(100, int(limit)))):
                    if getattr(getattr(item, "author", None), "id", None) != author_id:
                        continue
                    created_at = getattr(item, "created_at", None)
                    if created_at is not None and created_at < cutoff:
                        continue
                    candidates.append(item)
                    if len(candidates) >= limit:
                        break
            if message not in candidates:
                candidates.insert(0, message)
        except Exception:
            logger.exception("Could not enumerate moderation deletion candidates guild=%s", guild.id)
            candidates = [message]
        deleted = 0
        for item in candidates[: max(1, min(100, int(limit)))]:
            try:
                await item.delete()
                deleted += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        return deleted, "success"

    async def _execute_core(self, bot, guild, target, action, reason, policy, *, source_channel=None, message=None, timeout_minutes=10, classification=None, window_seconds=8.0):
        tool_names = {"warn": "warn_member", "timeout": "timeout_member", "ban": "ban_member"}
        tool = tool_names.get(action)
        reason = str(reason or "AI moderation")[:500]
        if not tool:
            return {"tool": None, "executed": False, "result": "ignored", "state_id": "AI-ignored"}
        if not isinstance(policy, dict) or policy.get("enabled") is False or policy.get("kill_switch") is True:
            self._record(bot, guild.id, target.id, tool, "blocked_by_policy", reason)
            return {"tool": tool, "executed": False, "result": "blocked_by_policy", "state_id": "AI-policy"}
        allowed = policy.get("automatic_actions", ["warn", "timeout", "ban"])
        if not isinstance(allowed, list) or (action not in allowed and tool not in allowed):
            self._record(bot, guild.id, target.id, tool, "blocked_by_policy", reason)
            return {"tool": tool, "executed": False, "result": "blocked_by_policy", "state_id": "AI-policy"}
        if action == "ban" and policy.get("allow_ai_ban") is not True:
            self._record(bot, guild.id, target.id, tool, "blocked_by_policy", reason)
            return {"tool": tool, "executed": False, "result": "blocked_by_policy", "state_id": "AI-policy"}

        trusted = self._trusted_reason(guild, target, policy)
        if trusted:
            self._record(bot, guild.id, target.id, tool, "skipped_trusted", reason, details=f"trusted={trusted}")
            await self._send_mod_log(bot, guild, policy, target, action=action, result="skipped_trusted", reason=reason, state_id=f"AI-trusted-{trusted}", classification=classification)
            return {"tool": tool, "executed": False, "result": "skipped_trusted", "state_id": f"AI-trusted-{trusted}"}

        bot_member = getattr(guild, "me", None)
        bot_user_id = getattr(getattr(bot, "user", None), "id", None)
        target_top_role = getattr(target, "top_role", None)
        bot_top_role = getattr(bot_member, "top_role", None)
        try:
            hierarchy_block = target.id in {getattr(guild, "owner_id", None), bot_user_id} or (target_top_role is not None and bot_top_role is not None and target_top_role >= bot_top_role)
        except TypeError:
            hierarchy_block = target.id in {getattr(guild, "owner_id", None), bot_user_id}
        if bot_member is None or hierarchy_block:
            self._record(bot, guild.id, target.id, tool, "blocked_by_hierarchy", reason)
            await self._send_mod_log(bot, guild, policy, target, action=action, result="blocked_by_hierarchy", reason=reason, state_id="AI-hierarchy", classification=classification)
            return {"tool": tool, "executed": False, "result": "blocked_by_hierarchy", "state_id": "AI-hierarchy"}

        permissions = getattr(bot_member, "guild_permissions", None)
        required_permission = {"timeout": "moderate_members", "ban": "ban_members"}.get(action)
        if required_permission and not getattr(permissions, required_permission, False):
            self._record(bot, guild.id, target.id, tool, "blocked_by_permissions", reason)
            await self._send_mod_log(bot, guild, policy, target, action=action, result="blocked_by_permissions", reason=reason, state_id="AI-permissions", classification=classification)
            return {"tool": tool, "executed": False, "result": "blocked_by_permissions", "state_id": "AI-permissions"}
        if action == "warn" and source_channel is not None:
            channel_permissions = source_channel.permissions_for(bot_member)
            if not getattr(channel_permissions, "send_messages", False):
                self._record(bot, guild.id, target.id, tool, "blocked_by_permissions", reason)
                return {"tool": tool, "executed": False, "result": "blocked_by_permissions", "state_id": "AI-permissions"}

        try:
            maximum = max(1, min(1000, int(policy.get("max_actions_per_hour", 20))))
        except (TypeError, ValueError, OverflowError):
            maximum = 20
        if not await self._reserve_action(guild.id, maximum):
            self._record(bot, guild.id, target.id, tool, "blocked_by_rate_limit", reason)
            return {"tool": tool, "executed": False, "result": "blocked_by_rate_limit", "state_id": "AI-rate-limit"}

        state_id = f"AI-{int(time.time() * 1000)}-{target.id}"
        effective_action = action
        effective_timeout = max(1, min(60, int(timeout_minutes)))
        if classification in _ABUSE_ESCALATION_CLASSES:
            recent = 0
            try:
                recent = await asyncio.to_thread(
                    self.store.count_recent_moderation_actions,
                    guild.id, target.id, _ABUSE_ESCALATION_CLASSES, 86400,
                )
            except Exception:
                logger.exception("Could not read moderation escalation history")
            incident_number = recent + 1
            if incident_number == 1 and "warn" in allowed:
                effective_action = "warn"
            elif incident_number == 2 and "timeout" in allowed:
                effective_action = "timeout"
                effective_timeout = max(1, min(60, int(policy.get("escalation_timeout_short", 5))))
            elif incident_number >= 3 and "timeout" in allowed:
                effective_action = "timeout"
                effective_timeout = max(1, min(60, int(policy.get("escalation_timeout_long", 15))))

        deleted_count = 0
        delete_status = "not_requested"
        try:
            if effective_action == "warn":
                warning = await issue_warning(
                    guild,
                    target,
                    getattr(getattr(bot, "user", None), "id", 0),
                    "Vixen AI",
                    reason,
                    threshold=999,
                    timeout_minutes=effective_timeout,
                    allow_threshold_timeout=False,
                    source="ai",
                )
                if message is not None and classification in _ABUSE_ESCALATION_CLASSES and policy.get("delete_offending_messages", True):
                    deleted_count, delete_status = await self._delete_offending_messages(message, window_seconds=window_seconds)
                if source_channel is not None:
                    await source_channel.send(
                        f"⚠️ {target.mention}: {reason} (warning #{warning['warning_id']})",
                        delete_after=8,
                    )
                result = {"tool": "warn_member", "executed": True, "result": "success", "escalation": "warning", "state_id": state_id}
            elif effective_action == "timeout":
                await target.timeout(discord.utils.utcnow() + datetime.timedelta(minutes=effective_timeout), reason=reason)
                if message is not None and classification in _ABUSE_ESCALATION_CLASSES and policy.get("delete_offending_messages", True):
                    deleted_count, delete_status = await self._delete_offending_messages(message, window_seconds=window_seconds)
                result = {"tool": "timeout_member", "executed": True, "result": "success", "duration_minutes": effective_timeout, "state_id": state_id}
            else:
                await guild.ban(target, reason=reason, delete_message_seconds=0)
                result = {"tool": "ban_member", "executed": True, "result": "success", "state_id": state_id}
        except (discord.Forbidden, discord.HTTPException) as exc:
            result = {"tool": tool, "executed": False, "result": f"discord_error:{type(exc).__name__}", "state_id": state_id}

        result_value = result.get("result", "unknown")
        self._record(
            bot, guild.id, target.id, tool, result_value, reason,
            details=f"classification={classification or ''} effective_action={effective_action} deleted={deleted_count} delete_status={delete_status} state_id={state_id}",
        )
        if result_value == "success":
            logger.info("AI moderation tool executed guild=%s target=%s tool=%s state=%s", guild.id, target.id, effective_action, state_id)
        await self._send_mod_log(
            bot, guild, policy, target, action=effective_action, result=result_value,
            reason=reason, state_id=state_id, classification=classification, deleted_count=deleted_count,
        )
        result["deleted_messages"] = deleted_count
        result["effective_action"] = effective_action
        return result

    async def execute(self, bot, message, action, reason, policy, timeout_minutes=10, *, classification=None, window_seconds=8.0):
        return await self._execute_core(
            bot, message.guild, message.author, action, reason, policy,
            source_channel=getattr(message, "channel", None), message=message,
            timeout_minutes=timeout_minutes, classification=classification, window_seconds=window_seconds,
        )

    async def execute_member(self, bot, member, action, reason, policy, *, classification="raid_like", timeout_minutes=10):
        return await self._execute_core(
            bot, member.guild, member, action, reason, policy,
            source_channel=None, message=None, timeout_minutes=timeout_minutes, classification=classification,
        )


moderation_tools = ModerationToolLayer()
