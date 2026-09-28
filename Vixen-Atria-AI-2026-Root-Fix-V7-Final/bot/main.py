"""
main.py
--------
نقطة تشغيل البوت الرئيسية.
يقوم بتحميل التوكن من ملف .env، تفعيل الـ Intents اللازمة،
تحميل جميع الـ Cogs (الوحدات)، وتشغيل سيرفر لوحة التحكم المدمج.
"""

import asyncio
import os
import sys
import logging
import time
import shutil

try:
    from rich.logging import RichHandler
except ImportError:  # optional terminal enhancement; standard logging remains functional
    RichHandler = None

import discord
from discord.ext import commands
from dotenv import load_dotenv

# يسمح بتشغيل الملف مباشرة من أي مكان بدون مشاكل استيراد
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before importing services that read provider configuration at module import time.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from bot.utils.data_manager import load_settings
from bot.services.ai_provider import ai_provider
from bot.web.server import start_web_server

TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
GUILD_ID = (os.getenv("GUILD_ID") or "").strip()

if RichHandler is not None and sys.stderr.isatty():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, markup=False, show_path=False)],
    )
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
logger = logging.getLogger("Vixen")

VIXEN_BANNER = r"""
.:::::::.         .:::::::.       .::::::::::::::::.       .:::::::.           .:::::::.
 ':::::::.       .:::::::'        '::::::::::::::::::.     ':::::::.         .:::::::'
  ':::::::.     .:::::::'         '::::::::::::::::::.      ':::::::.       .:::::::'
   ':::::::.   .:::::::'          '::::::.                   ':::::::.     .:::::::'
    ':::::::. .:::::::'           '::::::.                    ':::::::.   .:::::::'
     ':::::::..:::::::'            '::::::::::::::.             ':::::::..:::::::'
      ':::::::::::::'             '::::::::::::::.              '::::::::::::'
       ':::::::::::'              '::::::.                     .::::::::::::.
        ':::::::::'               '::::::.                    .:::::::.  .:::::::.
         ':::::::'                '::::::::::::::::::.       .:::::::.    .:::::::.
          ':::::'                 '::::::::::::::::::.      .:::::::.      .:::::::.
           ':::'                  '::::::::::::::::::.     .:::::::.        .:::::::.
            ''                    '::::::::::::::::::.    .:::::::.          .:::::::.
""".strip("\n")

VIXEN_VERSION = os.getenv("VIXEN_VERSION", "2026.09.28-dm-operations-v5")


def render_startup_banner():
    """Render the exact VEX mark on every process start with a lightweight TTY animation."""
    lines = VIXEN_BANNER.splitlines()
    if not sys.stdout.isatty():
        logger.info("VEX // ATRIA AI SYSTEM %s\n%s", VIXEN_VERSION, VIXEN_BANNER)
        return
    columns = shutil.get_terminal_size((120, 24)).columns
    print(f"\nVEX // ATRIA AI SYSTEM {VIXEN_VERSION}  |  terminal={columns}\n")
    for line in lines:
        print(line)
        sys.stdout.flush()
        time.sleep(0.018)
    print("\n✓ startup pipeline → Discord • Dashboard • AI • Memory • Workers\n")



async def system_heartbeat():
    """Emit an honest one-line health heartbeat every 60 seconds without spamming the terminal."""
    frames = ("·", "•", "●", "•")
    frame_index = 0
    while True:
        await asyncio.sleep(60)
        try:
            atria_cog = bot.get_cog("Atria")
            ai_state = getattr(atria_cog, "runtime_status", lambda: {})() if atria_cog else {}
            provider = ai_provider.status()
            metrics = provider.get("metrics", {}) if isinstance(provider.get("metrics", {}), dict) else {}
            marker = frames[frame_index % len(frames)]
            frame_index += 1
            logger.info(
                "%s VEX heartbeat | discord=%s | dashboard=%s | ai=%s | chat=%s | moderation=%s | req=%s ok=%s fail=%s 429-recovered=%s continued=%s active=%s circuit=%s",
                marker,
                "connected" if bot.is_ready() else "offline",
                "started" if getattr(bot, "_dashboard_runner", None) else "offline",
                provider.get("status", "unknown"),
                bool(ai_state.get("chat_enabled", False)),
                bool(ai_state.get("moderation_enabled", False)),
                metrics.get("requests", 0),
                metrics.get("successes", 0),
                metrics.get("failures", 0),
                metrics.get("rate_limit_recoveries", 0),
                metrics.get("continuations", 0),
                provider.get("active_requests", 0),
                "open" if provider.get("circuit_open") else "closed",
            )
        except Exception:
            logger.exception("Vixen heartbeat collection failed")


settings = load_settings()
PREFIX = (os.getenv("BOT_PREFIX") or settings.get("bot", {}).get("prefix", "!") or "!").strip()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
bot._vixen_started_at = time.monotonic()

CRITICAL_COGS = {
    "bot.cogs.atria",
    "bot.cogs.control",
}

COGS = [
    "bot.cogs.economy",
    "bot.cogs.bank",
    "bot.cogs.gambling",
    "bot.cogs.shop",
    "bot.cogs.leaderboard",
    "bot.cogs.admin",
    "bot.cogs.general",
    "bot.cogs.control",
    "bot.cogs.atria",
    # --- النظام الاحترافي ---
    "bot.cogs.staff",     # الرتب الإدارية Dev/Founder/Team
    "bot.cogs.levels",    # الخبرة والمستويات
    "bot.cogs.stocks",    # الأسهم والاستثمار
    "bot.cogs.loans",     # القروض
    "bot.cogs.rob",       # السرقة
    "bot.cogs.career",    # الوظائف والبزنس
]


@bot.event
async def on_ready():
    logger.info(f"تم تسجيل الدخول باسم {bot.user} (ID: {bot.user.id}) | VEX {VIXEN_VERSION}")
    try:
        if not getattr(bot, "_vixen_commands_synced", False):
            if GUILD_ID:
                guild = discord.Object(id=int(GUILD_ID))
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
            else:
                synced = await bot.tree.sync()
            bot._vixen_commands_synced = True
            logger.info(f"تمت مزامنة {len(synced)} أمر Slash.")
        else:
            logger.info("تم تخطي مزامنة Slash المتكررة بعد إعادة الاتصال.")
        atria_cog = bot.get_cog("Atria")
        if atria_cog is None:
            logger.error("Atria Cog غير محمّل؛ AI Chat وAI Moderation لن يستقبلا رسائل Discord.")
        else:
            logger.info("AI runtime جاهز: %s", atria_cog.runtime_status())
        if not bot.intents.message_content:
            logger.error("Message Content Intent is disabled in code; AI Chat and AI Moderation cannot read message text.")
        else:
            logger.info("Message Content Intent is requested by the bot; ensure it is also enabled in Discord Developer Portal.")
    except Exception as e:
        logger.error(f"فشل مزامنة الأوامر: {e}")

    bot_settings = load_settings().get("bot", {})
    presence = bot_settings.get("presence", {}) if isinstance(bot_settings, dict) else {}
    presence_type = presence.get("type", "playing") if isinstance(presence, dict) else "playing"
    presence_name = presence.get("name", f"{PREFIX}help | نظام العملات") if isinstance(presence, dict) else f"{PREFIX}help | نظام العملات"
    activity_types = {
        "playing": discord.ActivityType.playing,
        "listening": discord.ActivityType.listening,
        "watching": discord.ActivityType.watching,
    }
    if not isinstance(presence_type, str) or presence_type not in activity_types:
        presence_type = "playing"
    if not isinstance(presence_name, str) or not presence_name.strip():
        presence_name = f"{PREFIX}help | نظام العملات"
    activity = discord.Activity(type=activity_types.get(presence_type, discord.ActivityType.playing), name=presence_name)
    await bot.change_presence(activity=activity, status=discord.Status.online)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ ناقص معلومة: `{error.param.name}`")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ لازم تنتظر `{error.retry_after:.1f}` ثانية قبل ما تعيد المحاولة.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 ما عندك صلاحية تسوي هذا الأمر.")
        return
    logger.error(f"خطأ غير متوقع: {error}")
    await ctx.send("❌ صار خطأ غير متوقع أثناء تنفيذ الأمر.")


async def load_cogs():
    failures = []
    for cog in COGS:
        try:
            await bot.load_extension(cog)
            logger.info("تم تحميل: %s", cog)
        except Exception as exc:
            failures.append((cog, exc))
            logger.exception("فشل تحميل %s", cog)
    critical_failures = [(name, exc) for name, exc in failures if name in CRITICAL_COGS]
    if critical_failures:
        summary = "; ".join(f"{name}: {type(exc).__name__}" for name, exc in critical_failures)
        raise RuntimeError(f"Critical cogs failed to load: {summary}")
    if failures:
        logger.warning("تم تشغيل البوت مع %d cog اختيارية غير محملة: %s", len(failures), ", ".join(name for name, _ in failures))
    return failures


async def main():
    render_startup_banner()
    async with bot:
        await load_cogs()
        # تشغيل لوحة التحكم (نفس العملية — كل تعديل باللوحة يظهر فورًا بالبوت)
        dashboard_started = await start_web_server(bot)
        heartbeat_task = asyncio.create_task(system_heartbeat(), name="vixen-system-heartbeat")
        bot._vixen_heartbeat_task = heartbeat_task
        try:
            if not TOKEN or TOKEN == "ضع_التوكن_هنا":
                logger.error("لم يتم ضبط DISCORD_TOKEN! افتح ملف .env وضع التوكن الصحيح.")
                if dashboard_started:
                    logger.warning("لوحة التحكم تعمل بوضع الإعداد فقط حتى تتم إضافة DISCORD_TOKEN و OWNER_ID.")
                    await asyncio.Event().wait()
                return
            await bot.start(TOKEN)
        finally:
            try:
                from bot.services.ai_chat import ai_chat_service
                await ai_chat_service.close()
            except Exception:
                logger.exception("Could not close AI Chat background tasks")
            heartbeat = getattr(bot, "_vixen_heartbeat_task", None)
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
                finally:
                    delattr(bot, "_vixen_heartbeat_task")
            runner = getattr(bot, "_dashboard_runner", None)
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:
                    logger.exception("Could not close Dashboard server")
                finally:
                    delattr(bot, "_dashboard_runner")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("تم إيقاف البوت.")
