"""
ranks.py
--------
نظام الرتب الإدارية الاحترافي (Dev / Founder / Team).

طرق الحصول على رتبة:
  1) المالك (Owner): آيدي ثابت في ملف .env (OWNER_ID) — يملك كل الصلاحيات
     ولا يمكن سحب رتبته، ويتجاوز كل الحدود.
  2) أكواد الفريق: أكواد تُولَّد من الداشبورد أو أوامر البوت (مثال: CB-DEV-8F3K9A)
     وتُفعَّل بالأمر /activate — الكود يُربط بآيدي العضو نهائيًا.

تسلسل الرتب: dev (3) > founder (2) > team (1)

كل رتبة لها قائمة صلاحيات + حد يومي للإضافة (0 = بلا حد)،
وكل شيء قابل للتعديل من الداشبورد عبر settings.json.
"""

import os
import copy
import logging
import secrets
import string
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot.utils.data_manager import load_settings, _atomic_write, _read_json, _lock, now_ts

STAFF_FILE = Path(__file__).resolve().parent.parent / "data" / "staff.json"
LOGS_FILE = Path(__file__).resolve().parent.parent / "data" / "logs.json"
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# تعريف الرتب والصلاحيات الافتراضية
# ---------------------------------------------------------------------------

RANK_LEVELS: Dict[str, int] = {"dev": 3, "founder": 2, "team": 1}
RANK_EMOJI = {"owner": "👑", "dev": "🛠️", "founder": "💎", "team": "🛡️"}

# كل الصلاحيات الموجودة بالنظام
ALL_PERMISSIONS: List[str] = [
    "view_stats",      # مشاهدة الإحصائيات المباشرة
    "view_users",      # مشاهدة قائمة الأعضاء وأرصدتهم
    "view_logs",       # مشاهدة سجل العمليات
    "add_money",       # إضافة فلوس (لنفسه أو لغيره) من الداشبورد والبوت
    "remove_money",    # سحب فلوس من عضو
    "reset_user",      # تصفير رصيد عضو
    "manage_shop",     # إدارة المتجر (تحفظية — عبر الإعدادات)
    "manage_codes",    # توليد وسحب أكواد الفريق
    "manage_staff",    # تعيين/إزالة أعضاء الفريق مباشرة
    "manage_settings",
    "manage_server", "manage_channels", "manage_messages", "manage_roles",
    "moderate_members", "kick_members", "ban_members", "move_members",
    "mute_members", "deafen_members", "dm_members", "manage_emojis",
    "manage_events",
]

# الصلاحيات الافتراضية لكل رتبة (تُقرأ أيضًا من settings.ranks.permissions لو موجودة)
DEFAULT_PERMISSIONS: Dict[str, List[str]] = {
    "dev": ALL_PERMISSIONS.copy(),
    "founder": [
        "view_stats", "view_users", "view_logs", "manage_server", "manage_channels", "manage_messages", "manage_roles",
        "moderate_members", "kick_members", "ban_members", "move_members", "mute_members", "deafen_members", "dm_members", "manage_emojis", "manage_events",
        "add_money", "remove_money", "reset_user", "manage_codes", "manage_server", "manage_channels", "manage_messages", "manage_roles",
        "moderate_members", "kick_members", "ban_members", "move_members", "mute_members", "deafen_members", "dm_members", "manage_emojis", "manage_events",
    ],
    "team": [
        "view_stats", "view_users", "view_logs", "manage_channels", "moderate_members", "dm_members", "move_members", "mute_members", "deafen_members", "add_money",
    ],
}

# الحد اليومي للإضافة لكل رتبة (0 = بلا حد) — يتجاوزه المالك دائمًا
DEFAULT_DAILY_LIMITS: Dict[str, int] = {
    "dev": 0,          # بلا حد
    "founder": 100000,
    "team": 5000,
}

RANK_NAMES_AR = {"owner": "المالك", "dev": "ديفيلوبر", "founder": "فاوندر", "team": "فريق"}

# هويات جلسات Dashboard غير المرتبطة بعضو Discord. IDs سالبة عمدًا ولا يمكن
# أن تتطابق مع Discord snowflakes. تُستخدم فقط أثناء تنفيذ أوامر Dashboard.
_DASHBOARD_PRINCIPALS: Dict[int, str] = {}

def register_dashboard_principal(principal_id: int, rank: str) -> None:
    if principal_id >= 0 or rank not in RANK_LEVELS:
        raise ValueError("invalid dashboard principal")
    _DASHBOARD_PRINCIPALS[int(principal_id)] = rank

def unregister_dashboard_principal(principal_id: int) -> None:
    _DASHBOARD_PRINCIPALS.pop(int(principal_id), None)

def dashboard_principal_rank(user_id: int) -> Optional[str]:
    return _DASHBOARD_PRINCIPALS.get(int(user_id))


# ---------------------------------------------------------------------------
# تخزين staff.json
# ---------------------------------------------------------------------------

DEFAULT_STAFF = {"codes": {}, "staff": {}, "usage": {}}


def _read_staff() -> Dict[str, Any]:
    with _lock:
        data = _read_json(STAFF_FILE, DEFAULT_STAFF)
        for key in DEFAULT_STAFF:
            if not isinstance(data.get(key), dict):
                logger.warning("Invalid %s section in staff data; using an empty mapping", key)
                data[key] = {}
        return data


def _write_staff(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise TypeError("staff data must be a dictionary")
    with _lock:
        _atomic_write(STAFF_FILE, data)


def get_owner_id() -> Optional[int]:
    """Return the immutable owner identity configured by the deployment environment.

    The owner must never be sourced from mutable dashboard settings; otherwise a
    staff member with ``manage_settings`` could rewrite ``ranks.owner_id`` and
    escalate to owner privileges.
    """
    raw = os.getenv("OWNER_ID", "").strip()
    if not raw.isdigit():
        return None
    try:
        owner_id = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return owner_id if owner_id > 0 else None


def is_owner(user_id: int) -> bool:
    return get_owner_id() == int(user_id)


# ---------------------------------------------------------------------------
# الصلاحيات
# ---------------------------------------------------------------------------

def get_rank(user_id: int) -> Optional[str]:
    """يرجع رتبة العضو أو هوية Dashboard المؤقتة."""
    uid = int(user_id)
    dashboard_rank = dashboard_principal_rank(uid)
    if dashboard_rank:
        return dashboard_rank
    if is_owner(uid):
        return "owner"
    staff = _read_staff()["staff"]
    entry = staff.get(str(uid))
    if entry and entry.get("rank") in RANK_LEVELS:
        return entry["rank"]
    return None


def get_permissions_for_rank(rank: str) -> List[str]:
    """Resolve the effective permissions for a rank from the configured policy."""
    if rank == "owner":
        return ALL_PERMISSIONS.copy()
    if rank not in RANK_LEVELS:
        return []
    configured = load_settings().get("ranks", {}).get("permissions", {})
    perms = configured.get(rank, DEFAULT_PERMISSIONS.get(rank, []))
    if not isinstance(perms, list):
        perms = DEFAULT_PERMISSIONS.get(rank, [])
    if "*" in perms:
        return ALL_PERMISSIONS.copy()
    return [p for p in perms if p in ALL_PERMISSIONS]


def get_permissions(user_id: int) -> List[str]:
    """صلاحيات العضو حسب رتبته (المالك يملك الكل دائمًا)."""
    rank = get_rank(user_id)
    if rank is None:
        return []
    return get_permissions_for_rank(rank)


def has_permission(user_id: int, permission: str) -> bool:
    return permission in get_permissions(user_id)


def get_daily_limit_for_rank(rank: str) -> int:
    """Return the effective daily money-addition quota for a rank."""
    if rank == "owner":
        return 0
    if rank not in RANK_LEVELS:
        return 0
    limits = load_settings().get("ranks", {}).get("daily_limits", {})
    try:
        value = limits.get(rank, DEFAULT_DAILY_LIMITS.get(rank, 0))
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        logger.warning("Invalid configured daily limit for rank=%s", rank)
        return max(0, DEFAULT_DAILY_LIMITS.get(rank, 0))


def get_daily_limit(user_id: int) -> int:
    """الحد اليومي المسموح لإضافة الفلوس لهذه الرتبة (0 = بلا حد)."""
    rank = get_rank(user_id)
    if rank is None:
        return 0
    return get_daily_limit_for_rank(rank)


def get_today_usage(user_id: int) -> int:
    """كم أضاف هذا العضو اليوم (للتحقق من الحد اليومي)."""
    with _lock:
        staff = _read_staff()
        today = time.strftime("%Y-%m-%d")
        entry = staff.get("usage", {}).get(str(user_id), {})
        if not isinstance(entry, dict) or entry.get("date") != today:
            return 0
        try:
            return max(0, int(entry.get("added", 0)))
        except (TypeError, ValueError, OverflowError):
            logger.warning("Invalid daily usage value for principal %s", user_id)
            return 0


def record_usage(user_id: int, amount: int) -> None:
    """يسجل المبلغ المضاف اليوم (لأجل الحد اليومي)."""
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return
    adjust_daily_usage(user_id, amount)


def reserve_daily_quota(user_id: int, amount: int, limit: int) -> bool:
    """Reserve daily addition quota atomically; zero limit means unlimited."""
    if (
        not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0
        or not isinstance(limit, int) or isinstance(limit, bool) or limit < 0
    ):
        return False
    with _lock:
        staff = _read_staff()
        today = time.strftime("%Y-%m-%d")
        usage = staff.setdefault("usage", {})
        entry = usage.get(str(user_id), {})
        if entry.get("date") != today:
            entry = {"date": today, "added": 0}
        try:
            current = max(0, int(entry.get("added", 0)))
        except (TypeError, ValueError, OverflowError):
            logger.warning("Invalid daily quota entry for principal %s", user_id)
            current = 0
        if limit and current + amount > limit:
            return False
        entry["added"] = current + amount
        usage[str(user_id)] = entry
        _write_staff(staff)
        return True


def adjust_daily_usage(user_id: int, amount: int) -> None:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount == 0:
        return
    with _lock:
        staff = _read_staff()
        today = time.strftime("%Y-%m-%d")
        usage = staff.setdefault("usage", {})
        entry = usage.get(str(user_id), {})
        if not isinstance(entry, dict) or entry.get("date") != today:
            entry = {"date": today, "added": 0}
        try:
            current = max(0, int(entry.get("added", 0)))
        except (TypeError, ValueError, OverflowError):
            current = 0
        entry["added"] = max(0, current + amount)
        usage[str(user_id)] = entry
        _write_staff(staff)


def remaining_daily_quota(user_id: int) -> Optional[int]:
    """المتبقي من حد الإضافة اليومي (None = بلا حد)."""
    limit = get_daily_limit(user_id)
    if limit <= 0:
        return None
    return max(0, limit - get_today_usage(user_id))


# ---------------------------------------------------------------------------
# الأكواد
# ---------------------------------------------------------------------------

def generate_code(rank: str, created_by: int, note: str = "") -> Optional[str]:
    """يولد كود تفعيل جديد للرتبة المحددة، مثال: CB-DEV-8F3K9A"""
    if rank not in RANK_LEVELS:
        return None
    alphabet = string.ascii_uppercase + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(6))
    code = f"CB-{rank.upper()}-{body}"
    with _lock:
        staff = _read_staff()
        while code in staff["codes"]:  # منع التكرار النظري
            body = "".join(secrets.choice(alphabet) for _ in range(6))
            code = f"CB-{rank.upper()}-{body}"
        staff["codes"][code] = {
            "rank": rank,
            "created_by": str(created_by),
            "created_at": now_ts(),
            "note": note,
            "used_by": None,
            "used_at": None,
            "revoked": False,
        }
        _write_staff(staff)
    return code


def list_codes() -> Dict[str, Any]:
    with _lock:
        return copy.deepcopy(_read_staff()["codes"])


def list_staff() -> Dict[str, Any]:
    with _lock:
        return copy.deepcopy(_read_staff()["staff"])


def revoke_code(code: str) -> bool:
    """يسحب كودًا (يمنع استخدامه مستقبلًا)."""
    with _lock:
        staff = _read_staff()
        entry = staff["codes"].get(code)
        if not entry:
            return False
        entry["revoked"] = True
        _write_staff(staff)
    return True


def delete_code(code: str) -> bool:
    with _lock:
        staff = _read_staff()
        if code not in staff["codes"]:
            return False
        del staff["codes"][code]
        _write_staff(staff)
    return True


def activate_code(code: str, user_id: int) -> Dict[str, Any]:
    """يفعّل الكود ويربطه بالعضو. يرجع نتيجة العملية."""
    code = (code or "").strip().upper()
    with _lock:
        staff = _read_staff()
        entry = staff["codes"].get(code)
        if not entry:
            return {"ok": False, "message": "الكود غير موجود، تأكد من كتابته بشكل صحيح."}
        if entry.get("revoked"):
            return {"ok": False, "message": "هذا الكود تم سحبه ولا يمكن استخدامه."}
        if entry.get("used_by"):
            return {"ok": False, "message": "هذا الكود تم استخدامه مسبقًا ولا يمكن إعادة استخدامه."}
        # لو العضو عنده رتبة أعلى أو مساوية بالفعل من كود آخر ما ننزل رتبته
        current = staff["staff"].get(str(user_id))
        if current:
            current_level = RANK_LEVELS.get(current.get("rank"), 0)
            new_level = RANK_LEVELS.get(entry["rank"], 0)
            if current_level >= new_level and current.get("rank") in RANK_LEVELS and current.get("code") != code:
                return {
                    "ok": False,
                    "message": f"عندك رتبة **{RANK_NAMES_AR.get(current['rank'])}** بالفعل وما تقدر تاخذ رتبة أقل بنفس الحساب.",
                }
        entry["used_by"] = str(int(user_id))
        entry["used_at"] = now_ts()
        staff["staff"][str(int(user_id))] = {
            "rank": entry["rank"],
            "code": code,
            "activated_at": now_ts(),
            "name": "",
        }
        _write_staff(staff)
    return {"ok": True, "rank": entry["rank"], "message": f"تم تفعيل رتبة {RANK_NAMES_AR.get(entry['rank'])}"}


def set_rank(user_id: int, rank: str, by_id: int) -> bool:
    """تعيين رتبة مباشرة بدون كود (للمالك أو الديفيلوبر)."""
    if rank not in RANK_LEVELS:
        return False
    with _lock:
        staff = _read_staff()
        staff["staff"][str(int(user_id))] = {
            "rank": rank,
            "code": "",
            "activated_at": now_ts(),
            "granted_by": str(int(by_id)),
            "name": "",
        }
        _write_staff(staff)
    return True


def remove_staff(user_id: int) -> bool:
    """إزالة عضو من الفريق (لا تشمل المالك)."""
    if is_owner(user_id):
        return False
    with _lock:
        staff = _read_staff()
        uid = str(int(user_id))
        if uid not in staff["staff"]:
            return False
        del staff["staff"][uid]
        _write_staff(staff)
    return True


# ---------------------------------------------------------------------------
# سجل العمليات (يظهر بالداشبورد مباشرة)
# ---------------------------------------------------------------------------

def load_logs() -> List[Dict[str, Any]]:
    with _lock:
        data = _read_json(LOGS_FILE, [])
        valid_logs = [entry for entry in data if isinstance(entry, dict)]
        if len(valid_logs) != len(data):
            logger.warning("Discarding invalid entries from the staff audit log")
        return copy.deepcopy(valid_logs[-300:])


def log_action(actor_id: int, actor_name: str, action: str, details: str, source: str = "discord") -> bool:
    """Best-effort audit logging; never turns an already-completed action into a failure."""
    try:
        with _lock:
            logs = load_logs()
            logs.append({
                "ts": now_ts(),
                "actor_id": str(actor_id),
                "actor_name": str(actor_name)[:100],
                "action": str(action)[:100],
                "details": str(details)[:1000],
                "source": str(source)[:30],
            })
            logs = logs[-300:]
            _atomic_write(LOGS_FILE, logs)
        return True
    except Exception:
        logger.exception("Could not persist audit log action=%s source=%s", action, source)
        return False


# ---------------------------------------------------------------------------
# فحوصات جاهزة لأوامر البوت
# ---------------------------------------------------------------------------

def rank_check(permission: str):
    """يرجع دالة فحص جاهزة: تقبل العضو إذا كان مالكًا أو يملك الصلاحية
    أو عنده صلاحية Administrator بالديسكورد."""
    from discord.ext import commands

    async def predicate(ctx: commands.Context) -> bool:
        # DashboardContext يمرر نفس permission system بدون تحويل الجلسة إلى Owner.
        if getattr(ctx, "dashboard_session", None) is not None:
            session = ctx.dashboard_session
            if session.get("virtual"):
                return permission in get_permissions_for_rank(session.get("rank", ""))
        if getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.administrator:
            return True
        return has_permission(ctx.author.id, permission)

    return commands.check(predicate)
