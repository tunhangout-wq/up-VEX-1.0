"""
web/server.py
--------------
سيرفر لوحة التحكم المدمج مع البوت (aiohttp — نفس عملية البوت).

هذا يعني أن أي عملية تسويها باللوحة (إضافة فلوس، تعديل إعدادات...)
تنفذ فورًا على نفس بيانات البوت — بدون تصدير ملفات أو إعادة تشغيل!

المزايا:
  - تسجيل دخول بكود الفريق (Dev/Founder/Team) أو بآيدي المالك + كلمة المرور
  - صلاحيات لكل رتبة (نفس صلاحيات البوت تمامًا)
  - إحصائيات مباشرة: عدد المستخدمين، إجمالي الفلوس، القروض، الأسهم
  - إدارة فلوس الأعضاء (إضافة لنفسه أو لغيره، سحب، تصفير)
  - إدارة أكواد الفريق (توليد/سحب)
  - تعديل كل إعدادات البوت مباشرة
  - سجل عمليات مباشر
"""

import os
import time
import secrets
import logging
import json
import inspect
import hashlib
import hmac
import math
import sqlite3
import types
import typing
import asyncio
from pathlib import Path
from uuid import uuid4

from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.data_manager import (
    atomic_update_user, atomic_update_settings, load_users, load_settings, save_settings,
    load_market, load_loans, atomic_update_loans, get_economy_stats, now_ts
)
from bot.utils import ranks
from bot.cogs.atria import atria_chat
from bot.services.ai_provider import ai_provider
from bot.services.ai_chat import ai_chat_service
from bot.services.ai_orchestrator import ai_orchestrator
from bot.utils.ai_store import ai_store
from bot.utils.dm_store import dm_store
from bot.services.dm_center import render_embed, render_message
from bot.services.dm_bulk import DMBulkManager
from bot.services.live_terminal import live_terminal, install_live_terminal

logger = logging.getLogger("Vixen.Web")
BOT_APP_KEY = web.AppKey("bot")
DM_MANAGER_KEY = web.AppKey("dm_manager")
DM_PREVIEW_TOKENS = {}
DM_BULK_PREVIEW_TOKENS = {}
DM_PREVIEW_TTL = 300
DM_BULK_MAX_RECIPIENTS = 5000
DM_STARTER_TEMPLATES = [
    ("Welcome", {"title": "Welcome, {username}", "description": "Welcome to {server}.", "color": "#2ecc71", "footer": "Member {member_count} · {timestamp}", "timestamp": True}, "Community", "ترحيب بالأعضاء الجدد."),
    ("Announcement", {"title": "{server} announcement", "description": "{reason}", "color": "#3498db", "timestamp": True}, "Community", "إعلان عام للسيرفر."),
    ("Event", {"title": "Event update · {server}", "description": "{reason}", "color": "#9b59b6", "timestamp": True}, "Events", "تحديثات الفعاليات."),
    ("Update", {"title": "Server update", "description": "{reason}", "color": "#1abc9c", "footer": "{server} · {timestamp}", "timestamp": True}, "Operations", "إشعار بتحديث أو تغيير."),
    ("Warning", {"title": "Moderation notice", "description": "A warning was issued in {server}. Reason: {reason}", "color": "#f5a623", "footer": "Contact the moderation team if you need help."}, "Moderation", "تنبيه إداري رسمي."),
    ("Timeout", {"title": "Timeout notice", "description": "A timeout was applied in {server}. Reason: {reason}", "color": "#e74c3c", "footer": "{timestamp}", "timestamp": True}, "Moderation", "إشعار تقييد مؤقت."),
    ("Moderation Notice", {"title": "Moderation notice", "description": "{reason}", "color": "#e67e22", "footer": "{server}"}, "Moderation", "رسالة متابعة من فريق الإشراف."),
    ("Appeal Info", {"title": "Appeal information", "description": "Please contact the moderation team if you want to discuss this action.", "color": "#f39c12"}, "Moderation", "معلومات التواصل بعد إجراء إداري."),
    ("Community Reminder", {"title": "Community reminder", "description": "A quick reminder for {server}: {reason}", "color": "#2ecc71"}, "Community", "تذكير لقواعد أو عادات المجتمع."),
    ("Feedback Request", {"title": "We want your feedback", "description": "Tell us what you think about {server}. {reason}", "color": "#8e44ad"}, "Community", "طلب ملاحظات من الأعضاء."),
    ("Maintenance", {"title": "Maintenance notice", "description": "{server} may be temporarily unavailable. {reason}", "color": "#607d8b", "timestamp": True}, "Operations", "إشعار صيانة أو توقف مؤقت."),
    ("Staff Notice", {"title": "Staff notice · {server}", "description": "{reason}", "color": "#34495e", "timestamp": True}, "Staff", "إشعار داخلي أو تنسيق إداري."),
]

DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard"

SESSION_TTL = 7 * 24 * 3600  # أسبوع
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW = 300  # 5 دقائق
MAX_SESSIONS = 5000
MAX_ATTEMPT_IPS = 10000


class DashboardSessions:
    """إدارة جلسات تسجيل الدخول (بالذاكرة)."""

    def __init__(self):
        self.sessions = {}
        self.attempts = {}  # ip → [timestamps]

    def _prune_attempts(self, ip):
        now = time.time()
        attempts = [t for t in self.attempts.get(ip, []) if now - t < ATTEMPT_WINDOW]
        if attempts:
            self.attempts[ip] = attempts
        else:
            self.attempts.pop(ip, None)

    def too_many_attempts(self, ip) -> bool:
        self._prune_attempts(ip)
        return len(self.attempts.get(ip, [])) >= MAX_ATTEMPTS

    def record_attempt(self, ip):
        if ip not in self.attempts and len(self.attempts) >= MAX_ATTEMPT_IPS:
            self.attempts.pop(next(iter(self.attempts)))
        self.attempts.setdefault(ip, []).append(time.time())

    def create(self, user_id, rank: str, name: str = "", virtual: bool = False, code: str = "") -> str:
        now = time.time()
        expired = [token for token, value in self.sessions.items() if value.get("expires", 0) <= now]
        for expired_token in expired:
            self.sessions.pop(expired_token, None)
        while len(self.sessions) >= MAX_SESSIONS:
            self.sessions.pop(next(iter(self.sessions)))
        token = secrets.token_hex(32)
        self.sessions[token] = {
            "user_id": str(user_id) if user_id else "0",
            "rank": rank,
            "name": name,
            "virtual": virtual,   # جلسة بكود غير مربوط بآيدي ديسكورد
            "code": code,
            "expires": now + SESSION_TTL,
        }
        return token

    def get(self, token: str):
        session = self.sessions.get(token)
        if not session:
            return None
        if time.time() > session["expires"]:
            del self.sessions[token]
            return None
        return session


SESSIONS = DashboardSessions()


def _json_response(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _error(message: str, status: int = 400) -> web.Response:
    return _json_response({"ok": False, "error": message}, status)


def get_session_from_request(request: web.Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return SESSIONS.get(auth[7:].strip())


def require_permission(request: web.Request, permission: str):
    """يرجع الجلسة إذا كانت صالحة وتملك الصلاحية، وإلا يرفع خطأ HTTP."""
    session = get_session_from_request(request)
    if not session:
        raise web.HTTPUnauthorized(reason="انتهت الجلسة — سجل دخول من جديد")
    if permission:
        if session.get("virtual"):
            if permission not in ranks.get_permissions_for_rank(session.get("rank", "")):
                raise web.HTTPForbidden(reason="ما عندك صلاحية لهذه العملية برتبتك الحالية")
        else:
            uid = int(session["user_id"]) if session["user_id"].isdigit() else 0
            if not ranks.has_permission(uid, permission):
                raise web.HTTPForbidden(reason="ما عندك صلاحية لهذه العملية برتبتك الحالية")
    return session


def _session_has(session, permission: str) -> bool:
    if session.get("virtual"):
        return permission in ranks.get_permissions_for_rank(session.get("rank", ""))
    uid = int(session["user_id"]) if session["user_id"].isdigit() else 0
    return ranks.has_permission(uid, permission)


def resolve_name(bot, user_id) -> str:
    """يحاول يجيب اسم العضو من كاش البوت."""
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return "؟"
    if bot and uid:
        getter = getattr(bot, "get_user", None)
        if getter:
            user = getter(uid)
            if user:
                return user.name
        for guild in getattr(bot, "guilds", []):
            member_getter = getattr(guild, "get_member", None)
            member = member_getter(uid) if member_getter else None
            if member:
                return member.display_name
    s = str(user_id)
    return f"عضو {s[-4:]}" if len(s) >= 4 else "عضو"


def _refresh_overdue(loans_data) -> None:
    """Refresh a snapshot for callers and persist only the rows still active at commit time.

    Dashboard reads can race with Discord loan operations. A stale snapshot must
    never be written back over a newer loan state, so overdue transitions use
    the same atomic read-modify-write boundary as the loan commands.
    """
    now = now_ts()
    for loan in loans_data.get("loans", []):
        if isinstance(loan, dict) and loan.get("status") == "active" and loan.get("due", 0) < now:
            loan["status"] = "overdue"

    def mark_overdue(current):
        changed = False
        for loan in current.get("loans", []):
            if isinstance(loan, dict) and loan.get("status") == "active" and loan.get("due", 0) < now:
                loan["status"] = "overdue"
                changed = True
        return changed

    try:
        atomic_update_loans(mark_overdue)
    except (OSError, TypeError, ValueError, OverflowError):
        logger.exception("Could not persist overdue loan transitions")


# ---------------------------------------------------------------------------
# الصفحات الثابتة
# ---------------------------------------------------------------------------

async def serve_index(request: web.Request) -> web.Response:
    index_path = DASHBOARD_DIR / "index.html"
    if not index_path.exists():
        return web.Response(text="<h1>ملف الداشبورد غير موجود</h1>", content_type="text/html")
    return web.FileResponse(index_path)


# ---------------------------------------------------------------------------
# API: تسجيل الدخول
# ---------------------------------------------------------------------------

async def api_login(request: web.Request) -> web.Response:
    ip = request.remote or "؟"
    if SESSIONS.too_many_attempts(ip):
        return _error("محاولات كثيرة خاطئة — انتظر 5 دقائق وحاول مجددًا", 429)

    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")

    method = body.get("method", "code")
    settings = load_settings()

    if method == "code":
        code = (body.get("code") or "").strip().upper()
        entry = ranks.list_codes().get(code)
        if not entry:
            SESSIONS.record_attempt(ip)
            return _error("الكود غير صحيح — تأكد من كتابته", 401)
        if entry.get("revoked"):
            SESSIONS.record_attempt(ip)
            return _error("هذا الكود تم سحبه", 403)

        rank = entry.get("rank")
        if rank not in ranks.RANK_LEVELS:
            return _error("بيانات كود غير صالحة", 403)
        used_by = entry.get("used_by")
        if used_by:
            return _error("هذا الكود تم استخدامه مسبقًا ولا يمكن تسجيل الدخول به مرة أخرى", 409)

        # اربط الكود بهوية Dashboard مؤقتة قبل إنشاء الجلسة. الكود نفسه one-time.
        principal_id = -(secrets.randbelow((1 << 62) - 1) + 1)
        activation = ranks.activate_code(code, principal_id)
        if not activation.get("ok"):
            return _error(activation.get("message", "تعذر تفعيل الكود."), 409)
        token = SESSIONS.create(0, rank, name=f"كود …{code[-4:]}", virtual=True, code=code)
        perms = ranks.get_permissions_for_rank(rank)
        limit = ranks.get_daily_limit_for_rank(rank)

        return _json_response({
            "ok": True,
            "token": token,
            "rank": rank,
            "rank_name": ranks.RANK_NAMES_AR.get(rank, rank),
            "permissions": perms,
            "daily_limit": limit,
            "owner": False,
        })

    elif method == "owner":
        owner_id = str(body.get("owner_id") or "").strip()
        password = str(body.get("password") or "")
        dash_cfg = settings.get("dashboard", {})
        real_owner = ranks.get_owner_id()

        if not real_owner:
            return _error("ما تم ضبط OWNER_ID بملف .env — راجع ملف .env.example", 400)
        configured_password = str(os.getenv("DASHBOARD_PASSWORD") or dash_cfg.get("password") or "")
        if owner_id != str(real_owner):
            SESSIONS.record_attempt(ip)
            return _error("الآيدي ما يطابق آيدي المالك المسجل", 401)
        if not configured_password:
            return _error("دخول المالك غير مفعّل: اضبط DASHBOARD_PASSWORD", 503)
        if not hmac.compare_digest(password, configured_password):
            SESSIONS.record_attempt(ip)
            return _error("كلمة المرور غير صحيحة", 401)

        token = SESSIONS.create(real_owner, "owner", name="المالك")
        return _json_response({
            "ok": True,
            "token": token,
            "rank": "owner",
            "rank_name": ranks.RANK_NAMES_AR["owner"],
            "permissions": ranks.ALL_PERMISSIONS,
            "daily_limit": 0,
            "owner": True,
        })

    return _error("طريقة دخول غير معروفة")


async def api_me(request: web.Request) -> web.Response:
    session = get_session_from_request(request)
    if not session:
        return _error("جلسة غير صالحة", 401)

    rank = session["rank"]
    if session.get("virtual"):
        perms = ranks.get_permissions_for_rank(rank)
        limit = ranks.get_daily_limit_for_rank(rank)
        used_today = 0
    else:
        uid = int(session["user_id"]) if session["user_id"].isdigit() else 0
        perms = ranks.get_permissions(uid) if uid else []
        limit = ranks.get_daily_limit(uid) if uid else 0
        used_today = ranks.get_today_usage(uid) if uid else 0

    return _json_response({
        "ok": True,
        "rank": rank,
        "rank_name": ranks.RANK_NAMES_AR.get(rank, rank),
        "permissions": perms,
        "daily_limit": limit,
        "used_today": used_today,
        "owner": rank == "owner",
        "name": session.get("name", ""),
    })


# ---------------------------------------------------------------------------
# API: الإحصائيات المباشرة
# ---------------------------------------------------------------------------

async def api_stats(request: web.Request) -> web.Response:
    require_permission(request, "view_stats")
    settings = load_settings()
    stats = get_economy_stats()

    loans_data = load_loans()
    _refresh_overdue(loans_data)
    active_loans = [l for l in loans_data["loans"] if l["status"] in ("active", "overdue")]
    loans_total = sum(l["total"] - l["paid"] for l in active_loans)

    market = load_market()
    stocks_listed = sum(e.get("price", 0) for e in market.get("prices", {}).values())
    invested = 0
    for u in load_users().values():
        for sym, holding in (u.get("stocks") or {}).items():
            price = market.get("prices", {}).get(sym, {}).get("price", holding.get("avg_cost", 0))
            invested += price * holding.get("qty", 0)

    bot = request.app.get(BOT_APP_KEY)
    bot_info = {}
    if bot:
        try:
            latency = getattr(bot, "latency", float("nan"))
            latency_ms = round(latency * 1000) if math.isfinite(latency) else None
            bot_info = {
                "name": str(bot.user) if bot.user else "…",
                "latency_ms": latency_ms,
                "guilds": len(bot.guilds),
                "connected": bool(bot.user and bot.is_ready()),
            }
        except Exception:
            bot_info = {}

    return _json_response({
        "ok": True,
        "ts": now_ts(),
        "users": stats["total_users"],
        "total_wallet": stats["total_wallet"],
        "total_bank": stats["total_bank"],
        "total_money": stats["total_money"],
        "total_earned": stats["total_earned"],
        "active_loans": len(active_loans),
        "loans_total": loans_total,
        "stocks_listed_value": round(stocks_listed, 2),
        "invested_value": int(invested),
        "staff_count": len(ranks.list_staff()),
        "codes_available": sum(1 for c in ranks.list_codes().values()
                               if not c.get("revoked") and not c.get("used_by")),
        "bot": bot_info,
        "currency_symbol": settings.get("bot", {}).get("currency_symbol", "🪙"),
    })


async def api_bot_profile(request: web.Request) -> web.Response:
    require_permission(request, "view_stats")
    bot = request.app.get(BOT_APP_KEY)
    settings = load_settings()
    bot_settings = settings.get("bot", {})
    presence = bot_settings.get("presence", {}) if isinstance(bot_settings, dict) else {}
    guilds = list(getattr(bot, "guilds", []))
    user = getattr(bot, "user", None) if bot else None
    latency = getattr(bot, "latency", float("nan")) if bot else float("nan")
    latency_ms = round(latency * 1000) if math.isfinite(latency) else None
    started = getattr(bot, "_vixen_started_at", None) if bot else None
    uptime_seconds = max(0, int(time.monotonic() - started)) if started is not None else None
    return _json_response({
        "ok": True,
        "connected": bool(user and bot.is_ready()),
        "name": user.name if user else None,
        "display_name": user.global_name if user else None,
        "id": str(user.id) if user else None,
        "avatar": user.display_avatar.url if user else None,
        "latency_ms": latency_ms,
        "uptime_seconds": uptime_seconds,
        "guilds": len(guilds),
        "members": sum(int(guild.member_count or len(guild.members)) for guild in guilds),
        "commands": len(_all_commands(bot)),
        "version": os.getenv("VIXEN_VERSION", "development"),
        "ai": ai_provider.status(),
        "presence": presence if isinstance(presence, dict) else {"type": "playing", "name": ""},
    })


async def api_bot_presence(request: web.Request) -> web.Response:
    session = require_permission(request, "manage_settings")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    activity_type = body.get("type")
    name = body.get("name")
    if activity_type not in {"playing", "listening", "watching"}:
        return _error("نوع Presence غير صالح")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
        return _error("اكتب Presence لا يتجاوز 128 حرفًا")
    presence = {"type": activity_type, "name": name.strip()}
    def update_presence(settings):
        bot_settings = settings.setdefault("bot", {})
        if not isinstance(bot_settings, dict):
            bot_settings = {}
            settings["bot"] = bot_settings
        bot_settings["presence"] = presence
        return True
    atomic_update_settings(update_presence)
    applied = False
    bot = request.app.get(BOT_APP_KEY)
    if bot and bot.is_ready():
        activity_types = {
            "playing": discord.ActivityType.playing,
            "listening": discord.ActivityType.listening,
            "watching": discord.ActivityType.watching,
        }
        try:
            await bot.change_presence(activity=discord.Activity(type=activity_types[activity_type], name=presence["name"]))
            applied = True
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not apply bot Presence immediately")
    ranks.log_action(
        int(session.get("user_id") or 0),
        session.get("name", "Dashboard"),
        "bot_presence",
        f"type={activity_type} name={presence['name']} applied={applied}",
        "dashboard",
    )
    return _json_response({"ok": True, "presence": presence, "applied": applied})


# ---------------------------------------------------------------------------
# API: الأعضاء
# ---------------------------------------------------------------------------

async def api_users(request: web.Request) -> web.Response:
    require_permission(request, "view_users")
    bot = request.app.get(BOT_APP_KEY)
    query = (request.query.get("q") or "").strip().lower()

    users = load_users()
    market = load_market()

    results = []
    for uid, data in users.items():
        name = resolve_name(bot, uid)
        if query and query not in name.lower() and query not in uid:
            continue
        stocks_value = 0
        for sym, holding in (data.get("stocks") or {}).items():
            price = market.get("prices", {}).get(sym, {}).get("price", holding.get("avg_cost", 0))
            stocks_value += price * holding.get("qty", 0)
        results.append({
            "id": uid,
            "name": name,
            "wallet": data.get("wallet", 0),
            "bank": data.get("bank", 0),
            "total": data.get("wallet", 0) + data.get("bank", 0),
            "stocks_value": int(stocks_value),
            "level": data.get("level", 0),
            "job": data.get("job", ""),
            "rank": ranks.get_rank(int(uid)),
        })
        if len(results) >= 200:
            break

    results.sort(key=lambda u: u["total"], reverse=True)
    return _json_response({"ok": True, "users": results})


async def api_members(request: web.Request) -> web.Response:
    require_permission(request, "view_users")
    bot = request.app.get(BOT_APP_KEY)
    guild = bot.guilds[0] if bot and bot.guilds else None
    if not guild:
        return _json_response({"ok": True, "members": [], "next": None})
    q=(request.query.get("q") or "").strip().lower()
    try: limit=max(1,min(100, int(request.query.get("limit", "100"))))
    except ValueError: limit=100
    try: after=int(request.query.get("after", "0"))
    except ValueError: after=0
    members=sorted(guild.members,key=lambda m:m.id)
    out=[]
    for m in members:
        if m.id <= after or m.bot: continue
        hay=f"{m.display_name} {m.name} {m.id}".lower()
        if q and q not in hay: continue
        out.append({"id":str(m.id),"name":m.display_name,"username":m.name,"avatar":m.display_avatar.url,"bot":m.bot})
        if len(out)>=limit: break
    nxt=out[-1]["id"] if len(out)==limit else None
    return _json_response({"ok":True,"members":out,"next":nxt})


class _DashboardPermissions:
    administrator = False

class DashboardAuthor:
    def __init__(self, user_id: int, name: str, rank: str):
        self.id = int(user_id)
        self.name = name or f"Dashboard {rank}"
        self.display_name = self.name
        self.bot = False
        self.guild_permissions = _DashboardPermissions()
        self.mention = self.name
        self.rank = rank
    def __str__(self):
        return self.name

class DashboardContext:
    def __init__(self, bot, guild, author, channel, session=None):
        self.bot=bot; self.guild=guild; self.author=author; self.channel=channel; self.interaction=None; self.message=None
        self.dashboard_session=session
        self.sent=[]
    async def send(self, content=None, **kwargs):
        text=content
        if text is None and kwargs.get("embed") is not None:
            text=getattr(kwargs["embed"],"description",None) or getattr(kwargs["embed"],"title",None) or "تم التنفيذ."
        self.sent.append(str(text or "تم التنفيذ."))
        return text


def _command_parameter_meta(command, name, annotation):
    """Extract Discord application-command metadata, including choices."""
    param = getattr(command, "_params", {}).get(name) if hasattr(command, "_params") else None
    choices = []
    if param is not None:
        for choice in getattr(param, "choices", []) or []:
            choices.append({"name": choice.name, "value": choice.value})
    typ = getattr(param, "type", None) if param is not None else None
    return typ, choices

def _all_commands(bot):
    out=[]
    if bot is None:
        return out
    def walk(container, prefix=""):
        children = container.get_commands() if hasattr(container, "get_commands") else getattr(container, "commands", [])
        for c in children:
            path=f"{prefix}.{c.name}" if prefix else c.name
            nested=getattr(c,"commands",None)
            if nested:
                walk(c,path)
                continue
            try: sig=inspect.signature(c.callback)
            except Exception: continue
            args=[]
            for n,p in sig.parameters.items():
                if n in ("self","ctx") or p.kind in (p.VAR_POSITIONAL,p.VAR_KEYWORD): continue
                annotation=getattr(p,"annotation",str)
                if annotation is inspect._empty: annotation=str
                discord_type, choices = _command_parameter_meta(c,n,annotation)
                args.append({
                    "name":n,
                    "type":getattr(annotation,"__name__",str(annotation)),
                    "discord_type":getattr(discord_type,"name",str(discord_type or "")),
                    "required":p.default is inspect._empty,
                    "default":None if p.default is inspect._empty else p.default,
                    "choices":choices,
                })
            out.append({
                "name": path,
                "description": getattr(c, "description", "") or "",
                "args": args,
                "cog": getattr(getattr(c, "cog", None), "qualified_name", ""),
                "aliases": list(getattr(c, "aliases", []) or []),
            })
    for cog in bot.cogs.values(): walk(cog)
    return sorted(out,key=lambda x:x["name"])


def _find_command(bot, path: str):
    """Find a leaf command inside the same loaded Cog tree used by the bot.

    Cog instances expose ``get_commands`` but not ``get_command``.  The old
    dashboard executor called the latter on the Cog, which made every command
    appear in the catalog but fail with "الأمر غير موجود" when executed.
    """
    normalized = str(path or "").strip().lstrip("/")
    normalized = normalized.replace("/", ".").replace(" ", ".")
    parts = [part for part in normalized.split(".") if part]
    if not parts or bot is None:
        return None

    def find_child(container, name):
        children = (
            container.get_commands()
            if hasattr(container, "get_commands")
            else getattr(container, "commands", [])
        )
        wanted = name.lower()
        for child in children or []:
            names = [getattr(child, "name", ""), getattr(child, "qualified_name", "")]
            names.extend(getattr(child, "aliases", []) or [])
            if any(str(candidate).lower() == wanted for candidate in names):
                return child
        return None

    for cog in bot.cogs.values():
        current = find_child(cog, parts[0])
        if current is None:
            continue
        for part in parts[1:]:
            current = find_child(current, part)
            if current is None:
                break
        if current is not None and not getattr(current, "commands", None):
            return current
    return None


def _resolve_arg(bot, guild, value, typ, command_param=None):
    # app_commands.Choice must be reconstructed before invoking the callback.
    choices = list(getattr(command_param, "choices", []) or []) if command_param is not None else []
    if choices:
        wanted = str(value)
        for choice in choices:
            if str(choice.value) == wanted or str(choice.name) == wanted:
                return app_commands.Choice(name=choice.name, value=choice.value)
        raise ValueError(f"قيمة غير صالحة: {value}")

    # ``Optional[discord.Member]`` and the newer ``discord.Member | None``
    # both need to be reduced to their concrete Discord type before lookup.
    origin = typing.get_origin(typ)
    if origin in (typing.Union, types.UnionType):
        candidates = [item for item in typing.get_args(typ) if item is not type(None)]
        if len(candidates) == 1:
            typ = candidates[0]
    name=getattr(typ,"__name__","")
    text=str(value).strip()
    if name in ("Member","User") or (isinstance(typ, type) and issubclass(typ, (discord.Member, discord.User))):
        cleaned=text.strip("<@!>")
        if not cleaned.isdigit(): raise ValueError("Member/User ID غير صالح")
        uid=int(cleaned)
        user=guild.get_member(uid) or bot.get_user(uid)
        if user is None: raise ValueError("العضو غير موجود")
        return user
    if name=="Role" or (isinstance(typ, type) and issubclass(typ, discord.Role)):
        role=guild.get_role(int(text));
        if role is None: raise ValueError("الرتبة غير موجودة")
        return role
    if name in ("TextChannel","VoiceChannel","CategoryChannel") or (
        isinstance(typ, type) and issubclass(typ, discord.abc.GuildChannel)
    ):
        channel=guild.get_channel(int(text));
        if channel is None: raise ValueError("القناة غير موجودة")
        return channel
    if typ is bool or name=="bool": return text.lower() in ("1","true","yes","on","نعم","صح")
    if typ is int or name=="int": return int(text)
    if typ is float or name=="float": return float(text)
    return value


async def api_commands(request: web.Request) -> web.Response:
    require_permission(request, "view_stats")
    return _json_response({"ok":True,"commands":_all_commands(request.app.get(BOT_APP_KEY))})


async def api_command_execute(request: web.Request) -> web.Response:
    session=require_permission(request, "view_stats")
    try:
        body=await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    path=str(body.get("command") or "").strip().lstrip("/").replace("/", ".").replace(" ", ".")
    if not path: return _error("command مطلوب")
    bot=request.app.get(BOT_APP_KEY); guild=bot.guilds[0] if bot and bot.guilds else None
    if not guild: return _error("البوت غير متصل بسيرفر",503)
    uid=int(session.get("user_id") or 0)
    if session.get("virtual"):
        digest=int(hashlib.sha256(session.get("code", "").encode()).hexdigest()[:15], 16)
        principal_id=-(digest or 1)
        ranks.register_dashboard_principal(principal_id, session.get("rank", "team"))
        author=DashboardAuthor(principal_id, session.get("name", "Dashboard Staff"), session.get("rank", "team"))
    else:
        author=guild.get_member(uid)
        if not author: return _error("عضو الجلسة غير موجود في السيرفر",403)
    channel=guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
    ctx=DashboardContext(bot,guild,author,channel,session=session)
    cmd=None
    # Resolve the same loaded command object used by Discord.
    cmd = _find_command(bot, path)
    # Accept a stale client path or a Discord-style qualified name without
    # weakening the catalog: resolution still only walks loaded leaf commands.
    if cmd is None:
        compact = ".".join(part for part in path.split(".") if part)
        cmd = _find_command(bot, compact)
    if cmd is None: return _error("الأمر غير موجود",404)
    try:
        ctx.command = cmd
        if hasattr(cmd,"can_run") and not await cmd.can_run(ctx): return _error("ما عندك صلاحية لهذا الأمر",403)
        sig=inspect.signature(cmd.callback); raw=body.get("args") or {}; call=[]
        for n,p in sig.parameters.items():
            if n in ("self","ctx"): continue
            if p.default is inspect._empty and n not in raw: return _error(f"المعامل {n} مطلوب")
            if n in raw:
                param_meta = getattr(cmd, "_params", {}).get(n) if hasattr(cmd, "_params") else None
                call.append(_resolve_arg(bot,guild,raw[n],getattr(p,"annotation",str),param_meta))
            elif p.default is not inspect._empty: call.append(p.default)
        if any(v is None for v in call): return _error("تعذر العثور على أحد المعاملات",404)
        # مهلة زمنية قصوى 25 ثانية لأي أمر: بعض الأوامر (مثل lockdown) تتعامل
        # مع عدد كبير من القنوات/الأعضاء وقد تتأخر بسبب Discord rate limits.
        # بدون هذه المهلة، الطلب يبقى معلّقًا في المتصفح بلا أي رد.
        try:
            await asyncio.wait_for(cmd.callback(getattr(cmd,"cog",None), ctx, *call), timeout=25)
        except asyncio.TimeoutError:
            logger.warning(
                "Dashboard command timed out: /%s by %s",
                path,
                getattr(author, "name", session.get("name", "unknown")),
            )
            return _error("تجاوز الأمر المهلة الزمنية (25 ثانية) — قد يكون لا يزال يعمل بالخلفية، تحقق من سجل التدقيق بعد قليل", 504)
        message = "\n".join(ctx.sent[-3:]) or "تم التنفيذ."
        actor_name = getattr(author, "name", session.get("name", "Dashboard"))
        try:
            ranks.log_action(
                getattr(author, "id", 0),
                actor_name,
                "dashboard_command",
                f"تنفيذ /{path}",
                "dashboard",
            )
        except Exception:
            # لا نحول عملية Discord الناجحة إلى فشل HTTP بسبب تعطل سجل التدقيق.
            logger.exception("Could not persist dashboard audit log for /%s", path)
        logger.info("Dashboard command executed: /%s by %s", path, actor_name)
        return _json_response({"ok":True,"message":message})
    except (discord.Forbidden,discord.HTTPException):
        logger.warning(
            "Dashboard command rejected by Discord: /%s by %s",
            path,
            getattr(author, "name", session.get("name", "unknown")),
        )
        return _error("Discord رفض تنفيذ العملية.",403)
    except commands.CheckFailure:
        logger.warning(
            "Dashboard command denied: /%s by %s",
            path,
            getattr(author, "name", session.get("name", "unknown")),
        )
        return _error("ما عندك صلاحية لهذا الأمر.", 403)
    except (TypeError, ValueError):
        return _error("المعاملات غير صالحة.", 400)
    except Exception:
        logger.exception("Dashboard command execution failed: /%s", path)
        return _error("تعذر تنفيذ الأمر. راجع سجل البوت للتفاصيل.", 500)


async def api_atria(request: web.Request) -> web.Response:
    require_permission(request, "view_stats")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    prompt = body.get("prompt")
    mode = body.get("mode", "chat")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        return _error("اكتب نصاً صالحاً لا يتجاوز 4000 حرف.")
    if mode != "moderation":
        return _error("استخدم مسار AI Chat المستقل للمحادثة.")
    system = "You are a Discord safety moderator. Return JSON only with classification and short reason. Do not request or execute Discord actions in this manual analysis."
    try:
        answer = await atria_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            1400,
        )
        return _json_response({"ok": True, "answer": answer})
    except (RuntimeError, ValueError):
        logger.exception("Dashboard Atria request failed")
        return _error("تعذر الوصول إلى خدمة Atria الآن.", 502)


def _sync_ai_runtime(request: web.Request) -> bool:
    """Push persisted AI policy changes into the already-loaded Discord Cog immediately."""
    bot = request.app.get(BOT_APP_KEY)
    cog = getattr(bot, "cogs", {}).get("Atria") if bot else None
    refresh = getattr(cog, "refresh_ai_config", None)
    if not callable(refresh):
        logger.warning("AI runtime sync skipped: Atria Cog is not loaded")
        return False
    try:
        refresh()
        return True
    except Exception:
        logger.exception("AI runtime sync failed after Dashboard settings save")
        return False


async def api_ai_status(request: web.Request) -> web.Response:
    require_permission(request, "view_stats")
    settings = load_settings()
    ai_settings = settings.get("ai", {})
    moderation = ai_settings.get("moderation", {}) if isinstance(ai_settings, dict) else {}
    if not isinstance(moderation, dict):
        moderation = {}
    chat = ai_settings.get("chat", {}) if isinstance(ai_settings, dict) else {}
    if not isinstance(chat, dict):
        chat = {}
    legacy_moderation = settings.get("atria", {})
    if not isinstance(legacy_moderation, dict):
        legacy_moderation = {}
    bot = request.app.get(BOT_APP_KEY)
    bot_intents = getattr(bot, "intents", None)
    return _json_response({
        "ok": True,
        "provider": ai_provider.status(),
        "moderation_enabled": bool(moderation.get("enabled", legacy_moderation.get("moderation_enabled", False))),
        "moderation_killed": bool(moderation.get("kill_switch", False)),
        "chat_enabled": bool(chat.get("enabled", False)),
        "runtime": {
            "atria_cog_loaded": bool(bot and "Atria" in getattr(bot, "cogs", {})),
            "message_content_intent_requested": bool(getattr(bot_intents, "message_content", False)),
            "message_content_portal_hint": (
                "Enable Message Content Intent in Discord Developer Portal"
                if not bool(getattr(bot_intents, "message_content", False)) else None
            ),
            "chat_allowed_channels": len(chat.get("allowed_channels", [])) if isinstance(chat, dict) else 0,
            "moderation_channels": len(moderation.get("channels", [])) if isinstance(moderation, dict) else 0,
            "moderation_actions": list(moderation.get("automatic_actions", [])) if isinstance(moderation, dict) else [],
            "chat_metrics": ai_chat_service.metrics(),
            "chat_controls": {
                "smart_context": chat.get("smart_context_enabled", True) is not False,
                "intent_router": chat.get("intent_router_enabled", True) is not False,
                "auto_continuation": chat.get("auto_continuation_enabled", True) is not False,
                "micro_social_fast_lane": chat.get("micro_social_fast_lane", True) is not False,
                "self_repair": chat.get("self_repair_enabled", True) is not False,
                "humor_level": chat.get("humor_level", 5),
                "emoji_level": chat.get("emoji_level", 3),
                "answer_density": chat.get("answer_density", "smart"),
                "initiative": chat.get("initiative", "balanced"),
            },
            "live_policy": getattr(getattr(bot, "cogs", {}).get("Atria"), "runtime_status", lambda: {})(),
        },
    })


async def api_ai_test(request: web.Request) -> web.Response:
    require_permission(request, "manage_settings")
    status = await ai_provider.test_connection()
    return _json_response({"ok": True, "provider": status})


async def api_ai_chat_settings(request: web.Request) -> web.Response:
    session = require_permission(request, "manage_settings")
    settings = load_settings()
    ai_settings = settings.get("ai", {})
    config = ai_settings.get("chat", {}) if isinstance(ai_settings, dict) else {}
    if request.method == "GET":
        bot = request.app.get(BOT_APP_KEY)
        guilds = []
        for guild in getattr(bot, "guilds", []):
            guilds.append({
                "id": str(guild.id),
                "name": guild.name,
                "channels": [
                    {"id": str(channel.id), "name": channel.name}
                    for channel in guild.text_channels
                ],
            })
        return _json_response({
            "ok": True,
            "config": config if isinstance(config, dict) else {},
            "guilds": guilds,
        })

    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    channel_ids = body.get("allowed_channels", [])
    ignored_ids = body.get("ignored_channels", [])
    if not isinstance(channel_ids, list) or not isinstance(ignored_ids, list):
        return _error("قائمة القنوات غير صالحة")
    if len(channel_ids) > 100 or len(ignored_ids) > 100:
        return _error("عدد القنوات يتجاوز الحد")
    bot = request.app.get(BOT_APP_KEY)
    valid_channels = {
        str(channel.id)
        for guild in getattr(bot, "guilds", [])
        for channel in guild.text_channels
    }
    normalized_channels = {str(value) for value in channel_ids if str(value).isdigit()}
    normalized_ignored = {str(value) for value in ignored_ids if str(value).isdigit()}
    if len(normalized_channels) != len(channel_ids) or len(normalized_ignored) != len(ignored_ids):
        return _error("أحد معرّفات القنوات غير صالح")
    if not normalized_channels.issubset(valid_channels) or not normalized_ignored.issubset(valid_channels):
        return _error("القناة المحددة غير موجودة ضمن سيرفرات البوت")
    mode = body.get("response_mode", "mention_only")
    if mode not in {"mention_only", "every_message", "commands_only"}:
        return _error("وضع الرد غير صالح")
    speed_mode = body.get("speed_mode", "flash")
    if speed_mode not in {"flash", "balanced", "deep"}:
        return _error("وضع السرعة غير صالح")
    language = body.get("language", "auto")
    if language not in {"auto", "tunisian", "arabic", "french", "english"}:
        return _error("اللغة غير صالحة")
    try:
        context_messages = max(12, min(100, int(body.get("context_messages", 100))))
        retention_days = max(1, min(3650, int(body.get("retention_days", 30))))
        max_output_tokens = max(1, min(65536, int(body.get("max_output_tokens", 65536))))
        streaming = body.get("streaming", True)
        smart_context_enabled = body.get("smart_context_enabled", True)
        intent_router_enabled = body.get("intent_router_enabled", True)
        flash_path_enabled = body.get("flash_path_enabled", True)
        quality_guard_enabled = body.get("quality_guard_enabled", True)
        owner_actions_enabled = body.get("owner_actions_enabled", True)
        self_repair_enabled = body.get("self_repair_enabled", True)
        auto_continuation_enabled = body.get("auto_continuation_enabled", True)
        micro_social_fast_lane = body.get("micro_social_fast_lane", True)
        # Backward-compatible fields; no Vixen-side AI request limiter is enforced.
        cooldowns = {
            "user_cooldown_seconds": 0,
            "channel_cooldown_seconds": 0,
            "global_cooldown_seconds": 0,
        }
    except (TypeError, ValueError, OverflowError):
        return _error("إعدادات السياق أو طول الرد غير صالحة")
    if not all(isinstance(value, bool) for value in (streaming, smart_context_enabled, intent_router_enabled, flash_path_enabled, quality_guard_enabled, owner_actions_enabled, self_repair_enabled, auto_continuation_enabled, micro_social_fast_lane)):
        return _error("إعدادات Streaming أو Smart Context أو Intent Router أو Self-Repair أو Micro Social غير صالحة")
    humor_level = body.get("humor_level", 5)
    emoji_level = body.get("emoji_level", 3)
    answer_density = body.get("answer_density", "smart")
    initiative = body.get("initiative", "balanced")
    try:
        humor_level = max(0, min(10, int(humor_level)))
        emoji_level = max(0, min(10, int(emoji_level)))
        context_char_budget = max(12_000, min(125_000, int(body.get("context_char_budget", 50_000))))
    except (TypeError, ValueError, OverflowError):
        return _error("إعدادات أسلوب الرد والسياق غير صالحة")
    if answer_density not in {"brief", "smart", "detailed"} or initiative not in {"low", "balanced", "proactive"}:
        return _error("إعدادات كثافة الرد أو المبادرة غير صالحة")
    enabled = body.get("enabled", False)
    tunisian_mode = body.get("tunisian_mode", False)
    if not isinstance(enabled, bool) or not isinstance(tunisian_mode, bool):
        return _error("حالة التفعيل غير صالحة")
    if enabled and not normalized_channels:
        return _error("اختر قناة مسموحة واحدة على الأقل قبل التفعيل")
    config = {
        "enabled": enabled,
        "allowed_channels": sorted(normalized_channels),
        "ignored_channels": sorted(normalized_ignored),
        "response_mode": mode,
        "speed_mode": speed_mode,
        "language": language,
        "tunisian_mode": tunisian_mode,
        "context_messages": context_messages,
        "retention_days": retention_days,
        "streaming": streaming,
        "max_output_tokens": max_output_tokens,
        "context_char_budget": context_char_budget,
        "smart_context_enabled": smart_context_enabled,
        "intent_router_enabled": intent_router_enabled,
        "flash_path_enabled": flash_path_enabled,
        "quality_guard_enabled": quality_guard_enabled,
        "self_repair_enabled": self_repair_enabled,
        "auto_continuation_enabled": auto_continuation_enabled,
        "micro_social_fast_lane": micro_social_fast_lane,
        "owner_actions_enabled": owner_actions_enabled,
        "humor_level": humor_level,
        "emoji_level": emoji_level,
        "answer_density": answer_density,
        "initiative": initiative,
        "name": str(body.get("name", "Vixen")).strip()[:80] or "Vixen",
        "tone": str(body.get("tone", "friendly")).strip()[:80] or "friendly",
        "style": str(body.get("style", "conversational")).strip()[:120] or "conversational",
        "system_prompt": str(body.get("system_prompt", "")).strip()[:2000],
        **cooldowns,
    }
    def update_chat_settings(settings):
        ai_settings = settings.get("ai", {})
        if not isinstance(ai_settings, dict):
            ai_settings = {}
        ai_settings["chat"] = config
        settings["ai"] = ai_settings
        return True
    atomic_update_settings(update_chat_settings)
    runtime_synced = _sync_ai_runtime(request)
    ranks.log_action(
        int(session.get("user_id") or 0),
        session.get("name", "Dashboard"),
        "ai_chat_settings",
        f"enabled={enabled} channels={len(normalized_channels)} mode={mode} speed={speed_mode} smart={smart_context_enabled} router={intent_router_enabled} flash={flash_path_enabled}",
        "dashboard",
    )
    return _json_response({"ok": True, "config": config, "runtime_synced": runtime_synced})


async def api_ai_moderation_settings(request: web.Request) -> web.Response:
    session = require_permission(request, "manage_settings")
    settings = load_settings()
    ai_settings = settings.get("ai", {})
    config = ai_settings.get("moderation", {}) if isinstance(ai_settings, dict) else {}
    legacy = settings.get("atria", {})
    if request.method == "GET":
        bot = request.app.get(BOT_APP_KEY)
        guilds = [{
            "id": str(guild.id),
            "name": guild.name,
            "channels": [{"id": str(channel.id), "name": channel.name} for channel in guild.text_channels],
        } for guild in getattr(bot, "guilds", [])]
        return _json_response({
            "ok": True,
            "config": config if isinstance(config, dict) else {},
            "legacy": legacy if isinstance(legacy, dict) else {},
            "guilds": guilds,
        })

    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    channel_ids = body.get("channels", [])
    actions = body.get("automatic_actions", [])
    detections = body.get("detection_types", [])
    if not all(isinstance(value, list) for value in (channel_ids, actions, detections)):
        return _error("قوائم سياسة AI غير صالحة")
    if len(channel_ids) > 100:
        return _error("عدد القنوات يتجاوز الحد")
    valid_channels = {
        str(channel.id)
        for guild in getattr(request.app.get(BOT_APP_KEY), "guilds", [])
        for channel in guild.text_channels
    }
    normalized_channels = {str(value) for value in channel_ids if str(value).isdigit()}
    if len(normalized_channels) != len(channel_ids) or not normalized_channels.issubset(valid_channels):
        return _error("القناة المحددة غير موجودة ضمن سيرفرات البوت")
    if any(action not in {"warn", "timeout", "ban"} for action in actions):
        return _error("إجراء تلقائي غير مدعوم")
    allowed_detections = {
        "spam", "flood", "repeated_messages", "toxicity", "harassment", "suspicious", "raid_like",
        "character_spam", "invite_link", "suspicious_links", "mention_abuse", "long_content", "scam",
    }
    if any(detection not in allowed_detections for detection in detections):
        return _error("نوع اكتشاف غير مدعوم")
    sensitivity = body.get("sensitivity", "medium")
    if sensitivity not in {"low", "medium", "high"}:
        return _error("مستوى الحساسية غير صالح")
    enabled = body.get("enabled", False)
    allow_ai_ban = body.get("allow_ai_ban", False)
    if not isinstance(enabled, bool) or not isinstance(allow_ai_ban, bool):
        return _error("حالة التفعيل غير صالحة")
    if enabled and (not normalized_channels or not detections or not actions):
        return _error("اختر قناة ونوع اكتشاف وإجراء تلقائي واحدًا على الأقل قبل التفعيل")
    if allow_ai_ban and "ban" not in actions:
        return _error("أضف ban إلى الإجراءات التلقائية قبل تفعيل السماح به")
    try:
        max_actions = max(1, min(1000, int(body.get("max_actions_per_hour", 20))))
        timeout_minutes = max(1, min(60, int(body.get("timeout_minutes", 10))))
        ai_timeout_seconds = max(1.0, min(8.0, float(body.get("ai_timeout_seconds", 6.0))))
        window_seconds = max(2.0, min(30.0, float(body.get("window_seconds", 8))))
        repeat_threshold = max(2, min(10, int(body.get("repeat_threshold", 5))))
        flood_threshold = max(3, min(30, int(body.get("flood_threshold", 8))))
        mention_threshold = max(3, min(30, int(body.get("mention_threshold", 8))))
        raid_window_seconds = max(5.0, min(120.0, float(body.get("raid_window_seconds", 20))))
        raid_join_threshold = max(3, min(50, int(body.get("raid_join_threshold", 8))))
        escalation_timeout_short = max(1, min(60, int(body.get("escalation_timeout_short", 5))))
        escalation_timeout_long = max(escalation_timeout_short, min(120, int(body.get("escalation_timeout_long", 15))))
        mod_log_channel = str(body.get("mod_log_channel", body.get("mod_log_channel_id", ""))).strip()
        if mod_log_channel and (not mod_log_channel.isdigit() or int(mod_log_channel) not in valid_channels):
            return _error("قناة سجل Moderation غير صالحة")
    except (TypeError, ValueError, OverflowError):
        return _error("إعدادات السرعة والكشف غير صالحة")
    fast_path_enabled = body.get("fast_path_enabled", True)
    delete_offending_messages = body.get("delete_offending_messages", True)
    allow_server_invites = body.get("allow_server_invites", False)
    if not all(isinstance(value, bool) for value in (fast_path_enabled, delete_offending_messages, allow_server_invites)):
        return _error("إعدادات Fast Path أو الحذف أو الروابط غير صالحة")
    whitelist_users = body.get("whitelist_users", [])
    whitelist_roles = body.get("whitelist_roles", [])
    if not isinstance(whitelist_users, list) or not isinstance(whitelist_roles, list) or len(whitelist_users) > 200 or len(whitelist_roles) > 200:
        return _error("قائمة الاستثناءات غير صالحة")
    normalized_whitelist_users = sorted({str(value) for value in whitelist_users if str(value).isdigit()})
    normalized_whitelist_roles = sorted({str(value) for value in whitelist_roles if str(value).isdigit()})
    if len(normalized_whitelist_users) != len(whitelist_users) or len(normalized_whitelist_roles) != len(whitelist_roles):
        return _error("أحد معرّفات الاستثناءات غير صالح")

    config = {
        "enabled": enabled,
        "kill_switch": False,
        "channels": sorted(normalized_channels),
        "detection_types": list(dict.fromkeys(detections)),
        "sensitivity": sensitivity,
        "automatic_actions": list(dict.fromkeys(actions)),
        "allow_ai_ban": allow_ai_ban,
        "max_actions_per_hour": max_actions,
        "timeout_minutes": timeout_minutes,
        "fast_path_enabled": fast_path_enabled,
        "ai_timeout_seconds": ai_timeout_seconds,
        "window_seconds": window_seconds,
        "repeat_threshold": repeat_threshold,
        "flood_threshold": flood_threshold,
        "mention_threshold": mention_threshold,
        "mod_log_channel": mod_log_channel,
        "whitelist_users": normalized_whitelist_users,
        "whitelist_roles": normalized_whitelist_roles,
        "delete_offending_messages": delete_offending_messages,
        "allow_server_invites": allow_server_invites,
        "raid_window_seconds": raid_window_seconds,
        "raid_join_threshold": raid_join_threshold,
        "escalation_timeout_short": escalation_timeout_short,
        "escalation_timeout_long": escalation_timeout_long,
    }
    def update_moderation_settings(settings):
        ai_settings = settings.get("ai", {})
        if not isinstance(ai_settings, dict):
            ai_settings = {}
        ai_settings["moderation"] = config
        settings["ai"] = ai_settings
        legacy = settings.setdefault("atria", {})
        if isinstance(legacy, dict):
            legacy.update(moderation_enabled=enabled, timeout_minutes=timeout_minutes)
        return True
    atomic_update_settings(update_moderation_settings)
    runtime_synced = _sync_ai_runtime(request)
    ranks.log_action(
        int(session.get("user_id") or 0),
        session.get("name", "Dashboard"),
        "ai_moderation_settings",
        f"enabled={enabled} channels={len(normalized_channels)} actions={','.join(config['automatic_actions'])}",
        "dashboard",
    )
    return _json_response({"ok": True, "config": config, "runtime_synced": runtime_synced})


def _safe_ai_public_error(exc=None, fallback="⚠️ الـAI غير متاح توا، وتمّ احتواء الخطأ.") -> str:
    """Never send raw provider diagnostics, quotas, retry timers or request internals to the UI."""
    if exc is not None:
        safe = getattr(exc, "safe_message", None)
        if isinstance(safe, str) and safe.strip():
            return safe.strip()[:500]
    return fallback


async def api_ai_chat_test(request: web.Request) -> web.Response:
    require_permission(request, "manage_settings")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        return _error("اكتب رسالة صالحة لا تتجاوز 12000 حرف")
    settings = load_settings()
    ai_settings = settings.get("ai", {})
    config = ai_settings.get("chat", {}) if isinstance(ai_settings, dict) else {}
    if not isinstance(config, dict):
        config = {}
    try:
        answer = await ai_chat_service.test_prompt(prompt, config)
        return _json_response({"ok": True, "answer": answer[:4000]})
    except Exception as exc:
        logger.exception("Dashboard AI Chat test failed")
        return _error(_safe_ai_public_error(exc), 502)


async def api_ai_chat_stream(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events stream for the Dashboard AI Chat tester."""
    require_permission(request, "manage_settings")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        return _error("اكتب رسالة صالحة لا تتجاوز 12000 حرف")
    settings = load_settings()
    ai_settings = settings.get("ai", {})
    config = ai_settings.get("chat", {}) if isinstance(ai_settings, dict) else {}
    if not isinstance(config, dict):
        config = {}
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)
    request_id = uuid4().hex[:12]
    try:
        plan = ai_orchestrator.plan(prompt, config)
        start_event = {
            "type": "start",
            "request_id": request_id,
            "intent": plan.intent,
            "profile": plan.profile,
            "max_output_tokens": plan.max_output_tokens,
            "context_messages": plan.context_messages,
        }
        await response.write(("data: " + json.dumps(start_event, ensure_ascii=False) + "\n\n").encode("utf-8"))
        async for delta in ai_chat_service.test_stream(prompt, config):
            live_request_id = ai_provider.status().get("request_id")
            await response.write(("data: " + json.dumps({"type": "delta", "text": str(delta), "request_id": live_request_id or request_id}, ensure_ascii=False) + "\n\n").encode("utf-8"))
        status = ai_provider.status()
        done_event = {
            "type": "done",
            "request_id": request_id,
            "provider_request_id": status.get("request_id"),
            "latency_ms": status.get("total_latency_ms", status.get("latency_ms")),
            "ttft_ms": status.get("first_token_latency_ms"),
            "recovery": (status.get("recovery") or {}).get("type") if isinstance(status.get("recovery"), dict) else None,
        }
        await response.write(("data: " + json.dumps(done_event, ensure_ascii=False) + "\n\n").encode("utf-8"))
    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, BrokenPipeError):
        logger.info("Dashboard AI Chat client disconnected request=%s", request_id)
    except Exception as exc:
        logger.exception("Dashboard AI Chat stream failed request=%s", request_id)
        try:
            await response.write(("data: " + json.dumps({"type": "error", "request_id": request_id, "message": _safe_ai_public_error(exc)}, ensure_ascii=False) + "\n\n").encode("utf-8"))
        except (ConnectionResetError, BrokenPipeError, RuntimeError):
            pass
    finally:
        try:
            await response.write_eof()
        except (ConnectionResetError, BrokenPipeError, RuntimeError):
            pass
    return response


async def api_ai_chat_logs(request: web.Request) -> web.Response:
    require_permission(request, "manage_settings")
    channel_id = request.query.get("channel_id", "")
    if not channel_id.isdigit():
        return _error("اختر قناة AI Chat")
    settings = load_settings()
    ai_settings = settings.get("ai", {})
    config = ai_settings.get("chat", {}) if isinstance(ai_settings, dict) else {}
    allowed = config.get("allowed_channels", []) if isinstance(config, dict) else []
    if not isinstance(allowed, list) or channel_id not in {str(value) for value in allowed}:
        return _error("سجل المحادثة متاح للقنوات المسموحة فقط", 403)
    bot = request.app.get(BOT_APP_KEY)
    guild = next(
        (item for item in getattr(bot, "guilds", []) if item.get_channel(int(channel_id)) is not None),
        None,
    )
    if guild is None:
        return _error("القناة غير متاحة للبوت", 404)
    try:
        limit = max(1, min(500, int(request.query.get("limit", "100"))))
    except (TypeError, ValueError, OverflowError):
        return _error("قيمة limit غير صالحة")
    return _json_response({"ok": True, "messages": ai_store.recent_chat_entries(guild.id, int(channel_id), limit)})


async def api_ai_moderation_kill(request: web.Request) -> web.Response:
    session = require_permission(request, "manage_settings")
    def kill_moderation(settings):
        ai_settings = settings.setdefault("ai", {})
        if not isinstance(ai_settings, dict):
            ai_settings = {}
            settings["ai"] = ai_settings
        moderation = ai_settings.setdefault("moderation", {})
        if not isinstance(moderation, dict):
            moderation = {}
            ai_settings["moderation"] = moderation
        moderation.update(enabled=False, kill_switch=True)
        legacy_moderation = settings.setdefault("atria", {})
        if isinstance(legacy_moderation, dict):
            legacy_moderation["moderation_enabled"] = False
        return True
    atomic_update_settings(kill_moderation)
    runtime_synced = _sync_ai_runtime(request)
    ranks.log_action(
        int(session.get("user_id") or 0),
        session.get("name", "Dashboard"),
        "ai_moderation_kill",
        "Emergency AI moderation kill switch activated",
        "dashboard",
    )
    logger.warning("AI moderation emergency kill switch activated by %s", session.get("name", "Dashboard"))
    return _json_response({"ok": True, "enabled": False, "kill_switch": True, "runtime_synced": runtime_synced})


async def api_ai_moderation_logs(request: web.Request) -> web.Response:
    require_permission(request, "view_logs")
    bot = request.app.get(BOT_APP_KEY)
    guild_id = request.query.get("guild_id")
    guild = next(
        (item for item in getattr(bot, "guilds", []) if str(item.id) == str(guild_id)),
        None,
    ) if guild_id else (bot.guilds[0] if bot and bot.guilds else None)
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    try:
        limit = max(1, min(500, int(request.query.get("limit", "100"))))
    except (TypeError, ValueError, OverflowError):
        return _error("قيمة limit غير صالحة")
    return _json_response({
        "ok": True,
        "logs": ai_store.recent_moderation(guild.id, limit),
    })


async def api_moderation_warnings(request: web.Request) -> web.Response:
    require_permission(request, "view_logs")
    bot = request.app.get(BOT_APP_KEY)
    guild_id = request.query.get("guild_id")
    guild = next(
        (item for item in getattr(bot, "guilds", []) if str(item.id) == str(guild_id)),
        None,
    ) if guild_id else (bot.guilds[0] if bot and bot.guilds else None)
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    try:
        limit = max(1, min(500, int(request.query.get("limit", "100"))))
        member_id = request.query.get("member_id")
        moderator_id = request.query.get("moderator_id")
        if member_id is not None and not member_id.isdigit():
            return _error("Member ID غير صالح")
        if moderator_id is not None and not moderator_id.isdigit():
            return _error("Moderator ID غير صالح")
    except (TypeError, ValueError, OverflowError):
        return _error("معاملات البحث غير صالحة")
    warnings = ai_store.recent_guild_warnings(
        guild.id,
        limit,
        int(member_id) if member_id else None,
        int(moderator_id) if moderator_id else None,
    )
    for item in warnings:
        item["member_name"] = resolve_name(bot, item["user_id"])
        item["moderator_name"] = resolve_name(bot, item["moderator_id"])
    query = (request.query.get("q") or "").strip().lower()[:100]
    if query:
        warnings = [
            item for item in warnings
            if query in f"{item['user_id']} {item['member_name']} {item['moderator_id']} {item['moderator_name']} {item['reason']}".lower()
        ]
    return _json_response({"ok": True, "warnings": warnings})


async def api_voice_targets(request: web.Request) -> web.Response:
    require_permission(request, "move_members")
    bot = request.app.get(BOT_APP_KEY)
    guilds = []
    for guild in getattr(bot, "guilds", []):
        voice_client = guild.voice_client
        guilds.append({
            "id": str(guild.id),
            "name": guild.name,
            "connected_channel_id": str(voice_client.channel.id) if voice_client and voice_client.is_connected() else None,
            "channels": [
                {"id": str(channel.id), "name": channel.name}
                for channel in [*guild.voice_channels, *guild.stage_channels]
            ],
        })
    return _json_response({"ok": True, "guilds": guilds})


async def api_voice_action(request: web.Request) -> web.Response:
    session = require_permission(request, "move_members")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    action = body.get("action")
    guild_id = body.get("guild_id")
    channel_id = body.get("channel_id")
    if action not in {"join", "leave"} or not str(guild_id or "").isdigit():
        return _error("إجراء أو سيرفر غير صالح")
    bot = request.app.get(BOT_APP_KEY)
    guild = next((item for item in getattr(bot, "guilds", []) if str(item.id) == str(guild_id)), None)
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    try:
        voice_client = guild.voice_client
        if action == "leave":
            if voice_client is None or not voice_client.is_connected():
                return _error("البوت غير متصل بقناة صوتية", 409)
            channel_name = voice_client.channel.name
            await voice_client.disconnect(force=True)
            result_channel_id = None
        else:
            if not str(channel_id or "").isdigit():
                return _error("اختر قناة صوتية")
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                return _error("القناة الصوتية غير موجودة في السيرفر", 404)
            bot_member = guild.me
            if bot_member is None or not channel.permissions_for(bot_member).connect:
                return _error("البوت لا يملك صلاحية الاتصال بهذه القناة", 403)
            if voice_client and voice_client.is_connected():
                if voice_client.channel.id != channel.id:
                    await voice_client.move_to(channel)
            else:
                await channel.connect(timeout=10, reconnect=True)
            channel_name = channel.name
            result_channel_id = str(channel.id)
    except discord.Forbidden:
        return _error("Discord رفض اتصال الصوت بسبب الصلاحيات", 403)
    except discord.HTTPException:
        logger.exception("Voice action failed guild=%s action=%s", guild.id, action)
        return _error("تعذر تنفيذ عملية الصوت عبر Discord", 502)
    actor_id = int(session.get("user_id") or 0)
    try:
        ranks.log_action(actor_id, session.get("name", "Dashboard"), f"voice_{action}", f"guild={guild.id} channel={channel_name}", "dashboard")
    except Exception:
        logger.exception("Could not audit Dashboard voice action")
    logger.info("voice_action guild=%s channel=%s action=%s", guild.id, channel_name, action)
    return _json_response({"ok": True, "action": action, "channel_id": result_channel_id, "channel_name": channel_name})


def _dm_guild(bot, guild_id):
    if not str(guild_id or "").isdigit():
        return None
    return next((guild for guild in getattr(bot, "guilds", []) if str(guild.id) == str(guild_id)), None)


def _audit_dm(session, action, details):
    try:
        ranks.log_action(
            int(session.get("user_id") or 0),
            session.get("name", "Dashboard"),
            action,
            details,
            "dashboard",
        )
    except Exception:
        logger.exception("Could not persist DM audit event action=%s", action)


async def _dm_member(guild, member_id):
    if not str(member_id or "").isdigit():
        return None
    member = guild.get_member(int(member_id))
    if member is not None:
        return member
    try:
        return await guild.fetch_member(int(member_id))
    except discord.NotFound:
        return None


def _dm_variables(bot, guild, member, session, reason=""):
    settings = load_settings()
    bot_config = settings.get("bot", {})
    warning_count = ai_store.count_warnings(guild.id, member.id)
    roles = ", ".join(role.name for role in getattr(member, "roles", []) if not role.is_default())
    now = discord.utils.utcnow()
    return {
        "user": member.mention, "mention": member.mention, "user_id": member.id,
        "username": member.name, "display_name": member.display_name, "server": guild.name, "server_id": guild.id,
        "member_count": guild.member_count or 0, "moderator": session.get("name", "Staff"),
        "reason": str(reason)[:1000], "warnings": warning_count, "roles": roles,
        "is_bot": "yes" if getattr(member, "bot", False) else "no", "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M UTC"),
        "server_icon": guild.icon.url if guild.icon else "",
        "bot_avatar": bot.user.display_avatar.url if bot and bot.user else "",
        "bot": bot.user.name if bot and bot.user else "Vixen",
        "primary_color": bot_config.get("embed_color", "#F5A623"),
        "accent_color": bot_config.get("success_color", "#2ECC71"),
    }


def _template_design(template):
    return {"content": template.get("content", ""), "embed": template.get("embed", {})}


def _dm_staff_ids():
    staff_ids = set()
    try:
        staff_ids.update(int(uid) for uid in ranks.list_staff())
    except (TypeError, ValueError, OverflowError):
        pass
    owner_id = ranks.get_owner_id()
    if owner_id:
        staff_ids.add(int(owner_id))
    return staff_ids


async def _ensure_dm_member_cache(guild):
    members = list(getattr(guild, "members", []) or [])
    expected = int(getattr(guild, "member_count", 0) or 0)
    complete = expected == 0 or len(members) >= expected
    if not complete:
        chunk = getattr(guild, "chunk", None)
        if chunk:
            try:
                await chunk(cache=True)
                members = list(getattr(guild, "members", []) or [])
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Could not chunk guild members guild=%s", guild.id)
        complete = expected == 0 or len(members) >= expected
    return members, complete


def _target_member_ids(members, filters, session, guild):
    filters = filters if isinstance(filters, dict) else {}
    mode = str(filters.get("mode", "all")).lower()
    include_roles = {str(value) for value in filters.get("role_ids", []) if str(value).isdigit()}
    exclude_roles = {str(value) for value in filters.get("exclude_role_ids", []) if str(value).isdigit()}
    selected_ids = {str(value) for value in filters.get("member_ids", []) if str(value).isdigit()}
    exclude_bots = bool(filters.get("exclude_bots", True))
    exclude_staff = bool(filters.get("exclude_staff", False))
    exclude_self = bool(filters.get("exclude_self", True))
    staff_ids = _dm_staff_ids()
    sender_id = int(session.get("user_id") or 0)
    result, seen = [], set()
    for member in members:
        member_id = int(member.id)
        sid = str(member_id)
        if sid in seen or (mode == "selected" and sid not in selected_ids):
            continue
        if mode not in {"all", "roles", "selected"}:
            continue
        member_role_ids = {str(role.id) for role in getattr(member, "roles", [])}
        if mode == "roles" and include_roles and not (member_role_ids & include_roles):
            continue
        if include_roles and mode != "selected" and not (member_role_ids & include_roles):
            continue
        if exclude_roles and member_role_ids & exclude_roles:
            continue
        if exclude_bots and getattr(member, "bot", False):
            continue
        if exclude_staff and member_id in staff_ids:
            continue
        if exclude_self and member_id == sender_id:
            continue
        if member_id == getattr(getattr(guild, "me", None), "id", None):
            continue
        seen.add(sid)
        result.append(member_id)
    return result


async def _resolve_dm_message(request_body, bot, guild, member, session):
    template_id = request_body.get("template_id")
    if template_id is not None and not str(template_id).isdigit():
        raise ValueError("معرّف القالب غير صالح")
    template = dm_store.get_template(guild.id, template_id) if template_id else None
    if template_id and template is None:
        raise LookupError("القالب غير موجود")
    if template and "embed" not in request_body and "content" not in request_body:
        design = _template_design(template)
    else:
        design = {
            "content": request_body.get("content", template.get("content", "") if template else ""),
            "embed": request_body.get("embed", template.get("embed", {}) if template else {}),
        }
    rendered = render_message(design, _dm_variables(bot, guild, member, session, request_body.get("reason", "")))
    return template, rendered, design


async def api_dm_center(request: web.Request) -> web.Response:
    require_permission(request, "dm_members")
    bot = request.app.get(BOT_APP_KEY)
    bot_config = load_settings().get("bot", {})
    bot_avatar = bot.user.display_avatar.url if bot and bot.user else ""
    guilds = [{
        "id": str(guild.id), "name": guild.name, "icon": guild.icon.url if guild.icon else "",
        "member_count": guild.member_count or 0,
        "cached_member_count": len(getattr(guild, "members", []) or []),
        "member_cache_complete": not guild.member_count or len(getattr(guild, "members", []) or []) >= guild.member_count,
        "primary_color": bot_config.get("embed_color", "#F5A623"),
        "accent_color": bot_config.get("success_color", "#2ECC71"),
        "bot_name": str(bot.user.name) if bot and bot.user else "Vixen", "bot_avatar": bot_avatar,
    } for guild in getattr(bot, "guilds", [])]
    return _json_response({"ok": True, "guilds": guilds, "bulk": {"max_recipients": DM_BULK_MAX_RECIPIENTS, "max_messages_per_minute": 60}})


async def api_dm_roles(request: web.Request) -> web.Response:
    require_permission(request, "dm_members")
    bot = request.app.get(BOT_APP_KEY)
    guild = _dm_guild(bot, request.query.get("guild_id"))
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    roles = []
    for role in sorted(getattr(guild, "roles", []) or [], key=lambda item: (item.position, item.name), reverse=True):
        if role.is_default():
            continue
        roles.append({"id": str(role.id), "name": role.name, "position": role.position, "member_count": len(getattr(role, "members", []) or [])})
    return _json_response({"ok": True, "roles": roles})


async def api_dm_members(request: web.Request) -> web.Response:
    require_permission(request, "dm_members")
    guild = _dm_guild(request.app.get(BOT_APP_KEY), request.query.get("guild_id"))
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    query = (request.query.get("q") or "").strip().lower()[:100]
    members = []
    source = getattr(guild, "members", []) or []
    for member in source:
        haystack = f"{member.id} {member.name} {member.display_name}".lower()
        if query and query not in haystack:
            continue
        members.append({
            "id": str(member.id), "username": member.name, "display_name": member.display_name,
            "avatar": member.display_avatar.url, "roles": [role.name for role in member.roles if not role.is_default()],
            "role_ids": [str(role.id) for role in member.roles if not role.is_default()], "is_bot": member.bot,
        })
        if len(members) >= 100:
            break
    return _json_response({"ok": True, "members": members, "cached_member_count": len(source), "guild_member_count": guild.member_count or len(source)})


async def api_dm_templates(request: web.Request) -> web.Response:
    session = require_permission(request, "dm_members")
    bot = request.app.get(BOT_APP_KEY)
    if request.method == "GET":
        guild = _dm_guild(bot, request.query.get("guild_id"))
        if guild is None:
            return _error("السيرفر غير متاح للبوت", 404)
        templates = dm_store.list_templates(guild.id, query=request.query.get("q", ""), category=request.query.get("category", ""), favorites_only=request.query.get("favorites") == "1")
        categories = sorted({item.get("category", "General") for item in dm_store.list_templates(guild.id)})
        return _json_response({"ok": True, "templates": templates, "categories": categories})
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    guild = _dm_guild(bot, body.get("guild_id"))
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    action, raw_template_id = body.get("action", "save"), body.get("template_id")
    if raw_template_id is not None and not str(raw_template_id).isdigit():
        return _error("معرّف القالب غير صالح")
    if action in {"delete", "duplicate", "favorite"} and raw_template_id is None:
        return _error("اختر قالبًا أولًا")
    try:
        if action == "delete":
            if not dm_store.delete_template(guild.id, raw_template_id):
                return _error("القالب غير موجود", 404)
            result = {"deleted": True}
        elif action == "favorite":
            if not dm_store.set_template_favorite(guild.id, raw_template_id, bool(body.get("favorite"))):
                return _error("القالب غير موجود", 404)
            result = {"favorite": bool(body.get("favorite"))}
        elif action == "duplicate":
            name = str(body.get("name", "")).strip()
            if not name or len(name) > 100:
                return _error("اسم النسخة مطلوب (حد أقصى 100 حرف)")
            new_id = dm_store.duplicate_template(guild.id, raw_template_id, name, int(session.get("user_id") or 0))
            if new_id is None:
                return _error("القالب غير موجود", 404)
            result = {"template_id": new_id}
        elif action == "install_defaults":
            existing = {item["name"].casefold() for item in dm_store.list_templates(guild.id)}
            created = 0
            for name, embed_design, category, description in DM_STARTER_TEMPLATES:
                if name.casefold() in existing:
                    continue
                dm_store.create_template(guild.id, name, embed_design, int(session.get("user_id") or 0), description=description, category=category)
                created += 1
            result = {"created": created}
        elif action == "save":
            name = str(body.get("name", "")).strip()
            content = str(body.get("content", ""))
            design = body.get("embed") or {}
            if not name or len(name) > 100:
                return _error("اسم القالب مطلوب (حد أقصى 100 حرف)")
            render_message({"content": content, "embed": design}, {})
            description, category = str(body.get("description", ""))[:200], str(body.get("category", "General") or "General").strip()[:40]
            template_id = body.get("template_id")
            if template_id:
                if not dm_store.update_template(guild.id, template_id, name, design, content=content, description=description, category=category):
                    return _error("القالب غير موجود", 404)
                result = {"template_id": int(template_id)}
            else:
                result = {"template_id": dm_store.create_template(guild.id, name, design, session.get("user_id", 0), content=content, description=description, category=category)}
        else:
            return _error("عملية القوالب غير صالحة")
    except LookupError as exc:
        return _error(str(exc), 404)
    except ValueError as exc:
        return _error(str(exc))
    except sqlite3.IntegrityError:
        return _error("يوجد قالب بهذا الاسم في السيرفر", 409)
    _audit_dm(session, f"dm_template_{action}", f"guild={guild.id} template={result.get('template_id', '')}")
    return _json_response({"ok": True, **result})


async def api_dm_preview(request: web.Request) -> web.Response:
    session = require_permission(request, "dm_members")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    bot = request.app.get(BOT_APP_KEY)
    guild = _dm_guild(bot, body.get("guild_id"))
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    member = await _dm_member(guild, body.get("member_id"))
    if member is None:
        return _error("العضو غير موجود في السيرفر", 404)
    try:
        template, rendered, _ = await _resolve_dm_message(body, bot, guild, member, session)
    except LookupError as exc:
        return _error(str(exc), 404)
    except ValueError as exc:
        return _error(str(exc))
    now = time.time()
    for token, preview in list(DM_PREVIEW_TOKENS.items()):
        if preview["expires_at"] <= now:
            DM_PREVIEW_TOKENS.pop(token, None)
    while len(DM_PREVIEW_TOKENS) >= 5000:
        DM_PREVIEW_TOKENS.pop(next(iter(DM_PREVIEW_TOKENS)))
    token = secrets.token_urlsafe(32)
    embed_data = rendered["embed"].to_dict() if rendered.get("embed") else None
    DM_PREVIEW_TOKENS[token] = {
        "sender_id": int(session.get("user_id") or 0), "sender_code": session.get("code", "") if session.get("virtual") else "", "sender_virtual": bool(session.get("virtual")), "guild_id": guild.id, "recipient_id": member.id,
        "template_id": int(template["template_id"]) if template else (int(body["template_id"]) if body.get("template_id") else None),
        "content": rendered.get("content") or "", "embed": embed_data, "expires_at": now + DM_PREVIEW_TTL,
    }
    return _json_response({"ok": True, "preview_token": token, "recipient": {"id": str(member.id), "name": member.display_name}, "content": rendered.get("content") or "", "embed": embed_data})


async def api_dm_send(request: web.Request) -> web.Response:
    session = require_permission(request, "dm_members")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict) or body.get("confirm") is not True or not isinstance(body.get("preview_token"), str):
        return _error("عاين الرسالة وأكّد الإرسال قبل المتابعة")
    preview = DM_PREVIEW_TOKENS.get(body["preview_token"])
    if preview is None or preview["expires_at"] <= time.time():
        return _error("انتهت المعاينة أو استُخدمت سابقًا؛ أنشئ معاينة جديدة", 409)
    sender_id = int(session.get("user_id") or 0)
    if sender_id != preview["sender_id"]:
        return _error("معاينة الرسالة لا تطابق جلسة المرسل", 403)
    if bool(session.get("virtual")) != bool(preview.get("sender_virtual")) or (session.get("virtual") and session.get("code", "") != preview.get("sender_code", "")):
        return _error("معاينة الرسالة مرتبطة بجلسة Dashboard أخرى", 403)
    if str(body.get("guild_id")) != str(preview["guild_id"]) or str(body.get("member_id")) != str(preview["recipient_id"]):
        return _error("السيرفر أو المستلم تغير بعد المعاينة", 409)
    bot = request.app.get(BOT_APP_KEY)
    guild = _dm_guild(bot, preview["guild_id"])
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    DM_PREVIEW_TOKENS.pop(body["preview_token"], None)
    member = await _dm_member(guild, preview["recipient_id"])
    if member is None:
        return _error("العضو غير موجود في السيرفر", 404)
    embed_json, content = preview.get("embed"), preview.get("content", "")
    try:
        embed = discord.Embed.from_dict(embed_json) if embed_json else None
    except (TypeError, ValueError):
        return _error("المعاينة المحفوظة لم تعد صالحة", 409)
    if not dm_store.reserve_send(sender_id, maximum=20):
        return _error("وصلت حد 20 رسالة مباشرة بالساعة", 429)
    try:
        await member.send(content=content or None, embed=embed)
    except (discord.Forbidden, discord.HTTPException) as exc:
        error = "المستلم أغلق الرسائل الخاصة" if isinstance(exc, discord.Forbidden) else f"Discord HTTP {exc.status}"
        dm_id = dm_store.record_send(guild.id, member.id, sender_id, preview.get("template_id"), "failed", error, embed_json or {}, content)
        _audit_dm(session, "dm_send_failed", f"guild={guild.id} member={member.id} dm_id={dm_id} error={error}")
        return _error(error, 502)
    dm_id = dm_store.record_send(guild.id, member.id, sender_id, preview.get("template_id"), "success", "", embed_json or {}, content)
    _audit_dm(session, "dm_send", f"guild={guild.id} member={member.id} dm_id={dm_id}")
    logger.info("Dashboard DM sent guild=%s recipient=%s sender=%s dm_id=%s", guild.id, member.id, sender_id, dm_id)
    return _json_response({"ok": True, "status": "success", "dm_id": dm_id, "recipient_id": str(member.id)})


async def api_dm_history(request: web.Request) -> web.Response:
    require_permission(request, "dm_members")
    bot = request.app.get(BOT_APP_KEY)
    guild = _dm_guild(bot, request.query.get("guild_id"))
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    try:
        limit = max(1, min(500, int(request.query.get("limit", "100"))))
    except (TypeError, ValueError, OverflowError):
        return _error("قيمة limit غير صالحة")
    history = dm_store.recent_history(guild.id, limit, status=request.query.get("status", ""), query=request.query.get("q", ""))
    members = {str(member.id): member.display_name for member in getattr(guild, "members", []) or []}
    for item in history:
        item["recipient_name"] = members.get(str(item["recipient_id"]), str(item["recipient_id"]))
        item["sender_name"] = resolve_name(bot, item["sender_id"]) if item["sender_id"] > 0 else "Dashboard"
    return _json_response({"ok": True, "history": history})


async def api_dm_bulk_preview(request: web.Request) -> web.Response:
    session = require_permission(request, "dm_members")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")
    bot = request.app.get(BOT_APP_KEY)
    guild = _dm_guild(bot, body.get("guild_id"))
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    filters = body.get("filters", {}) if isinstance(body.get("filters"), dict) else {}
    mode = str(filters.get("mode", "all")).lower()
    if mode not in {"all", "roles", "selected"}:
        return _error("طريقة الاستهداف غير صالحة")
    if mode == "roles" and not any(str(value).isdigit() for value in filters.get("role_ids", [])):
        return _error("اختر Role واحدة على الأقل لوضع الاستهداف حسب Role", 400)
    if mode == "selected" and not any(str(value).isdigit() for value in filters.get("member_ids", [])):
        return _error("أدخل Member IDs صحيحة لوضع الاستهداف المحدد", 400)
    members, complete = await _ensure_dm_member_cache(guild)
    recipient_ids = _target_member_ids(members, filters, session, guild)
    if not recipient_ids:
        return _error("لا توجد أهداف مطابقة للفلاتر", 400)
    if len(recipient_ids) > DM_BULK_MAX_RECIPIENTS:
        return _error(f"عدد المستلمين يتجاوز الحد الآمن {DM_BULK_MAX_RECIPIENTS}", 400)
    if not complete:
        return _error("قائمة أعضاء Discord غير مكتملة؛ فعّل Server Members Intent ثم أعد المحاولة", 503)
    sample_member = next((member for member in members if int(member.id) == recipient_ids[0]), None)
    try:
        template, rendered, design = await _resolve_dm_message(body, bot, guild, sample_member, session)
    except LookupError as exc:
        return _error(str(exc), 404)
    except ValueError as exc:
        return _error(str(exc))
    try:
        ppm = min(60, max(5, int(body.get("messages_per_minute", 20))))
    except (TypeError, ValueError, OverflowError):
        return _error("سرعة الإرسال يجب أن تكون بين 5 و60 رسالة بالدقيقة")
    now = time.time()
    for token, preview in list(DM_BULK_PREVIEW_TOKENS.items()):
        if preview["expires_at"] <= now:
            DM_BULK_PREVIEW_TOKENS.pop(token, None)
    while len(DM_BULK_PREVIEW_TOKENS) >= 200:
        DM_BULK_PREVIEW_TOKENS.pop(next(iter(DM_BULK_PREVIEW_TOKENS)))
    token = secrets.token_urlsafe(32)
    filters = body.get("filters", {}) if isinstance(body.get("filters"), dict) else {}
    target_summary = {"mode": filters.get("mode", "all"), "exclude_bots": bool(filters.get("exclude_bots", True)), "exclude_staff": bool(filters.get("exclude_staff", False)), "exclude_self": bool(filters.get("exclude_self", True)), "role_ids": list(filters.get("role_ids", [])), "exclude_role_ids": list(filters.get("exclude_role_ids", []))}
    DM_BULK_PREVIEW_TOKENS[token] = {
        "sender_id": int(session.get("user_id") or 0), "sender_code": session.get("code", "") if session.get("virtual") else "", "sender_virtual": bool(session.get("virtual")), "guild_id": guild.id, "recipient_ids": recipient_ids,
        "template_id": template["template_id"] if template else (int(body["template_id"]) if body.get("template_id") else None),
        "content": design.get("content", ""), "embed": design.get("embed", {}), "reason": str(body.get("reason", ""))[:1000],
        "messages_per_minute": ppm, "target_summary": target_summary, "expires_at": now + DM_PREVIEW_TTL,
    }
    sample_members = [
        {"id": str(member.id), "name": member.display_name, "username": member.name}
        for member in members if int(member.id) in set(recipient_ids[:10])
    ]
    return _json_response({"ok": True, "preview_token": token, "count": len(recipient_ids), "complete": complete, "sample": [str(item) for item in recipient_ids[:10]], "sample_members": sample_members, "target": target_summary, "messages_per_minute": ppm, "content": rendered.get("content") or "", "embed": rendered["embed"].to_dict() if rendered.get("embed") else None})


async def api_dm_bulk_start(request: web.Request) -> web.Response:
    session = require_permission(request, "dm_members")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict) or body.get("confirm") is not True or not isinstance(body.get("preview_token"), str):
        return _error("راجع المعاينة وأكّد عدد المستلمين قبل بدء DM All")
    preview = DM_BULK_PREVIEW_TOKENS.get(body["preview_token"])
    if preview is None or preview["expires_at"] <= time.time():
        return _error("انتهت معاينة DM All؛ اعمل معاينة جديدة", 409)
    sender_id = int(session.get("user_id") or 0)
    if sender_id != preview["sender_id"]:
        return _error("المعاينة لا تطابق جلسة المرسل", 403)
    if bool(session.get("virtual")) != bool(preview.get("sender_virtual")) or (session.get("virtual") and session.get("code", "") != preview.get("sender_code", "")):
        return _error("المعاينة مرتبطة بجلسة Dashboard أخرى", 403)
    try:
        confirmed_count = int(body.get("confirmed_count", -1))
    except (TypeError, ValueError, OverflowError):
        confirmed_count = -1
    if confirmed_count != len(preview["recipient_ids"]):
        return _error("عدد المستلمين تغير؛ أعد المعاينة قبل الإرسال", 409)
    manager = request.app.get(DM_MANAGER_KEY)
    if manager is None:
        return _error("DM worker غير متاح حاليًا", 503)
    try:
        job_id = manager.create_and_start(guild_id=preview["guild_id"], sender_id=sender_id, sender_name=session.get("name", "Dashboard"), template_id=preview.get("template_id"), reason=preview.get("reason", ""), content=preview.get("content", ""), embed=preview.get("embed") or {}, options={"messages_per_minute": preview.get("messages_per_minute", 20), "target": preview.get("target_summary", {}), "sender_code": session.get("code", "") if session.get("virtual") else ""}, recipient_ids=preview["recipient_ids"])
    except RuntimeError as exc:
        return _error(str(exc), 409)
    DM_BULK_PREVIEW_TOKENS.pop(body["preview_token"], None)
    _audit_dm(session, "dm_bulk_start", f"guild={preview['guild_id']} job={job_id} recipients={len(preview['recipient_ids'])}")
    return _json_response({"ok": True, "job_id": job_id, "count": len(preview["recipient_ids"]), "status": "queued"})


async def api_dm_bulk_jobs(request: web.Request) -> web.Response:
    require_permission(request, "dm_members")
    bot = request.app.get(BOT_APP_KEY)
    guild = _dm_guild(bot, request.query.get("guild_id"))
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    try:
        limit = max(1, min(100, int(request.query.get("limit", "30"))))
    except (TypeError, ValueError, OverflowError):
        return _error("قيمة limit غير صالحة")
    return _json_response({"ok": True, "jobs": dm_store.list_bulk_jobs(guild.id, limit)})


async def api_dm_bulk_status(request: web.Request) -> web.Response:
    require_permission(request, "dm_members")
    bot = request.app.get(BOT_APP_KEY)
    guild = _dm_guild(bot, request.query.get("guild_id"))
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    job = dm_store.get_bulk_job(request.query.get("job_id", ""))
    if not job or int(job["guild_id"]) != int(guild.id):
        return _error("حملة DM غير موجودة", 404)
    try:
        item_limit = max(1, min(100, int(request.query.get("items", "50"))))
    except (TypeError, ValueError, OverflowError):
        item_limit = 50
    job["items"] = dm_store.bulk_items(job["job_id"], item_limit)
    return _json_response({"ok": True, "job": job})


async def api_dm_bulk_control(request: web.Request) -> web.Response:
    session = require_permission(request, "dm_members")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict) or not body.get("job_id"):
        return _error("job_id مطلوب")
    job = dm_store.get_bulk_job(body["job_id"])
    if not job:
        return _error("حملة DM غير موجودة", 404)
    session_id = int(session.get("user_id") or 0)
    authorized = session_id == int(job["sender_id"])
    if session.get("virtual"):
        authorized = bool(session.get("code")) and session.get("code") == (job.get("options") or {}).get("sender_code", "")
    if not authorized and not (session_id > 0 and ranks.is_owner(session_id)):
        return _error("ما عندكش صلاحية تتحكم في حملة أنشأها مستخدم آخر", 403)
    bot = request.app.get(BOT_APP_KEY)
    guild = _dm_guild(bot, job["guild_id"])
    if guild is None:
        return _error("السيرفر غير متاح للبوت", 404)
    action = str(body.get("action", "")).lower()
    manager = request.app.get(DM_MANAGER_KEY)
    if action == "pause" and job["status"] in {"queued", "running"}:
        dm_store.set_bulk_job_status(job["job_id"], "paused")
    elif action == "resume" and job["status"] in {"paused", "interrupted"}:
        if int(job.get("uncertain", 0)):
            return _error("الحملة فيها عمليات غير مؤكدة بعد انقطاع سابق؛ راجع السجل قبل الاستئناف", 409)
        dm_store.set_bulk_job_status(job["job_id"], "queued")
        if manager is None:
            return _error("DM worker غير متاح حاليًا", 503)
        if not manager.resume_job(job["job_id"]):
            return _error("تعذر استئناف الحملة حاليًا", 409)
    elif action == "cancel" and job["status"] in {"queued", "running", "paused"}:
        dm_store.set_bulk_job_status(job["job_id"], "cancel_requested")
    else:
        return _error("لا يمكن تنفيذ العملية على حالة الحملة الحالية", 409)
    _audit_dm(session, f"dm_bulk_{action}", f"guild={guild.id} job={job['job_id']}")
    return _json_response({"ok": True, "status": dm_store.get_bulk_job(job["job_id"])["status"]})
async def api_money(request: web.Request) -> web.Response:
    session = require_permission(request, "")  # الصلاحية تتحدد حسب العملية تحت
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")

    virtual = session.get("virtual", False)
    actor_id = int(session["user_id"]) if (not virtual and session["user_id"].isdigit()) else 0
    user_id = str(body.get("user_id") or "").strip()
    action = body.get("action")          # add / remove / reset
    mode = body.get("mode", "wallet")    # wallet / bank
    try:
        amount = int(body.get("amount", 0))
    except (TypeError, ValueError):
        return _error("المبلغ لازم يكون رقمًا")

    if isinstance(body.get("amount", 0), bool) or not user_id.isdigit() or int(user_id) <= 0:
        return _error("آيدي العضو غير صالح")
    if mode not in {"wallet", "bank"}:
        return _error("نوع الرصيد غير صالح")

    settings = load_settings().get("economy", {})
    try:
        starting_balance = int(settings["starting_balance"])
        max_wallet = int(settings["max_wallet"])
        max_bank = int(settings["max_bank"])
        if min(max_wallet, max_bank) < 0:
            raise ValueError("invalid balance caps")
    except (KeyError, TypeError, ValueError, OverflowError):
        logger.exception("Invalid Dashboard economy settings")
        return _error("إعدادات الاقتصاد غير صالحة.", 503)

    bot = request.app.get(BOT_APP_KEY)
    if virtual:
        actor_name = session.get("name") or f"كود …{session.get('code', '')[-4:]}"
    else:
        actor_name = resolve_name(bot, actor_id)

    if action == "add":
        if not _session_has(session, "add_money"):
            return _error("ما عندك صلاحية إضافة فلوس", 403)
        if amount <= 0:
            return _error("المبلغ لازم يكون أكبر من صفر")
        if virtual:
            usage_key = f"code_{session.get('code', '؟')}"
            limit = ranks.get_daily_limit_for_rank(session["rank"])
        else:
            limit = ranks.get_daily_limit(actor_id)
            usage_key = actor_id
        if not ranks.reserve_daily_quota(usage_key, amount, limit):
            remaining = ranks.remaining_daily_quota(usage_key)
            return _error(f"تجاوزت حدك اليومي — المتبقي لك: {remaining or 0}", 403)

        outcome = {}
        balance_key = "bank" if mode == "bank" else "wallet"
        cap = max_bank if mode == "bank" else max_wallet

        def add_money(user):
            try:
                balance = max(0, int(user.get(balance_key, 0)))
                earned = max(0, int(user.get("total_earned", 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            credited = min(amount, max(0, cap - balance))
            if credited <= 0:
                outcome["full"] = True
                return False
            user[balance_key] = balance + credited
            if balance_key == "wallet":
                user["total_earned"] = earned + credited
            outcome.update({"credited": credited, "balance": balance + credited})
            return True

        try:
            new_data = atomic_update_user(int(user_id), add_money, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            ranks.adjust_daily_usage(usage_key, -amount)
            logger.exception("Dashboard money addition failed actor=%s target=%s", actor_id, user_id)
            return _error("تعذر حفظ الإضافة المالية. حاول لاحقًا.", 500)
        if outcome.get("full") or outcome.get("invalid"):
            ranks.adjust_daily_usage(usage_key, -amount)
            return _error("رصيد العضو وصل إلى الحد الأقصى أو أن بياناته غير صالحة.")
        if outcome["credited"] < amount:
            ranks.adjust_daily_usage(usage_key, outcome["credited"] - amount)
        try:
            ranks.log_action(actor_id, actor_name, "add_money",
                             f"أضاف {outcome['credited']} لـ {user_id} ({'بنك' if mode == 'bank' else 'محفظة'})", "dashboard")
        except (OSError, TypeError, ValueError):
            logger.exception("Could not write Dashboard money audit log")
        logger.info("dashboard_add_money actor=%s target=%s amount=%s", actor_id, user_id, outcome["credited"])
        return _json_response({"ok": True, "wallet": new_data["wallet"], "bank": new_data["bank"]})

    elif action == "remove":
        if not _session_has(session, "remove_money"):
            return _error("ما عندك صلاحية سحب فلوس", 403)
        if amount <= 0:
            return _error("المبلغ لازم يكون أكبر من صفر")
        balance_key = "bank" if mode == "bank" else "wallet"
        outcome = {}

        def remove_money(user):
            try:
                balance = max(0, int(user.get(balance_key, 0)))
            except (TypeError, ValueError, OverflowError):
                outcome["invalid"] = True
                return False
            removed = min(amount, balance)
            user[balance_key] = balance - removed
            outcome["removed"] = removed
            return True

        try:
            new_data = atomic_update_user(int(user_id), remove_money, starting_balance)
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Dashboard money removal failed actor=%s target=%s", actor_id, user_id)
            return _error("تعذر حفظ السحب المالي. حاول لاحقًا.", 500)
        if outcome.get("invalid"):
            return _error("رصيد العضو غير صالح.")
        try:
            ranks.log_action(actor_id, actor_name, "remove_money",
                             f"سحب {outcome['removed']} من {user_id} ({'بنك' if mode == 'bank' else 'محفظة'})", "dashboard")
        except (OSError, TypeError, ValueError):
            logger.exception("Could not write Dashboard money audit log")
        logger.info("dashboard_remove_money actor=%s target=%s amount=%s", actor_id, user_id, outcome["removed"])
        return _json_response({"ok": True, "wallet": new_data["wallet"], "bank": new_data["bank"]})

    elif action == "reset":
        if not _session_has(session, "reset_user"):
            return _error("ما عندك صلاحية التصفير", 403)
        try:
            new_data = atomic_update_user(
                int(user_id), lambda user: user.update({"wallet": 0, "bank": 0}), starting_balance
            )
        except (OSError, TypeError, ValueError, OverflowError):
            logger.exception("Dashboard balance reset failed actor=%s target=%s", actor_id, user_id)
            return _error("تعذر تصفير الرصيد. حاول لاحقًا.", 500)
        ranks.log_action(actor_id, actor_name, "reset_user", f"تصفير رصيد {user_id}", "dashboard")
        return _json_response({"ok": True, "wallet": new_data["wallet"], "bank": new_data["bank"]})

    return _error("عملية غير معروفة")


# ---------------------------------------------------------------------------
# API: الفريق والأكواد
# ---------------------------------------------------------------------------

async def api_staff(request: web.Request) -> web.Response:
    require_permission(request, "view_stats")
    bot = request.app.get(BOT_APP_KEY)

    staff = []
    for uid, entry in ranks.list_staff().items():
        staff.append({
            "id": uid,
            "name": resolve_name(bot, uid),
            "rank": entry.get("rank"),
            "rank_name": ranks.RANK_NAMES_AR.get(entry.get("rank", ""), entry.get("rank", "")),
            "code": entry.get("code", ""),
            "activated_at": entry.get("activated_at", 0),
            "added_today": ranks.get_today_usage(int(uid)),
        })

    codes = []
    for code, entry in ranks.list_codes().items():
        codes.append({
            "code": code,
            "rank": entry.get("rank"),
            "rank_name": ranks.RANK_NAMES_AR.get(entry.get("rank", ""), entry.get("rank", "")),
            "created_at": entry.get("created_at", 0),
            "used_by": entry.get("used_by"),
            "used_by_name": resolve_name(bot, entry["used_by"]) if entry.get("used_by") else None,
            "revoked": entry.get("revoked", False),
            "note": entry.get("note", ""),
        })
    codes.sort(key=lambda c: c["created_at"], reverse=True)

    return _json_response({
        "ok": True,
        "staff": staff,
        "codes": codes,
        "permissions_meta": {
            "all": ranks.ALL_PERMISSIONS,
            "defaults": ranks.DEFAULT_PERMISSIONS,
            "daily_limits": ranks.DEFAULT_DAILY_LIMITS,
            "owner_id": ranks.get_owner_id(),
        },
    })


async def api_staff_code(request: web.Request) -> web.Response:
    session = require_permission(request, "manage_codes")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")

    rank = body.get("rank", "")
    note = str(body.get("note", ""))[:100]
    actor_rank = session.get("rank", "")
    actor_level = 4 if actor_rank == "owner" else ranks.RANK_LEVELS.get(actor_rank, 0)
    if rank not in ranks.RANK_LEVELS:
        return _error("رتبة غير صالحة")
    if actor_rank != "owner" and ranks.RANK_LEVELS[rank] >= actor_level:
        return _error("لا يمكنك إنشاء كود لرتبة مساوية أو أعلى من رتبتك.", 403)
    try:
        count = max(1, min(int(body.get("count", 1)), 5))
    except (TypeError, ValueError):
        count = 1

    actor_id = int(session["user_id"]) if (not session.get("virtual") and session["user_id"].isdigit()) else 0
    codes = []
    for _ in range(count):
        code = ranks.generate_code(rank, actor_id, note)
        if code:
            codes.append(code)
    if not codes:
        return _error("رتبة غير صالحة")

    ranks.log_action(actor_id, session.get("name", "لوحة التحكم"), "generate_codes",
                     f"{len(codes)} كود رتبة {rank}", "dashboard")
    return _json_response({"ok": True, "codes": codes})


async def api_staff_revoke(request: web.Request) -> web.Response:
    session = require_permission(request, "manage_codes")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")

    code = (body.get("code") or "").strip().upper()
    entry = ranks.list_codes().get(code)
    if not isinstance(entry, dict):
        return _error("الكود غير موجود", 404)
    actor_rank = session.get("rank", "")
    actor_level = 4 if actor_rank == "owner" else ranks.RANK_LEVELS.get(actor_rank, 0)
    target_level = ranks.RANK_LEVELS.get(entry.get("rank", ""), 0)
    if actor_rank != "owner" and target_level >= actor_level:
        return _error("لا يمكنك سحب كود لرتبة مساوية أو أعلى من رتبتك.", 403)
    if ranks.revoke_code(code):
        actor_id = int(session["user_id"]) if (not session.get("virtual") and session["user_id"].isdigit()) else 0
        ranks.log_action(actor_id, session.get("name", "لوحة التحكم"), "revoke_code", f"سحب {code}", "dashboard")
        return _json_response({"ok": True})
    return _error("الكود غير موجود", 404)


async def api_staff_remove(request: web.Request) -> web.Response:
    session = require_permission(request, "manage_staff")
    try:
        body = await request.json()
    except Exception:
        return _error("طلب غير صالح")
    if not isinstance(body, dict):
        return _error("طلب غير صالح")

    user_id = str(body.get("user_id") or "").strip()
    if not user_id.isdigit() or int(user_id) <= 0:
        return _error("آيدي غير صالح")
    actor_rank = session.get("rank", "")
    target_rank = ranks.get_rank(int(user_id)) or ""
    actor_level = 4 if actor_rank == "owner" else ranks.RANK_LEVELS.get(actor_rank, 0)
    target_level = 4 if target_rank == "owner" else ranks.RANK_LEVELS.get(target_rank, 0)
    if target_rank and actor_rank != "owner" and target_level >= actor_level:
        return _error("لا يمكنك إزالة عضو رتبته مساوية أو أعلى من رتبتك.", 403)
    if ranks.remove_staff(int(user_id)):
        actor_id = int(session["user_id"]) if not session.get("virtual") and session["user_id"].isdigit() else 0
        ranks.log_action(actor_id, session.get("name", "لوحة التحكم"), "remove_staff", f"إزالة {user_id}", "dashboard")
        return _json_response({"ok": True})
    return _error("العضو ما عنده رتبة أو هو المالك", 400)


# ---------------------------------------------------------------------------
# API: الإعدادات
# ---------------------------------------------------------------------------

async def api_settings_get(request: web.Request) -> web.Response:
    require_permission(request, "manage_settings")
    return _json_response({"ok": True, "settings": load_settings()})


async def api_settings_save(request: web.Request) -> web.Response:
    session = require_permission(request, "manage_settings")
    try:
        body = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return _error("JSON غير صالح")
    settings = body.get("settings")
    if not isinstance(settings, dict):
        return _error("بنية الإعدادات غير صالحة")

    # Security-sensitive rank policy is owner-only. Keeping this rule here, at
    # the API boundary, prevents a Dev from rewriting permissions and escalating
    # to owner-equivalent access through the mutable settings document.
    ranks_cfg = settings.get("ranks")
    if isinstance(ranks_cfg, dict):
        protected_rank_keys = {"owner_id", "permissions", "daily_limits"}
        attempted_protected = protected_rank_keys.intersection(ranks_cfg)
        if attempted_protected and session.get("rank") != "owner":
            return _error("إعدادات الرتب الحساسة للمالك فقط", 403)
        if "owner_id" in ranks_cfg and session.get("rank") == "owner":
            configured_owner = str(ranks_cfg.get("owner_id") or "").strip()
            real_owner = ranks.get_owner_id()
            if configured_owner and configured_owner != str(real_owner or ""):
                return _error("OWNER_ID لا يمكن تغييره من الداشبورد", 400)
        else:
            # Never persist a dashboard-provided owner override.
            ranks_cfg = dict(ranks_cfg)
            ranks_cfg.pop("owner_id", None)
            settings = dict(settings)
            settings["ranks"] = ranks_cfg

    save_settings(settings)
    runtime_synced = _sync_ai_runtime(request)
    ranks.log_action(0, "لوحة التحكم", "save_settings", "تعديل الإعدادات من اللوحة", "dashboard")
    return _json_response({"ok": True, "runtime_synced": runtime_synced})


# ---------------------------------------------------------------------------
# API: السجل والسوق
# ---------------------------------------------------------------------------

async def api_logs(request: web.Request) -> web.Response:
    require_permission(request, "view_logs")
    logs = sorted(ranks.load_logs(), key=lambda l: l.get("ts", 0), reverse=True)[:100]
    return _json_response({"ok": True, "logs": logs})


async def api_terminal_stream(request: web.Request) -> web.StreamResponse:
    require_permission(request, "view_logs")
    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    queue = live_terminal.subscribe()
    try:
        await response.prepare(request)
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                await response.write(b": heartbeat\n\n")
                continue
            payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            await response.write(f"data: {payload}\n\n".encode("utf-8"))
    except ConnectionResetError:
        pass
    finally:
        live_terminal.unsubscribe(queue)
    return response


async def api_market(request: web.Request) -> web.Response:
    require_permission(request, "view_stats")
    cfg = load_settings().get("stocks", {})
    market = load_market()
    stocks = []
    for entry in cfg.get("symbols", []):
        sym = entry["symbol"].upper()
        data = market.get("prices", {}).get(sym, {"price": entry.get("base_price", 100), "prev": 0, "history": []})
        change = ((data["price"] - data["prev"]) / data["prev"] * 100) if data.get("prev") else 0
        stocks.append({
            "symbol": sym,
            "name": entry.get("name", ""),
            "emoji": entry.get("emoji", "📈"),
            "price": data.get("price", 0),
            "change": round(change, 2),
            "history": data.get("history", [])[-24:],
        })

    loans_data = load_loans()
    _refresh_overdue(loans_data)
    loans = [{
        "id": l["id"],
        "lender": l["lender"],
        "borrower": l["borrower"],
        "principal": l["principal"],
        "total": l["total"],
        "paid": l["paid"],
        "status": l["status"],
        "due": l["due"],
    } for l in loans_data["loans"][-50:]]

    return _json_response({"ok": True, "stocks": stocks, "loans": loans, "last_update": market.get("last_update", 0)})


# ---------------------------------------------------------------------------
# التشغيل
# ---------------------------------------------------------------------------

@web.middleware
async def error_middleware(request, handler):
    """يحول كل الأخطاء إلى استجابات JSON موحدة (تناسب الواجهة)."""
    try:
        return await handler(request)
    except web.HTTPException as e:
        if e.status >= 400:
            return web.json_response(
                {"ok": False, "error": e.reason or "خطأ في الطلب"},
                status=e.status,
            )
        raise
    except Exception as e:  # خطأ غير متوقع — نرجع JSON بدل صفحة خطأ نصية
        logger.exception(f"خطأ غير متوقع في {request.path}: {e}")
        return web.json_response(
            {"ok": False, "error": "صار خطأ غير متوقع بالسيرفر"},
            status=500,
        )


async def _dm_manager_context(app):
    manager = app[DM_MANAGER_KEY]
    manager.bind_bot(app[BOT_APP_KEY])
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


def create_web_app(bot) -> web.Application:
    app = web.Application(middlewares=[error_middleware])
    app[BOT_APP_KEY] = bot
    app[DM_MANAGER_KEY] = DMBulkManager(dm_store)
    app.cleanup_ctx.append(_dm_manager_context)

    app.router.add_get("/", serve_index)

    app.router.add_post("/api/login", api_login)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/users", api_users)
    app.router.add_get("/api/members", api_members)
    app.router.add_get("/api/commands", api_commands)
    app.router.add_post("/api/commands/execute", api_command_execute)
    app.router.add_post("/api/atria", api_atria)
    app.router.add_get("/api/ai/status", api_ai_status)
    app.router.add_post("/api/ai/test", api_ai_test)
    app.router.add_post("/api/ai/moderation/kill", api_ai_moderation_kill)
    app.router.add_route("GET", "/api/ai/moderation/settings", api_ai_moderation_settings)
    app.router.add_route("POST", "/api/ai/moderation/settings", api_ai_moderation_settings)
    app.router.add_get("/api/ai/moderation/logs", api_ai_moderation_logs)
    app.router.add_get("/api/moderation/warnings", api_moderation_warnings)
    app.router.add_get("/api/voice/targets", api_voice_targets)
    app.router.add_post("/api/voice/action", api_voice_action)
    app.router.add_get("/api/bot/profile", api_bot_profile)
    app.router.add_post("/api/bot/presence", api_bot_presence)
    app.router.add_get("/api/dm/center", api_dm_center)
    app.router.add_get("/api/dm/members", api_dm_members)
    app.router.add_get("/api/dm/roles", api_dm_roles)
    app.router.add_route("GET", "/api/dm/templates", api_dm_templates)
    app.router.add_route("POST", "/api/dm/templates", api_dm_templates)
    app.router.add_post("/api/dm/preview", api_dm_preview)
    app.router.add_post("/api/dm/send", api_dm_send)
    app.router.add_get("/api/dm/history", api_dm_history)
    app.router.add_post("/api/dm/bulk/preview", api_dm_bulk_preview)
    app.router.add_post("/api/dm/bulk/start", api_dm_bulk_start)
    app.router.add_get("/api/dm/bulk/jobs", api_dm_bulk_jobs)
    app.router.add_get("/api/dm/bulk/status", api_dm_bulk_status)
    app.router.add_post("/api/dm/bulk/control", api_dm_bulk_control)
    app.router.add_route("GET", "/api/ai/chat/settings", api_ai_chat_settings)
    app.router.add_route("POST", "/api/ai/chat/settings", api_ai_chat_settings)
    app.router.add_post("/api/ai/chat/test", api_ai_chat_test)
    app.router.add_post("/api/ai/chat/stream", api_ai_chat_stream)
    app.router.add_get("/api/ai/chat/logs", api_ai_chat_logs)
    app.router.add_post("/api/money", api_money)
    app.router.add_get("/api/staff", api_staff)
    app.router.add_post("/api/staff/code", api_staff_code)
    app.router.add_post("/api/staff/revoke", api_staff_revoke)
    app.router.add_post("/api/staff/remove", api_staff_remove)
    app.router.add_get("/api/settings", api_settings_get)
    app.router.add_post("/api/settings", api_settings_save)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/terminal/stream", api_terminal_stream)
    app.router.add_get("/api/market", api_market)

    if (DASHBOARD_DIR / "assets").exists():
        app.router.add_static("/assets/", str(DASHBOARD_DIR / "assets"))

    return app


async def start_web_server(bot) -> bool:
    """يشغل سيرفر الداشبورد داخل نفس حلقة أحداث البوت."""
    cfg = load_settings().get("dashboard", {})
    if not cfg.get("enabled", True):
        logger.info("لوحة التحكم معطلة من الإعدادات (dashboard.enabled=false)")
        return False

    host = (os.getenv("DASHBOARD_HOST") or cfg.get("host", "127.0.0.1") or "127.0.0.1").strip()
    raw_port = os.getenv("DASHBOARD_PORT") or cfg.get("port", 8080) or 8080
    try:
        port = int(str(raw_port).strip())
    except (TypeError, ValueError):
        port = 8080

    install_live_terminal()
    app = create_web_app(bot)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        bot._dashboard_runner = runner
        logger.info(f"🖥️ لوحة التحكم تعمل الآن على http://{host}:{port}")
        if not (os.getenv("DASHBOARD_PASSWORD") or cfg.get("password")):
            logger.warning("⚠️ DASHBOARD_PASSWORD غير مضبوط — دخول المالك معطل.")
        return True
    except OSError as e:
        logger.error(f"تعذر تشغيل لوحة التحكم على {host}:{port} — {e}")
        await runner.cleanup()
        return False
    except Exception:
        logger.exception("Could not start Dashboard server on %s:%s", host, port)
        await runner.cleanup()
        return False
