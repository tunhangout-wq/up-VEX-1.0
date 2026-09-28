"""Reliable, persistent Dashboard DM bulk-campaign worker."""

import asyncio
import logging
import time
from uuid import uuid4

import discord

from bot.services.dm_center import render_message
from bot.utils.dm_store import DMStore
from bot.utils.ai_store import ai_store

logger = logging.getLogger("Vixen.DMBulk")


class DMBulkManager:
    """Runs one or more persisted campaigns without losing per-recipient state."""

    def __init__(self, store: DMStore, *, max_jobs=2):
        self.store = store
        self.max_jobs = max(1, int(max_jobs))
        self._tasks = {}
        self._active_items = {}
        self._bot = None
        self._stopping = False
        self._semaphore = asyncio.Semaphore(self.max_jobs)

    def bind_bot(self, bot):
        self._bot = bot

    async def start(self):
        self._stopping = False
        self.store.mark_running_jobs_interrupted()

    async def stop(self):
        self._stopping = True
        task_ids = list(self._tasks)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Done callbacks may remove tasks from ``_tasks`` before gather returns.
        # Preserve the job IDs so every interrupted campaign is durably finalized.
        for job_id in task_ids:
            job = self.store.get_bulk_job(job_id)
            if job and job["status"] in {"queued", "running", "cancel_requested", "paused"}:
                self.store.interrupt_bulk_job(job_id)
        self._tasks.clear()
        self._active_items.clear()

    def create_and_start(self, *, guild_id, sender_id, sender_name, template_id, reason, content, embed, options, recipient_ids):
        if self._stopping:
            raise RuntimeError("DM worker is shutting down")
        if len(self._tasks) >= self.max_jobs:
            raise RuntimeError("عدد حملات DM النشطة وصل إلى الحد المسموح")
        job_id = uuid4().hex
        self.store.create_bulk_job(
            job_id, guild_id, sender_id, sender_name, template_id,
            reason, content, embed, options, recipient_ids,
        )
        task = asyncio.create_task(self._run(job_id), name=f"vixen-dm-bulk-{job_id[:8]}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))
        return job_id

    def is_running(self, job_id):
        task = self._tasks.get(str(job_id))
        return bool(task and not task.done())

    def resume_job(self, job_id):
        if self._stopping or self.is_running(job_id):
            return False
        task = asyncio.create_task(self._run(str(job_id)), name=f"vixen-dm-resume-{str(job_id)[:8]}")
        self._tasks[str(job_id)] = task
        task.add_done_callback(lambda _: self._tasks.pop(str(job_id), None))
        return True

    async def _run(self, job_id):
        async with self._semaphore:
            try:
                await self._process(job_id)
            except asyncio.CancelledError:
                job = self.store.get_bulk_job(job_id)
                if job and job["status"] in {"queued", "running", "paused", "cancel_requested"}:
                    self.store.interrupt_bulk_job(job_id, "Worker task stopped unexpectedly; send outcome is uncertain if a request was in flight.")
                self._active_items.pop(job_id, None)
                raise
            except Exception as exc:
                logger.exception("Bulk DM job crashed job=%s", job_id)
                self.store.set_bulk_job_status(job_id, "failed", f"Worker error: {type(exc).__name__}")

    async def _process(self, job_id):
        if self._bot is None:
            self.store.set_bulk_job_status(job_id, "failed", "Discord bot is not available.")
            return
        guild = next((item for item in getattr(self._bot, "guilds", []) if str(item.id) == str(self.store.get_bulk_job(job_id)["guild_id"])), None)
        if guild is None:
            self.store.set_bulk_job_status(job_id, "failed", "Discord server is unavailable.")
            return

        self.store.set_bulk_job_status(job_id, "running")
        last_attempt = 0.0
        while not self._stopping:
            job = self.store.get_bulk_job(job_id)
            if not job:
                return
            status = job["status"]
            if status == "paused":
                await asyncio.sleep(0.25)
                continue
            if status == "cancel_requested":
                self.store.cancel_pending_bulk_items(job_id)
                self.store.set_bulk_job_status(job_id, "cancelled")
                return
            if status not in {"running", "queued"}:
                return

            item = self.store.claim_next_bulk_item(job_id)
            if item is None:
                self.store.set_bulk_job_status(job_id, "completed")
                return

            member = guild.get_member(int(item["recipient_id"]))
            if member is None:
                try:
                    member = await guild.fetch_member(int(item["recipient_id"]))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            if member is None:
                error = "العضو لم يعد موجودًا في السيرفر"
                self.store.set_bulk_item_result(item["item_id"], "failed", error)
                self.store.increment_bulk_result(job_id, "failed", error)
                continue

            interval = self._interval(job.get("options") or {})
            delay = interval - (time.monotonic() - last_attempt)
            if delay > 0:
                await asyncio.sleep(delay)

            self._active_items[job_id] = item["item_id"]
            try:
                variables = self._variables(job, guild, member)
                rendered = render_message(
                    {"content": job.get("content", ""), "embed": job.get("embed")},
                    variables,
                )
                await member.send(content=rendered.get("content"), embed=rendered.get("embed"))
            except discord.Forbidden:
                error = "المستلم أغلق الرسائل الخاصة أو لا يسمح بالرسائل"
                self.store.set_bulk_item_result(item["item_id"], "failed", error)
                self.store.increment_bulk_result(job_id, "failed", error)
            except discord.HTTPException as exc:
                error = f"Discord HTTP {getattr(exc, 'status', 'error')}"
                self.store.set_bulk_item_result(item["item_id"], "failed", error)
                self.store.increment_bulk_result(job_id, "failed", error)
            except (TypeError, ValueError) as exc:
                error = str(exc)[:500] or "تصميم الرسالة غير صالح"
                self.store.set_bulk_item_result(item["item_id"], "failed", error)
                self.store.increment_bulk_result(job_id, "failed", error)
            except Exception as exc:
                logger.exception("Bulk DM recipient failed job=%s member=%s", job_id, member.id)
                error = f"Unexpected {type(exc).__name__}"
                self.store.set_bulk_item_result(item["item_id"], "uncertain", error)
                self.store.increment_bulk_result(job_id, "uncertain", error)
            else:
                self.store.set_bulk_item_result(item["item_id"], "success")
                self.store.increment_bulk_result(job_id, "success")
            self._active_items.pop(job_id, None)
            last_attempt = time.monotonic()

    @staticmethod
    def _interval(options):
        try:
            ppm = min(60.0, max(5.0, float(options.get("messages_per_minute", 20))))
        except (TypeError, ValueError, OverflowError):
            ppm = 20.0
        return max(1.0, 60.0 / ppm)

    def _variables(self, job, guild, member):
        now = discord.utils.utcnow()
        roles = ", ".join(role.name for role in getattr(member, "roles", []) if not role.is_default())
        content_blob = f"{job.get('content', '')} {job.get('embed') or {}}"
        warnings = ai_store.count_warnings(guild.id, member.id) if "{warnings}" in content_blob else 0
        return {
            "user": member.mention,
            "mention": member.mention,
            "user_id": member.id,
            "username": member.name,
            "display_name": member.display_name,
            "server": guild.name,
            "server_id": guild.id,
            "member_count": guild.member_count or 0,
            "moderator": job.get("sender_name") or "Staff",
            "reason": job.get("reason", "")[:1000],
            "warnings": warnings,
            "timestamp": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M UTC"),
            "roles": roles,
            "is_bot": "yes" if getattr(member, "bot", False) else "no",
            "server_icon": guild.icon.url if getattr(guild, "icon", None) else "",
            "bot_avatar": self._bot.user.display_avatar.url if self._bot and self._bot.user else "",
        }
