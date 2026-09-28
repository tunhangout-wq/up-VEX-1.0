"""Vixen Discord EDR Control Center: real Discord moderation/admin operations.
All commands are hybrid commands, so every command is available as /slash and prefix.
"""
import asyncio
import ipaddress
import logging
import math
import socket
import time
from typing import Optional
from urllib.parse import urlsplit
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from bot.utils.ai_store import ai_store
from bot.utils.data_manager import load_settings
from bot.utils import ranks
from bot.services.warnings import issue_warning

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        resolved = []
        for address_family, socket_type, protocol, _, sockaddr in records:
            address = sockaddr[0]
            if not ipaddress.ip_address(address).is_global:
                raise OSError("Image URL resolves to a non-public address")
            resolved.append({
                "hostname": host,
                "host": address,
                "port": port,
                "family": address_family,
                "proto": protocol,
                "flags": 0,
            })
        if not resolved:
            raise OSError("Image URL has no public address")
        return resolved

    async def close(self):
        return None


def need(permission: str):
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            raise commands.NoPrivateMessage("This command can only be used in a server.")
        from bot.utils import ranks
        if getattr(ctx, "dashboard_session", None) is not None and ctx.dashboard_session.get("virtual"):
            if permission in ranks.DEFAULT_PERMISSIONS.get(ctx.dashboard_session.get("rank", ""), []):
                return True
        if ranks.is_owner(ctx.author.id) or ranks.has_permission(ctx.author.id, permission):
            return True
        raise commands.CheckFailure(f"Missing Vixen permission: {permission}")
    return commands.check(predicate)


class Control(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lockdown_lock = asyncio.Lock()
        self.http_session = None

    async def cog_load(self):
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), limit=4, ttl_dns_cache=300)
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10, connect=3, sock_read=5),
            connector=connector,
        )

    async def cog_unload(self):
        if self.http_session is not None and not self.http_session.closed:
            await self.http_session.close()

    async def _download_image(self, url):
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError("invalid image URL")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in (None, 443)
        ):
            raise ValueError("image URL must use public HTTPS")
        try:
            address = ipaddress.ip_address(parsed.hostname)
            if not address.is_global:
                raise ValueError("image URL must use a public host")
        except ValueError:
            if parsed.hostname.replace(".", "").isdigit():
                raise ValueError("invalid image host") from None
        if self.http_session is None or self.http_session.closed:
            raise RuntimeError("image client is unavailable")
        async with self.http_session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError("image host returned an unsuccessful response")
            if response.content_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
                raise ValueError("unsupported image content type")
            if response.content_length is not None and response.content_length > MAX_IMAGE_BYTES:
                raise ValueError("image response is too large")
            data = await response.content.read(MAX_IMAGE_BYTES + 1)
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ValueError("image response is empty or too large")
            return data

    async def cog_command_error(self, ctx, error):
        original = getattr(error, "original", error)
        if isinstance(original, (discord.Forbidden, discord.HTTPException)):
            logger.warning("Control command rejected by Discord: %s", type(original).__name__)
            await self._reply(ctx, "❌ تعذر تنفيذ العملية بسبب صلاحيات البوت أو خطأ من Discord.", True)
            return
        logger.exception("Control command failed", exc_info=original)
        await self._reply(ctx, "❌ تعذر تنفيذ العملية. راجع سجل البوت للتفاصيل.", True)

    async def cog_app_command_error(self, interaction, error):
        original = getattr(error, "original", error)
        if isinstance(original, (discord.Forbidden, discord.HTTPException)):
            logger.warning("Control slash command rejected by Discord: %s", type(original).__name__)
            message = "❌ تعذر تنفيذ العملية بسبب صلاحيات البوت أو خطأ من Discord."
        else:
            logger.error("Control slash command failed: %s", type(original).__name__, exc_info=original)
            message = "❌ تعذر تنفيذ العملية. راجع سجل البوت للتفاصيل."
        if not interaction.response.is_done():
            await interaction.response.send_message(message, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)

    @staticmethod
    def _member_hierarchy_error(ctx, member):
        guild = ctx.guild
        bot_member = guild.me if guild else None
        if guild is None or bot_member is None:
            return "تعذر التحقق من هرمية الرتب في هذا السيرفر."
        bot_user_id = getattr(getattr(ctx.bot, "user", None), "id", None)
        if member.id in (guild.owner_id, bot_user_id):
            return "لا يمكن تنفيذ هذا الإجراء على مالك السيرفر أو البوت."
        if member.top_role >= bot_member.top_role:
            return "رتبة العضو أعلى من رتبة البوت أو مساوية لها."
        if isinstance(ctx.author, discord.Member):
            if ctx.author.id != guild.owner_id and member.top_role >= ctx.author.top_role:
                return "رتبة العضو أعلى من رتبتك أو مساوية لها."
        return None

    @staticmethod
    def _role_hierarchy_error(ctx, role):
        guild = ctx.guild
        bot_member = guild.me if guild else None
        if guild is None or bot_member is None or role.is_default() or role.managed:
            return "لا يمكن تعديل هذه الرتبة."
        if role >= bot_member.top_role:
            return "رتبة البوت يجب أن تكون أعلى من الرتبة المستهدفة."
        if isinstance(ctx.author, discord.Member):
            if ctx.author.id != guild.owner_id and role >= ctx.author.top_role:
                return "رتبتك يجب أن تكون أعلى من الرتبة المستهدفة."
        return None

    def _latency_ms(self) -> int:
        """Return a stable value while the Gateway is still connecting."""
        latency = self.bot.latency
        return round(latency * 1000) if math.isfinite(latency) else 0

    async def _reply(self, ctx, text, ephemeral=False):
        if ctx.interaction:
            await ctx.interaction.response.send_message(text, ephemeral=ephemeral)
        else:
            await ctx.send(text)

    @staticmethod
    def _audit(ctx, action, details):
        try:
            ranks.log_action(
                ctx.author.id,
                str(ctx.author),
                action,
                details,
                "dashboard" if getattr(ctx, "dashboard_session", None) else "discord",
            )
        except Exception:
            logger.exception("Could not persist moderation audit action=%s", action)

    # ---------------- server ----------------
    @commands.hybrid_group(name="server", invoke_without_command=True, description="إدارة السيرفر")
    async def server(self, ctx): await self._reply(ctx, "استخدم /server ثم اختر العملية.")

    @server.command(name="lockdown", description="قفل السيرفر دفعة واحدة مع حفظ الحالة")
    @need("manage_server")
    async def server_lockdown(self, ctx): await self._lockdown(ctx.guild, True, ctx)

    @server.command(name="unlockdown", description="استعادة حالة السيرفر المحفوظة")
    @need("manage_server")
    async def server_unlockdown(self, ctx): await self._lockdown(ctx.guild, False, ctx)

    @server.command(name="name", description="تغيير اسم السيرفر")
    @app_commands.describe(name="الاسم الجديد")
    @need("manage_server")
    async def server_name(self, ctx, name: str): await ctx.guild.edit(name=name); await self._reply(ctx, "✅ تم تغيير اسم السيرفر.")

    @server.command(name="icon", description="تغيير أيقونة السيرفر من رابط")
    @app_commands.describe(url="رابط الصورة")
    @need("manage_server")
    async def server_icon(self, ctx, url: str):
        data = await self._download_image(url)
        await ctx.guild.edit(icon=data)
        await self._reply(ctx, "✅ تم تحديث الأيقونة.")

    @server.command(name="verification", description="تغيير مستوى التحقق")
    @app_commands.describe(level="0 none, 1 low, 2 medium, 3 high, 4 highest")
    @need("manage_server")
    async def server_verification(self, ctx, level: int):
        await ctx.guild.edit(verification_level=discord.VerificationLevel(max(0,min(4,level))))
        await self._reply(ctx, "✅ تم تحديث مستوى التحقق.")

    @server.command(name="slowmode", description="تغيير slowmode لكل القنوات النصية")
    @app_commands.describe(seconds="الثواني 0-21600")
    @need("manage_server")
    async def server_slowmode(self, ctx, seconds: int):
        seconds=max(0,min(21600,seconds)); changed=0
        for c in ctx.guild.text_channels:
            try: await c.edit(slowmode_delay=seconds); changed+=1
            except discord.HTTPException: pass
            await asyncio.sleep(.15)
        await self._reply(ctx,f"✅ تم تحديث {changed} قناة.")

    @server.command(name="systemchannel", description="تعيين قناة النظام")
    @app_commands.describe(channel="القناة")
    @need("manage_server")
    async def server_systemchannel(self, ctx, channel: discord.TextChannel): await ctx.guild.edit(system_channel=channel); await self._reply(ctx,"✅ تم.")

    @server.command(name="features", description="عرض إعدادات السيرفر الأساسية")
    @need("view_stats")
    async def server_features(self, ctx): await self._reply(ctx,f"📊 {ctx.guild.name}\nMembers: {ctx.guild.member_count}\nChannels: {len(ctx.guild.channels)}\nRoles: {len(ctx.guild.roles)}")

    @server.command(name="description", description="تغيير وصف السيرفر")
    @app_commands.describe(description="الوصف")
    @need("manage_server")
    async def server_description(self, ctx, description: str): await ctx.guild.edit(description=description[:1000]); await self._reply(ctx,"✅ تم.")

    @server.command(name="banner", description="تغيير بانر السيرفر من رابط")
    @app_commands.describe(url="رابط الصورة")
    @need("manage_server")
    async def server_banner(self, ctx, url: str):
        data = await self._download_image(url)
        await ctx.guild.edit(banner=data)
        await self._reply(ctx,"✅ تم.")

    # ---------------- channel ----------------
    @commands.hybrid_group(name="channel", invoke_without_command=True, description="إدارة القنوات")
    async def channel(self, ctx): await self._reply(ctx,"استخدم /channel ثم اختر العملية.")

    @channel.command(name="create", description="إنشاء قناة نصية")
    @app_commands.describe(name="اسم القناة", category="التصنيف اختياري")
    @need("manage_channels")
    async def channel_create(self, ctx, name: str, category: Optional[discord.CategoryChannel]=None): c=await ctx.guild.create_text_channel(name,category=category); await self._reply(ctx,f"✅ <#{c.id}>")

    @channel.command(name="delete", description="حذف قناة")
    @app_commands.describe(channel="القناة")
    @need("manage_channels")
    async def channel_delete(self, ctx, channel: discord.TextChannel, confirm: bool = False):
        if not confirm:
            return await self._reply(ctx, "⚠️ أعد الأمر مع confirm=true لتأكيد حذف القناة.", True)
        await channel.delete(reason=f"Vixen by {ctx.author}")
        await self._reply(ctx, "✅ تم حذف القناة.")

    @channel.command(name="rename", description="إعادة تسمية قناة")
    @app_commands.describe(channel="القناة", name="الاسم")
    @need("manage_channels")
    async def channel_rename(self, ctx, channel: discord.TextChannel, name: str): await channel.edit(name=name); await self._reply(ctx,"✅ تم.")

    @channel.command(name="topic", description="تغيير موضوع القناة")
    @app_commands.describe(channel="القناة", topic="الموضوع")
    @need("manage_channels")
    async def channel_topic(self, ctx, channel: discord.TextChannel, topic: str): await channel.edit(topic=topic[:1024]); await self._reply(ctx,"✅ تم.")

    @channel.command(name="slowmode", description="تغيير slowmode لقناة")
    @app_commands.describe(channel="القناة", seconds="الثواني")
    @need("manage_channels")
    async def channel_slowmode(self, ctx, channel: discord.TextChannel, seconds: int): await channel.edit(slowmode_delay=max(0,min(21600,seconds))); await self._reply(ctx,"✅ تم.")

    @channel.command(name="clone", description="استنساخ قناة")
    @app_commands.describe(channel="القناة")
    @need("manage_channels")
    async def channel_clone(self, ctx, channel: discord.TextChannel): c=await channel.clone(reason=f"Vixen by {ctx.author}"); await self._reply(ctx,f"✅ <#{c.id}>")

    @channel.command(name="purge", description="حذف عدد من الرسائل")
    @app_commands.describe(channel="القناة", amount="1-100")
    @need("manage_messages")
    async def channel_purge(self, ctx, channel: discord.TextChannel, amount: int, confirm: bool = False):
        if not confirm:
            return await self._reply(ctx, "⚠️ أعد الأمر مع confirm=true لتأكيد حذف الرسائل.", True)
        deleted = await channel.purge(limit=max(1, min(100, amount)))
        await self._reply(ctx, f"🧹 حُذفت {len(deleted)} رسالة.", True)

    @channel.command(name="lock", description="قفل الكتابة")
    @app_commands.describe(channel="القناة")
    @need("manage_channels")
    async def channel_lock(self, ctx, channel: discord.TextChannel): await channel.set_permissions(ctx.guild.default_role, send_messages=False); await self._reply(ctx,"🔒 تم القفل.")

    @channel.command(name="unlock", description="فتح الكتابة")
    @app_commands.describe(channel="القناة")
    @need("manage_channels")
    async def channel_unlock(self, ctx, channel: discord.TextChannel): await channel.set_permissions(ctx.guild.default_role, send_messages=None); await self._reply(ctx,"🔓 تم الفتح.")

    @channel.command(name="category", description="نقل قناة إلى تصنيف")
    @app_commands.describe(channel="القناة", category="التصنيف")
    @need("manage_channels")
    async def channel_category(self, ctx, channel: discord.TextChannel, category: discord.CategoryChannel): await channel.edit(category=category); await self._reply(ctx,"✅ تم.")

    # ---------------- member ----------------
    @commands.hybrid_group(name="member", invoke_without_command=True, description="إدارة الأعضاء")
    async def member(self, ctx): await self._reply(ctx,"استخدم /member ثم اختر العملية.")

    @commands.hybrid_command(name="userinfo", description="عرض معلومات عضو")
    @app_commands.describe(member="العضو")
    @need("view_users")
    async def userinfo(self, ctx, member: discord.Member):
        embed = discord.Embed(title=member.display_name, color=discord.Colour.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User ID", value=str(member.id), inline=True)
        embed.add_field(name="Account created", value=f"<t:{int(member.created_at.timestamp())}:R>", inline=True)
        if member.joined_at:
            embed.add_field(name="Joined server", value=f"<t:{int(member.joined_at.timestamp())}:R>", inline=True)
        roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
        embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles[:20]) or "None", inline=False)
        embed.add_field(name="Account type", value="Bot" if member.bot else "User", inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="nickname", description="تغيير أو مسح لقب عضو")
    @app_commands.describe(member="العضو", nickname="اللقب الجديد؛ اتركه فارغًا لإعادة الضبط")
    @need("moderate_members")
    async def nickname(self, ctx, member: discord.Member, nickname: Optional[str] = None):
        error = self._member_hierarchy_error(ctx, member)
        if error:
            return await self._reply(ctx, f"❌ {error}", True)
        if nickname is not None and len(nickname) > 32:
            return await self._reply(ctx, "اللقب يجب ألا يتجاوز 32 حرفًا.", True)
        await member.edit(nick=nickname, reason=f"Vixen by {ctx.author}")
        self._audit(ctx, "nickname", f"guild={ctx.guild.id} member={member.id} nickname={nickname or ''}")
        await self._reply(ctx, "تم تحديث اللقب." if nickname else "تمت إعادة اللقب الأصلي.", True)

    @commands.hybrid_command(name="banner", description="عرض بانر حساب عضو")
    @app_commands.describe(user="الحساب")
    @need("view_users")
    async def user_banner(self, ctx, user: discord.User):
        fetched = await self.bot.fetch_user(user.id)
        if fetched.banner is None:
            return await self._reply(ctx, "هذا الحساب لا يملك بانرًا عامًا.", True)
        await self._reply(ctx, fetched.banner.url)

    @commands.hybrid_command(name="serverstats", description="إحصائيات السيرفر")
    @need("view_stats")
    async def serverstats(self, ctx):
        guild = ctx.guild
        embed = discord.Embed(title=f"{guild.name} statistics", colour=discord.Colour.blurple())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Members", value=str(guild.member_count or len(guild.members)), inline=True)
        embed.add_field(name="Text channels", value=str(len(guild.text_channels)), inline=True)
        embed.add_field(name="Voice channels", value=str(len(guild.voice_channels)), inline=True)
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="Emojis", value=str(len(guild.emojis)), inline=True)
        embed.add_field(name="Created", value=f"<t:{int(guild.created_at.timestamp())}:D>", inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="uptime", description="مدة تشغيل البوت")
    @need("view_stats")
    async def uptime(self, ctx):
        started = getattr(self.bot, "_vixen_started_at", None)
        if started is None:
            return await self._reply(ctx, "Uptime غير متاح في وضع الإعداد.", True)
        seconds = max(0, int(time.monotonic() - started))
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        await self._reply(ctx, f"Uptime: {days}d {hours}h {minutes}m {seconds}s")

    @commands.hybrid_command(name="join", description="إدخال البوت إلى قناة صوتية")
    @app_commands.describe(channel="القناة الصوتية؛ الافتراضي قناتك الحالية")
    @need("move_members")
    async def join_voice(self, ctx, channel: Optional[discord.VoiceChannel] = None):
        target = channel or getattr(getattr(ctx.author, "voice", None), "channel", None)
        if target is None:
            return await self._reply(ctx, "ادخل قناة صوتية أو حدد قناة للاتصال.", True)
        bot_member = ctx.guild.me
        if bot_member is None or not target.permissions_for(bot_member).connect:
            return await self._reply(ctx, "لا يملك البوت صلاحية الاتصال بهذه القناة.", True)
        voice_client = ctx.guild.voice_client
        if voice_client and voice_client.is_connected():
            if voice_client.channel.id != target.id:
                await voice_client.move_to(target)
        else:
            await target.connect(timeout=10, reconnect=True)
        logger.info("voice_join guild=%s channel=%s actor=%s", ctx.guild.id, target.id, ctx.author.id)
        await self._reply(ctx, f"🔊 تم الاتصال بـ{target.mention}.")

    @commands.hybrid_command(name="leave", description="إخراج البوت من القناة الصوتية")
    @need("move_members")
    async def leave_voice(self, ctx):
        voice_client = ctx.guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            return await self._reply(ctx, "البوت غير متصل بقناة صوتية.", True)
        await voice_client.disconnect(force=True)
        logger.info("voice_leave guild=%s actor=%s", ctx.guild.id, ctx.author.id)
        await self._reply(ctx, "🔇 تم قطع الاتصال الصوتي.")

    @commands.hybrid_command(name="warn", description="تسجيل تحذير لعضو")
    @app_commands.describe(member="العضو", reason="سبب التحذير")
    @need("moderate_members")
    async def warn_member(self, ctx, member: discord.Member, reason: str = "No reason provided"):
        error = self._member_hierarchy_error(ctx, member)
        if error:
            return await self._reply(ctx, f"❌ {error}", True)
        config = load_settings().get("moderation", {})
        if not isinstance(config, dict):
            config = {}
        result = await issue_warning(
            ctx.guild,
            member,
            ctx.author.id,
            str(ctx.author),
            reason,
            threshold=config.get("warning_threshold", 10),
            timeout_minutes=config.get("warning_timeout_minutes", 60),
        )
        message = f"⚠️ Warning #{result['warning_id']} recorded for {member.mention}. Total: {result['warning_count']}."
        if result["escalation"] == "timeout_applied":
            message += f" Threshold reached; timeout applied for {config.get('warning_timeout_minutes', 60)} minutes."
        elif result["escalation"].startswith("blocked_"):
            message += f" Threshold escalation blocked: {result['escalation']}."
        await self._reply(ctx, message, True)

    @commands.hybrid_command(name="warnings", description="عرض تحذيرات عضو")
    @app_commands.describe(member="العضو")
    @need("moderate_members")
    async def warnings(self, ctx, member: discord.Member):
        records = ai_store.recent_warnings(ctx.guild.id, member.id, 10)
        if not records:
            return await self._reply(ctx, f"لا توجد تحذيرات مسجلة لـ{member.mention}.", True)
        lines = [
            f"#{item['warning_id']} · <t:{int(item['timestamp'])}:R> · moderator {item['moderator_id']} · {item['reason'][:180]}"
            for item in records
        ]
        await self._reply(ctx, f"تحذيرات {member.mention} ({ai_store.count_warnings(ctx.guild.id, member.id)}):\n" + "\n".join(lines), True)

    @commands.hybrid_command(name="clearwarnings", description="مسح تحذيرات عضو")
    @app_commands.describe(member="العضو", confirm="تأكيد المسح")
    @need("moderate_members")
    async def clearwarnings(self, ctx, member: discord.Member, confirm: bool = False):
        if not confirm:
            return await self._reply(ctx, "⚠️ أعد الأمر مع confirm=true لتأكيد مسح جميع التحذيرات.", True)
        error = self._member_hierarchy_error(ctx, member)
        if error:
            return await self._reply(ctx, f"❌ {error}", True)
        count = ai_store.clear_warnings(ctx.guild.id, member.id)
        ranks.log_action(ctx.author.id, str(ctx.author), "clear_warnings", f"guild={ctx.guild.id} member={member.id} count={count}")
        await self._reply(ctx, f"تم مسح {count} تحذيرًا لـ{member.mention}.", True)

    @member.command(name="timeout", description="تقييد عضو")
    @app_commands.describe(member="العضو", minutes="الدقائق")
    @need("moderate_members")
    async def member_timeout(self, ctx, member: discord.Member, minutes: int):
        error = self._member_hierarchy_error(ctx, member)
        if error:
            return await self._reply(ctx, f"❌ {error}", True)
        await member.timeout(discord.utils.utcnow() + __import__('datetime').timedelta(minutes=max(1, min(40320, minutes))), reason=f"Vixen by {ctx.author}")
        logger.info("member_timeout actor=%s target=%s minutes=%s", ctx.author.id, member.id, minutes)
        self._audit(ctx, "member_timeout", f"guild={ctx.guild.id} member={member.id} minutes={max(1, min(40320, minutes))}")
        await self._reply(ctx, "⏳ تم.")

    @member.command(name="untimeout", description="إزالة التقييد")
    @need("moderate_members")
    async def member_untimeout(self, ctx, member: discord.Member):
        error = self._member_hierarchy_error(ctx, member)
        if error:
            return await self._reply(ctx, f"❌ {error}", True)
        await member.timeout(None, reason=f"Vixen by {ctx.author}")
        logger.info("member_untimeout actor=%s target=%s", ctx.author.id, member.id)
        self._audit(ctx, "member_untimeout", f"guild={ctx.guild.id} member={member.id}")
        await self._reply(ctx, "✅ تم.")

    @member.command(name="kick", description="طرد عضو")
    @need("kick_members")
    async def member_kick(self, ctx, member: discord.Member, reason: str="Vixen moderation", confirm: bool = False):
        if not confirm:
            return await self._reply(ctx, "⚠️ أعد الأمر مع confirm=true لتأكيد الطرد.", True)
        error = self._member_hierarchy_error(ctx, member)
        if error:
            return await self._reply(ctx, f"❌ {error}", True)
        await member.kick(reason=str(reason)[:512])
        logger.info("member_kick actor=%s target=%s", ctx.author.id, member.id)
        self._audit(ctx, "member_kick", f"guild={ctx.guild.id} member={member.id} reason={str(reason)[:400]}")
        await self._reply(ctx, "👢 تم الطرد.")

    @member.command(name="ban", description="حظر عضو")
    @need("ban_members")
    async def member_ban(self, ctx, member: discord.Member, reason: str="Vixen moderation", confirm: bool = False):
        if not confirm:
            return await self._reply(ctx, "⚠️ أعد الأمر مع confirm=true لتأكيد الحظر.", True)
        error = self._member_hierarchy_error(ctx, member)
        if error:
            return await self._reply(ctx, f"❌ {error}", True)
        await ctx.guild.ban(member, reason=str(reason)[:512], delete_message_seconds=0)
        logger.info("member_ban actor=%s target=%s", ctx.author.id, member.id)
        self._audit(ctx, "member_ban", f"guild={ctx.guild.id} member={member.id} reason={str(reason)[:400]}")
        await self._reply(ctx, "🔨 تم الحظر.")

    @member.command(name="unban", description="فك حظر بواسطة ID")
    @app_commands.describe(user_id="Discord user ID")
    @need("ban_members")
    async def member_unban(self, ctx, user_id: str): await ctx.guild.unban(discord.Object(id=int(user_id))); await self._reply(ctx,"✅ تم فك الحظر.")

    @member.command(name="deafen", description="كتم صوت العضو")
    @need("deafen_members")
    async def member_deafen(self, ctx, member: discord.Member):
        error = self._member_hierarchy_error(ctx, member)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await member.edit(deafen=True); await self._reply(ctx,"🔇 تم.")

    @member.command(name="undeafen", description="إلغاء كتم الصوت")
    @need("deafen_members")
    async def member_undeafen(self, ctx, member: discord.Member):
        error = self._member_hierarchy_error(ctx, member)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await member.edit(deafen=False); await self._reply(ctx,"🔊 تم.")

    @member.command(name="mute", description="كتم عضو في الصوت")
    @need("mute_members")
    async def member_mute(self, ctx, member: discord.Member):
        error = self._member_hierarchy_error(ctx, member)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await member.edit(mute=True); await self._reply(ctx,"🔇 تم.")

    @member.command(name="unmute", description="إلغاء كتم الصوت")
    @need("mute_members")
    async def member_unmute(self, ctx, member: discord.Member):
        error = self._member_hierarchy_error(ctx, member)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await member.edit(mute=False); await self._reply(ctx,"🔊 تم.")

    @member.command(name="move", description="نقل عضو صوتيًا")
    @app_commands.describe(member="العضو", channel="القناة الصوتية")
    @need("move_members")
    async def member_move(self, ctx, member: discord.Member, channel: discord.VoiceChannel):
        error = self._member_hierarchy_error(ctx, member)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await member.move_to(channel); await self._reply(ctx,"✅ تم النقل.")

    @member.command(name="dm", description="إرسال DM لعضو")
    @app_commands.describe(member="العضو", message="الرسالة")
    @need("dm_members")
    async def member_dm(self, ctx, member: discord.Member, message: str): await member.send(message); await self._reply(ctx,"✉️ تم الإرسال.",True)

    @member.command(name="disconnect", description="فصل عضو من القناة الصوتية")
    @need("move_members")
    async def member_disconnect(self, ctx, member: discord.Member):
        error = self._member_hierarchy_error(ctx, member)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await member.move_to(None); await self._reply(ctx,"✅ تم الفصل.")

    @member.command(name="avatar", description="إرسال رابط صورة عضو")
    @need("view_users")
    async def member_avatar(self, ctx, member: discord.Member): await self._reply(ctx,member.display_avatar.url)

    # ---------------- role ----------------
    @commands.hybrid_group(name="role", invoke_without_command=True, description="إدارة الرتب")
    async def role(self, ctx): await self._reply(ctx,"استخدم /role ثم اختر العملية.")

    @role.command(name="create", description="إنشاء رتبة")
    @app_commands.describe(name="اسم الرتبة")
    @need("manage_roles")
    async def role_create(self, ctx, name: str): r=await ctx.guild.create_role(name=name,reason=f"Vixen by {ctx.author}"); await self._reply(ctx,f"✅ {r.mention}")

    @role.command(name="delete", description="حذف رتبة")
    @app_commands.describe(role="الرتبة")
    @need("manage_roles")
    async def role_delete(self, ctx, role: discord.Role, confirm: bool = False):
        if not confirm:
            return await self._reply(ctx, "⚠️ أعد الأمر مع confirm=true لتأكيد حذف الرتبة.", True)
        error = self._role_hierarchy_error(ctx, role)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await role.delete(reason=f"Vixen by {ctx.author}"); await self._reply(ctx,"✅ تم.")

    @role.command(name="add", description="إضافة رتبة لعضو")
    @need("manage_roles")
    async def role_add(self, ctx, member: discord.Member, role: discord.Role):
        error = self._member_hierarchy_error(ctx, member) or self._role_hierarchy_error(ctx, role)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await member.add_roles(role,reason=f"Vixen by {ctx.author}"); await self._reply(ctx,"✅ تم.")

    @role.command(name="remove", description="إزالة رتبة من عضو")
    @need("manage_roles")
    async def role_remove(self, ctx, member: discord.Member, role: discord.Role):
        error = self._member_hierarchy_error(ctx, member) or self._role_hierarchy_error(ctx, role)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await member.remove_roles(role,reason=f"Vixen by {ctx.author}"); await self._reply(ctx,"✅ تم.")

    @role.command(name="rename", description="إعادة تسمية رتبة")
    @need("manage_roles")
    async def role_rename(self, ctx, role: discord.Role, name: str):
        error = self._role_hierarchy_error(ctx, role)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await role.edit(name=name[:100]); await self._reply(ctx,"✅ تم.")

    @role.command(name="list", description="عرض الرتب")
    @need("view_stats")
    async def role_list(self, ctx): await self._reply(ctx,"\n".join(f"• {r.name} ({r.id})" for r in ctx.guild.roles[-30:]))

    @role.command(name="color", description="تغيير لون رتبة")
    @app_commands.describe(role="الرتبة", hex_color="مثل #7c5cff")
    @need("manage_roles")
    async def role_color(self, ctx, role: discord.Role, hex_color: str):
        error = self._role_hierarchy_error(ctx, role)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        try:
            color = discord.Color(int(hex_color.strip().lstrip("#"), 16))
        except (AttributeError, TypeError, ValueError):
            return await self._reply(ctx, "❌ لون غير صالح.", True)
        await role.edit(color=color); await self._reply(ctx,"✅ تم.")

    @role.command(name="hoist", description="إظهار الرتبة منفصلة")
    @app_commands.describe(role="الرتبة", enabled="تشغيل")
    @need("manage_roles")
    async def role_hoist(self, ctx, role: discord.Role, enabled: bool):
        error = self._role_hierarchy_error(ctx, role)
        if error: return await self._reply(ctx, f"❌ {error}", True)
        await role.edit(hoist=enabled); await self._reply(ctx,"✅ تم.")

    # ---------------- security ----------------
    @commands.hybrid_group(name="security", invoke_without_command=True, description="الأمان")
    async def security(self, ctx): await self._reply(ctx,"استخدم /security ثم اختر العملية.")

    @security.command(name="bans", description="قائمة المحظورين")
    @need("view_stats")
    async def security_bans(self, ctx):
        bans=[f"{e.user} ({e.user.id})" async for e in ctx.guild.bans(limit=None)]
        await self._reply(ctx,"🔨 المحظورون:\n"+("\n".join(bans) if bans else "لا يوجد"))

    @security.command(name="invites", description="قائمة الدعوات")
    @need("manage_server")
    async def security_invites(self, ctx):
        inv=await ctx.guild.invites(); await self._reply(ctx,"\n".join(f"{i.code} — {i.uses or 0} uses" for i in inv) or "لا توجد دعوات")

    @security.command(name="deleteinvite", description="حذف دعوة")
    @app_commands.describe(code="كود الدعوة")
    @need("manage_server")
    async def security_deleteinvite(self, ctx, code: str):
        inv=await ctx.guild.fetch_invite(code); await inv.delete(reason=f"Vixen by {ctx.author}"); await self._reply(ctx,"✅ تم.")

    @security.command(name="createinvite", description="إنشاء دعوة")
    @app_commands.describe(channel="القناة", max_uses="عدد الاستخدامات")
    @need("manage_server")
    async def security_createinvite(self, ctx, channel: discord.TextChannel, max_uses: int=0): inv=await channel.create_invite(max_uses=max(0,min(100,max_uses))); await self._reply(ctx,inv.url)

    @security.command(name="webhooks", description="عرض webhooks")
    @need("manage_server")
    async def security_webhooks(self, ctx):
        hooks=[]
        for c in ctx.guild.channels:
            if hasattr(c,'webhooks'):
                try: hooks += await c.webhooks()
                except discord.HTTPException: pass
        await self._reply(ctx,"\n".join(f"{h.name} — {h.id}" for h in hooks) or "لا توجد")

    @security.command(name="deletewebhooks", description="حذف كل webhooks")
    @need("manage_server")
    async def security_deletewebhooks(self, ctx, confirm: bool = False):
        if not confirm:
            return await self._reply(ctx, "⚠️ أعد الأمر مع confirm=true لتأكيد حذف جميع webhooks.", True)
        n=0
        for c in ctx.guild.channels:
            if hasattr(c,'webhooks'):
                try:
                    for h in await c.webhooks(): await h.delete(reason=f"Vixen by {ctx.author}"); n+=1
                except discord.HTTPException: pass
        await self._reply(ctx,f"✅ حذف {n} webhook.")

    @security.command(name="audit", description="آخر سجلات تدقيق Discord")
    @need("view_logs")
    async def security_audit(self, ctx):
        entries=[e async for e in ctx.guild.audit_logs(limit=15)]
        await self._reply(ctx,"\n".join(f"{e.action.name}: {e.user} ({e.target})" for e in entries) or "لا توجد سجلات")

    @security.command(name="automod", description="عرض قواعد AutoMod")
    @need("view_stats")
    async def security_automod(self, ctx):
        rules=await ctx.guild.fetch_automod_rules(); await self._reply(ctx,"\n".join(f"{r.name} — {r.id}" for r in rules) or "لا توجد قواعد AutoMod")

    # ---------------- utility ----------------
    @commands.hybrid_group(name="utility", invoke_without_command=True, description="أدوات الإدارة")
    async def utility(self, ctx): await self._reply(ctx,"استخدم /utility ثم اختر العملية.")

    @utility.command(name="ping", description="حالة البوت")
    async def utility_ping(self, ctx): await self._reply(ctx,f"🏓 {self._latency_ms()}ms")

    @utility.command(name="members", description="عدد الأعضاء")
    @need("view_stats")
    async def utility_members(self, ctx): await self._reply(ctx,f"👥 {ctx.guild.member_count}")

    @utility.command(name="emoji", description="إنشاء emoji من رابط")
    @app_commands.describe(name="الاسم", url="رابط الصورة")
    @need("manage_emojis")
    async def utility_emoji(self, ctx, name: str, url: str):
        data = await self._download_image(url)
        e=await ctx.guild.create_custom_emoji(name=name,image=data,reason=f"Vixen by {ctx.author}"); await self._reply(ctx,f"✅ {e}")

    @utility.command(name="sticker", description="عرض معلومات الملصقات")
    @need("view_stats")
    async def utility_sticker(self, ctx): await self._reply(ctx,"\n".join(f"{s.name} ({s.id})" for s in ctx.guild.stickers) or "لا توجد ملصقات")

    @utility.command(name="event", description="إنشاء Scheduled Event")
    @app_commands.describe(name="الاسم", start_iso="وقت البداية ISO 8601", description="الوصف")
    @need("manage_events")
    async def utility_event(self, ctx, name: str, start_iso: str, description: str=""):
        from datetime import datetime, timezone
        start=datetime.fromisoformat(start_iso.replace('Z','+00:00')).astimezone(timezone.utc)
        e=await ctx.guild.create_scheduled_event(name=name,start_time=start,end_time=start+__import__("datetime").timedelta(hours=1),entity_type=discord.EntityType.external,privacy_level=discord.PrivacyLevel.guild_only,location=ctx.guild.name,description=description)
        await self._reply(ctx,f"✅ تم إنشاء الحدث {e.name}.")

    @utility.command(name="events", description="عرض الأحداث")
    @need("view_stats")
    async def utility_events(self, ctx): ev=await ctx.guild.fetch_scheduled_events(); await self._reply(ctx,"\n".join(f"{e.name} — {e.id}" for e in ev) or "لا توجد أحداث")

    @utility.command(name="prune", description="إزالة أعضاء غير نشطين")
    @app_commands.describe(days="1-30")
    @need("kick_members")
    async def utility_prune(self, ctx, days: int=7, confirm: bool = False):
        if not confirm:
            return await self._reply(ctx, "⚠️ أعد الأمر مع confirm=true لتأكيد إزالة الأعضاء غير النشطين.", True)
        days=max(1,min(30,days))
        n=await ctx.guild.prune_members(days=days,dry=False,reason=f"Vixen prune by {ctx.author}")
        await self._reply(ctx,f"✅ تم تنفيذ Prune لمدة {days} يومًا. الأعضاء الذين أزيلوا: {n}")

    @utility.command(name="botinfo", description="معلومات البوت")
    async def utility_botinfo(self, ctx): await self._reply(ctx,f"Vixen EDR\nLatency: {self._latency_ms()}ms\nGuilds: {len(self.bot.guilds)}")

    @utility.command(name="say", description="إرسال رسالة من البوت إلى قناة")
    @app_commands.describe(channel="القناة", message="الرسالة")
    @need("manage_messages")
    async def utility_say(self, ctx, channel: discord.TextChannel, message: str): await channel.send(message[:2000]); await self._reply(ctx,"✅ تم الإرسال.",True)

    @utility.command(name="channelinfo", description="معلومات قناة")
    @app_commands.describe(channel="القناة")
    @need("view_stats")
    async def utility_channelinfo(self, ctx, channel: discord.TextChannel): await self._reply(ctx,f"#{channel.name}\nID: {channel.id}\nTopic: {channel.topic or '-'}\nSlowmode: {channel.slowmode_delay}s")

    @utility.command(name="roleinfo", description="معلومات رتبة")
    @app_commands.describe(role="الرتبة")
    @need("view_stats")
    async def utility_roleinfo(self, ctx, role: discord.Role): await self._reply(ctx,f"{role.name}\nID: {role.id}\nPosition: {role.position}\nMembers: {len(role.members)}")

    async def _lockdown(self, guild, lock, ctx):
        """قفل/فتح كل القنوات النصية بالتوازي (بدل واحدة تلو الأخرى) حتى لا
        يعلّق الطلب لثوانٍ طويلة على سيرفر فيه عدد كبير من القنوات.
        كل قناة تُحدَّث بمهلة زمنية مستقلة (10 ثوانٍ) — قناة بطيئة أو محظورة
        الوصول لن توقف بقية القنوات ولن تعلّق العملية كاملة."""
        async with self._lockdown_lock:
            await self._apply_lockdown(guild, lock, ctx)

    async def _apply_lockdown(self, guild, lock, ctx):
        from bot.utils.data_manager import atomic_update_settings, load_settings
        settings = load_settings()
        store = settings.get("lockdown_state")
        if not isinstance(store, dict):
            store = {}
            settings["lockdown_state"] = store
        gid=str(guild.id)
        semaphore=asyncio.Semaphore(5)  # يحد التزامن حتى لا يصطدم بـ rate limit دفعة واحدة

        if lock and gid in store:
            await self._reply(ctx, "🔒 القفل مطبق بالفعل؛ استخدم unlockdown لاستعادة الحالة.", True)
            return
        if not lock and gid not in store:
            await self._reply(ctx, "لا توجد حالة محفوظة لاستعادتها.", True)
            return

        async def _apply(c, send_messages_value):
            async with semaphore:
                try:
                    ow=c.overwrites_for(guild.default_role)
                    prev=ow.send_messages
                    ow.send_messages=send_messages_value
                    await asyncio.wait_for(
                        c.set_permissions(guild.default_role,overwrite=ow,
                                           reason="Vixen Full Lockdown" if lock else "Vixen Unlockdown"),
                        timeout=10,
                    )
                    return str(c.id), prev, True
                except (discord.Forbidden, discord.HTTPException, asyncio.TimeoutError):
                    return str(c.id), None, False

        if lock:
            state = {
                str(channel.id): {"send_messages": channel.overwrites_for(guild.default_role).send_messages}
                for channel in guild.text_channels
            }
            if not state:
                await self._reply(ctx, "لا توجد قنوات نصية لقفلها.", True)
                return
            persisted = {}

            def save_lockdown_state(current):
                states = current.get("lockdown_state")
                if not isinstance(states, dict):
                    states = {}
                    current["lockdown_state"] = states
                if gid in states:
                    persisted["exists"] = True
                    return False
                states[gid] = state

            try:
                atomic_update_settings(save_lockdown_state)
            except (OSError, TypeError, ValueError):
                logger.exception("Could not save lockdown state guild=%s", gid)
                await self._reply(ctx, "❌ تعذر حفظ حالة القفل؛ لم يتم قفل القنوات.", True)
                return
            if persisted.get("exists"):
                await self._reply(ctx, "🔒 القفل مطبق بالفعل؛ استخدم unlockdown لاستعادة الحالة.", True)
                return

            results = await asyncio.gather(*[_apply(c, False) for c in guild.text_channels])
            applied = sum(1 for _, _, ok in results if ok)
            skipped = sum(1 for _, _, ok in results if not ok)
            msg = f"🔒 Full Lockdown اكتمل ({applied} قناة)."
            if skipped: msg += f" تم تخطي {skipped} قناة فشلت."
            await self._reply(ctx, msg)
        else:
            state=store.get(gid,{})
            channels=[c for c in guild.text_channels if str(c.id) in state]

            async def _restore(c):
                old=state.get(str(c.id))
                async with semaphore:
                    try:
                        ow=c.overwrites_for(guild.default_role)
                        ow.send_messages=old.get("send_messages")
                        await asyncio.wait_for(
                            c.set_permissions(guild.default_role,overwrite=ow,reason="Vixen Unlockdown"),
                            timeout=10,
                        )
                        return True
                    except (discord.Forbidden, discord.HTTPException, asyncio.TimeoutError):
                        return False

            results = await asyncio.gather(*[_restore(c) for c in channels])
            restored = sum(1 for ok in results if ok)
            skipped = len(results) - restored
            remaining = {
                str(channel.id): state[str(channel.id)]
                for channel, ok in zip(channels, results)
                if not ok and str(channel.id) in state
            }
            def update_lockdown_state(current):
                states = current.get("lockdown_state")
                if not isinstance(states, dict):
                    current["lockdown_state"] = {}
                    return
                if remaining:
                    states[gid] = remaining
                else:
                    states.pop(gid, None)

            try:
                atomic_update_settings(update_lockdown_state)
            except (OSError, TypeError, ValueError):
                logger.exception("Could not save restored lockdown state guild=%s", gid)
            msg = f"🔓 Unlockdown اكتمل ({restored} قناة)."
            if skipped: msg += f" تم تخطي {skipped} قناة فشلت."
            await self._reply(ctx, msg)


async def setup(bot): await bot.add_cog(Control(bot))
