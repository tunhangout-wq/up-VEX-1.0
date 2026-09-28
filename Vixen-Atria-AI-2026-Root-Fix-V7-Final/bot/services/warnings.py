"""Warning persistence and deterministic threshold escalation."""

import datetime
import logging

import discord

from bot.utils import ranks
from bot.utils.ai_store import ai_store

logger = logging.getLogger(__name__)


async def issue_warning(
    guild,
    member,
    moderator_id,
    moderator_name,
    reason,
    threshold=10,
    timeout_minutes=60,
    allow_threshold_timeout=True,
    reserve_escalation=None,
    source="discord",
):
    reason = str(reason or "No reason provided")[:1000]
    warning_id = ai_store.add_warning(guild.id, member.id, moderator_id, reason)
    warning_count = ai_store.count_warnings(guild.id, member.id)
    dm_status = "sent"
    try:
        await member.send(
            f"You received warning #{warning_id} in {guild.name}. Reason: {reason}"
        )
    except (discord.Forbidden, discord.HTTPException):
        dm_status = "failed"

    escalation = "not_due"
    try:
        threshold = max(1, min(100, int(threshold)))
        timeout_minutes = max(1, min(40320, int(timeout_minutes)))
    except (TypeError, ValueError, OverflowError):
        threshold, timeout_minutes = 10, 60

    if warning_count == threshold:
        if not allow_threshold_timeout:
            escalation = "blocked_by_policy"
        elif reserve_escalation is not None and not await reserve_escalation():
            escalation = "blocked_by_rate_limit"
        else:
            bot_member = guild.me
            if bot_member is None or not bot_member.guild_permissions.moderate_members:
                escalation = "blocked_by_permissions"
            elif member.id in {guild.owner_id, bot_member.id}:
                escalation = "blocked_by_hierarchy"
            elif member.top_role >= bot_member.top_role:
                escalation = "blocked_by_hierarchy"
            else:
                try:
                    await member.timeout(
                        discord.utils.utcnow() + datetime.timedelta(minutes=timeout_minutes),
                        reason=f"Automatic warning threshold ({warning_count})",
                    )
                    escalation = "timeout_applied"
                except (discord.Forbidden, discord.HTTPException) as exc:
                    escalation = f"discord_error:{type(exc).__name__}"

    try:
        ranks.log_action(
            int(moderator_id),
            str(moderator_name),
            "warn_member",
            f"guild={guild.id} member={member.id} warning_id={warning_id} count={warning_count} dm={dm_status} escalation={escalation} reason={reason[:250]}",
            source,
        )
    except Exception:
        logger.exception("Could not persist warning audit event guild=%s target=%s", guild.id, member.id)
    logger.info(
        "warning recorded guild=%s target=%s warning_id=%s count=%s escalation=%s",
        guild.id,
        member.id,
        warning_id,
        warning_count,
        escalation,
    )
    return {
        "warning_id": warning_id,
        "warning_count": warning_count,
        "dm_status": dm_status,
        "escalation": escalation,
    }