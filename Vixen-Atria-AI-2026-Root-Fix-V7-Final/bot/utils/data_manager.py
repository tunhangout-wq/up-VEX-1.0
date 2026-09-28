"""
data_manager.py
----------------
مدير البيانات المركزي للبوت.
يتكفل بقراءة وكتابة ملفات JSON (المستخدمين + الإعدادات) بطريقة آمنة
(Thread-safe / Atomic Write) حتى لا تتلف البيانات إذا حدثت كتابة متزامنة
من البوت ولوحة التحكم بنفس الوقت.
"""

import json
import copy
import logging
import math
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent / "data"
USERS_FILE = BASE_DIR / "users.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
MARKET_FILE = BASE_DIR / "market.json"
LOANS_FILE = BASE_DIR / "loans.json"
FINANCIAL_TXN_FILE = BASE_DIR / "financial_transaction.json"

_lock = threading.RLock()
logger = logging.getLogger(__name__)


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    """يكتب الملف بطريقة ذرية: يكتب بملف مؤقت ثم يستبدل الأصلي.
    هذا يمنع تلف الملف لو انقطع البرنامج أثناء الكتابة."""
    tmp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())

        if path.is_file():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(tmp_path, path)
        tmp_path = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            logger.exception("Could not fsync data directory %s", path.parent)
    except (OSError, TypeError, ValueError):
        logger.exception("Could not atomically write JSON file %s", path)
        raise
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove temporary JSON file %s", tmp_path)


def _quarantine_corrupt_file(path: Path) -> None:
    fd, corrupt_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".corrupt", dir=path.parent
    )
    os.close(fd)
    corrupt_path = Path(corrupt_name)
    try:
        os.replace(path, corrupt_path)
        logger.error("Quarantined invalid JSON file %s as %s", path, corrupt_path)
    except OSError:
        try:
            corrupt_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove quarantine placeholder %s", corrupt_path)
        raise


def _recover_financial_transaction() -> None:
    """Recover a completed/pending users+loans transaction after an interrupted write.

    The journal is intentionally treated as authoritative once it exists. This prevents
    a process crash between the two JSON file replacements from leaving money and loan
    state permanently out of sync. Recovery is idempotent: replaying the same snapshot
    is safe if the process crashed after both files were already written.
    """
    if not FINANCIAL_TXN_FILE.exists():
        return
    try:
        with FINANCIAL_TXN_FILE.open("r", encoding="utf-8") as f:
            journal = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        logger.critical("Financial transaction journal is unreadable: %s", FINANCIAL_TXN_FILE)
        raise RuntimeError("financial transaction journal is unreadable") from exc

    if (
        not isinstance(journal, dict)
        or journal.get("version") != 1
        or not isinstance(journal.get("users"), dict)
        or not isinstance(journal.get("loans"), dict)
        or not isinstance(journal["loans"].get("loans"), list)
        or not isinstance(journal["loans"].get("next_id"), int)
        or isinstance(journal["loans"].get("next_id"), bool)
        or journal["loans"]["next_id"] < 1
    ):
        logger.critical("Financial transaction journal has invalid structure: %s", FINANCIAL_TXN_FILE)
        raise RuntimeError("financial transaction journal has invalid structure")

    users = journal["users"]
    loans = journal["loans"]
    _atomic_write(USERS_FILE, users)
    _atomic_write(LOANS_FILE, loans)
    try:
        FINANCIAL_TXN_FILE.unlink(missing_ok=True)
        directory_fd = os.open(FINANCIAL_TXN_FILE.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # The committed state is already restored. Keeping the journal is safe because
        # recovery is idempotent; do not turn cleanup failure into a false transaction error.
        logger.exception("Recovered financial transaction but could not remove journal %s", FINANCIAL_TXN_FILE)
    logger.warning("Recovered interrupted financial transaction from %s", FINANCIAL_TXN_FILE)


def _read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = copy.deepcopy(default)
        _atomic_write(path, data)
        return data
    except (json.JSONDecodeError, UnicodeDecodeError):
        _quarantine_corrupt_file(path)
        data = copy.deepcopy(default)
        _atomic_write(path, data)
        return data
    except OSError:
        logger.exception("Could not read JSON file %s", path)
        raise

    if not isinstance(data, type(default)):
        _quarantine_corrupt_file(path)
        data = copy.deepcopy(default)
        _atomic_write(path, data)
    return data


def _deep_merge(default: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(default)
    for key, value in data.items():
        if key not in merged:
            merged[key] = copy.deepcopy(value)
            continue
        expected = merged[key]
        if isinstance(expected, dict):
            if isinstance(value, dict):
                merged[key] = _deep_merge(expected, value)
            else:
                logger.warning("Ignoring setting %s with invalid type", key)
        elif isinstance(expected, list):
            if isinstance(value, list):
                merged[key] = copy.deepcopy(value)
            else:
                logger.warning("Ignoring setting %s with invalid type", key)
        elif isinstance(expected, bool):
            if isinstance(value, bool):
                merged[key] = value
            else:
                logger.warning("Ignoring setting %s with invalid type", key)
        elif isinstance(expected, int):
            if isinstance(value, int) and not isinstance(value, bool):
                merged[key] = value
            else:
                logger.warning("Ignoring setting %s with invalid type", key)
        elif isinstance(expected, float):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                merged[key] = float(value)
            else:
                logger.warning("Ignoring setting %s with invalid type", key)
        elif expected is None:
            merged[key] = copy.deepcopy(value)
        elif isinstance(value, type(expected)):
            merged[key] = copy.deepcopy(value)
        else:
            logger.warning("Ignoring setting %s with invalid type", key)
    return merged


# ---------------------------------------------------------------------------
# إدارة المستخدمين
# ---------------------------------------------------------------------------

DEFAULT_USER = {
    "wallet": 0,
    "bank": 0,
    "last_daily": 0,
    "daily_streak": 0,
    "last_work": 0,
    "total_earned": 0,
    "inventory": [],
    "buffs": {},
    # --- نظام الخبرة والمستويات ---
    "xp": 0,
    "level": 0,
    "last_xp": 0,
    "messages": 0,
    # --- الأسهم والاستثمار ---
    "stocks": {},            # {"GOLD": {"qty": 2, "avg_cost": 100}}
    "last_interest": 0,      # آخر سحب فايدة بنكية
    "interest_total": 0,     # إجمالي الفوايد المسحوبة
    # --- السرقة ---
    "last_rob": 0,
    "rob_success": 0,
    "rob_fail": 0,
    "rob_profits": 0,
    # --- الوظائف والبزنس ---
    "job": "",               # id الوظيفة الحالية
    "job_seniority": 1,      # مستوى الأقدمية داخل نفس الوظيفة
    "job_works": 0,          # عدد مرات العمل بعد آخر ترقية
    "total_works": 0,
    "last_job_work": 0,
    "business": None         # {"level": 1, "last_collect": 0} أو None
}


def load_users() -> Dict[str, Any]:
    with _lock:
        _recover_financial_transaction()
        return _read_json(USERS_FILE, {})


def save_users(data: Dict[str, Any]) -> None:
    with _lock:
        _atomic_write(USERS_FILE, data)


def get_user(user_id: int, starting_balance: int = 100) -> Dict[str, Any]:
    """يرجع بيانات مستخدم، وينشئها لو ما كانت موجودة."""
    with _lock:
        users = load_users()
        uid = str(user_id)
        if uid not in users:
            new_user = copy.deepcopy(DEFAULT_USER)
            new_user["wallet"] = starting_balance
            users[uid] = new_user
            save_users(users)
        else:
            # يضمن إن أي حقول جديدة تضاف بالتحديثات المستقبلية تنضاف تلقائيًا
            changed = False
            if not isinstance(users[uid], dict):
                users[uid] = copy.deepcopy(DEFAULT_USER)
                users[uid]["wallet"] = starting_balance
                changed = True
            else:
                for key, value in DEFAULT_USER.items():
                    if key not in users[uid]:
                        users[uid][key] = copy.deepcopy(value)
                        changed = True
            if changed:
                save_users(users)
        return copy.deepcopy(users[uid])


def update_user(user_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
    """يحدث حقول معينة لمستخدم ويحفظ الملف."""
    with _lock:
        users = load_users()
        uid = str(user_id)
        if uid not in users:
            users[uid] = copy.deepcopy(DEFAULT_USER)
        users[uid].update(copy.deepcopy(updates))
        save_users(users)
        return copy.deepcopy(users[uid])


def atomic_update_user(
    user_id: int,
    updater: Callable[[Dict[str, Any]], Any],
    starting_balance: int = 100,
) -> Dict[str, Any]:
    """Apply one read-modify-write user operation under the shared data lock."""
    if not callable(updater):
        raise TypeError("updater must be callable")
    result = atomic_update_users(
        [user_id],
        lambda users: updater(users[str(user_id)]),
        starting_balance,
    )
    return result[str(user_id)]


def atomic_update_users(
    user_ids: List[int],
    updater: Callable[[Dict[str, Dict[str, Any]]], Any],
    starting_balance: int = 100,
) -> Dict[str, Dict[str, Any]]:
    """Apply one transaction to several users and persist them in one file write."""
    if not callable(updater):
        raise TypeError("updater must be callable")
    if not isinstance(user_ids, list) or not user_ids:
        raise ValueError("user_ids must be a non-empty list")
    if any(not isinstance(user_id, int) or isinstance(user_id, bool) for user_id in user_ids):
        raise TypeError("user_ids must contain integers")

    with _lock:
        users = load_users()
        selected = {}
        for uid in {str(user_id) for user_id in user_ids}:
            user = users.get(uid)
            if not isinstance(user, dict):
                user = copy.deepcopy(DEFAULT_USER)
                user["wallet"] = starting_balance
            else:
                user = _deep_merge(DEFAULT_USER, user)
            selected[uid] = user

        original = copy.deepcopy(selected)
        should_save = updater(selected)
        if should_save is False:
            return original
        if any(not isinstance(user, dict) for user in selected.values()):
            raise TypeError("user updates must remain dictionaries")
        users.update(selected)
        save_users(users)
        return copy.deepcopy(selected)


def atomic_update_users_and_loans(
    user_ids: List[int],
    updater: Callable[[Dict[str, Dict[str, Any]], Dict[str, Any]], Any],
    starting_balance: int = 100,
) -> Dict[str, Dict[str, Any]]:
    """Atomically commit related user-balance and loan-state changes.

    The callback receives selected user records and the full loan document. A durable
    journal is written before either file is replaced, so a crash between replacements
    can be recovered deterministically on the next read/startup.
    """
    if not callable(updater):
        raise TypeError("updater must be callable")
    if not isinstance(user_ids, list) or not user_ids:
        raise ValueError("user_ids must be a non-empty list")
    if any(not isinstance(user_id, int) or isinstance(user_id, bool) for user_id in user_ids):
        raise TypeError("user_ids must contain integers")

    with _lock:
        _recover_financial_transaction()
        users = load_users()
        loans = load_loans()
        if not isinstance(loans, dict) or not isinstance(loans.get("loans"), list):
            raise ValueError("invalid loan data")
        if not isinstance(loans.get("next_id"), int) or isinstance(loans.get("next_id"), bool) or loans["next_id"] < 1:
            raise ValueError("invalid loan data")

        selected: Dict[str, Dict[str, Any]] = {}
        for uid in {str(user_id) for user_id in user_ids}:
            user = users.get(uid)
            if not isinstance(user, dict):
                user = copy.deepcopy(DEFAULT_USER)
                user["wallet"] = starting_balance
            else:
                user = _deep_merge(DEFAULT_USER, user)
            selected[uid] = user

        original_users = copy.deepcopy(selected)
        should_save = updater(selected, loans)
        if should_save is False:
            return original_users
        if any(not isinstance(user, dict) for user in selected.values()):
            raise TypeError("user updates must remain dictionaries")
        if not isinstance(loans.get("loans"), list) or not isinstance(loans.get("next_id"), int) or isinstance(loans.get("next_id"), bool) or loans["next_id"] < 1:
            raise ValueError("loan update produced invalid data")

        users.update(selected)
        journal = {
            "version": 1,
            "transaction_id": str(time.time_ns()),
            "users": users,
            "loans": loans,
        }
        try:
            _atomic_write(FINANCIAL_TXN_FILE, journal)
            _atomic_write(USERS_FILE, users)
            _atomic_write(LOANS_FILE, loans)
            try:
                FINANCIAL_TXN_FILE.unlink(missing_ok=True)
                directory_fd = os.open(FINANCIAL_TXN_FILE.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Both data files are already committed. Retaining the journal is safe
                # because startup/read recovery replays the same snapshot idempotently.
                logger.exception("Financial transaction committed but journal cleanup failed")
        except Exception:
            # Keep the journal on disk. The next state read will deterministically
            # replay the intended snapshot rather than guessing which file won.
            logger.exception("Financial transaction commit interrupted; journal retained for recovery")
            raise
        return copy.deepcopy(selected)


def add_wallet(user_id: int, amount: int, max_wallet: int = 1_000_000) -> int:
    """يضيف (أو يطرح إذا كان الرقم سالب) من محفظة المستخدم، مع احترام الحد الأقصى."""
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise TypeError("wallet amount must be an integer")
    if not isinstance(max_wallet, int) or isinstance(max_wallet, bool) or max_wallet < 0:
        raise ValueError("max_wallet must be a non-negative integer")
    result = {}

    def apply(user):
        wallet = max(0, int(user.get("wallet", 0)))
        earned = max(0, int(user.get("total_earned", 0)))
        new_balance = max(0, min(wallet + amount, max_wallet))
        actual_change = new_balance - wallet
        user["wallet"] = new_balance
        if actual_change > 0:
            user["total_earned"] = earned + actual_change
        result["balance"] = new_balance

    atomic_update_user(user_id, apply)
    return result["balance"]


def add_bank(user_id: int, amount: int, max_bank: int = 5_000_000) -> int:
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise TypeError("bank amount must be an integer")
    if not isinstance(max_bank, int) or isinstance(max_bank, bool) or max_bank < 0:
        raise ValueError("max_bank must be a non-negative integer")
    result = {}

    def apply(user):
        bank = max(0, int(user.get("bank", 0)))
        user["bank"] = max(0, min(bank + amount, max_bank))
        result["balance"] = user["bank"]

    atomic_update_user(user_id, apply)
    return result["balance"]


def get_leaderboard(limit: int = 10):
    """يرجع أغنى المستخدمين مرتبين حسب (محفظة + بنك)."""
    with _lock:
        users = load_users()
        ranked = sorted(
            users.items(),
            key=lambda kv: kv[1].get("wallet", 0) + kv[1].get("bank", 0),
            reverse=True
        )
        return ranked[:limit]


def get_levels_leaderboard(limit: int = 10):
    """يرجع أعلى المستخدمين مستوى وخبرة."""
    with _lock:
        users = load_users()
        ranked = sorted(
            users.items(),
            key=lambda kv: (kv[1].get("level", 0), kv[1].get("xp", 0)),
            reverse=True
        )
        return ranked[:limit]


# ---------------------------------------------------------------------------
# السوق (الأسهم)
# ---------------------------------------------------------------------------

def load_market() -> Dict[str, Any]:
    with _lock:
        return _read_json(MARKET_FILE, {"prices": {}, "last_update": 0})


def save_market(data: Dict[str, Any]) -> None:
    with _lock:
        _atomic_write(MARKET_FILE, data)


# ---------------------------------------------------------------------------
# القروض
# ---------------------------------------------------------------------------

def load_loans() -> Dict[str, Any]:
    with _lock:
        _recover_financial_transaction()
        return _read_json(LOANS_FILE, {"next_id": 1, "loans": []})


def save_loans(data: Dict[str, Any]) -> None:
    with _lock:
        _atomic_write(LOANS_FILE, data)


def atomic_update_loans(updater: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
    """Apply a read-modify-write loan-file operation while holding the shared lock."""
    if not callable(updater):
        raise TypeError("updater must be callable")
    with _lock:
        data = _read_json(LOANS_FILE, {"next_id": 1, "loans": []})
        if (
            not isinstance(data.get("next_id"), int) or isinstance(data.get("next_id"), bool)
            or data["next_id"] < 1 or not isinstance(data.get("loans"), list)
        ):
            logger.error("Invalid loan file structure; refusing to update %s", LOANS_FILE)
            raise ValueError("invalid loan data")
        original = copy.deepcopy(data)
        if updater(data) is False:
            return original
        if (
            not isinstance(data.get("next_id"), int) or isinstance(data.get("next_id"), bool)
            or data["next_id"] < 1 or not isinstance(data.get("loans"), list)
        ):
            raise ValueError("loan update produced invalid data")
        _atomic_write(LOANS_FILE, data)
        return copy.deepcopy(data)


# ---------------------------------------------------------------------------
# إدارة الإعدادات (تُقرأ من لوحة التحكم أيضًا)
# ---------------------------------------------------------------------------

def _normalize_ai_settings(raw: Dict[str, Any], merged: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate the old `atria` settings into the live `ai` policy without losing modern settings."""
    legacy = raw.get("atria") if isinstance(raw, dict) else {}
    if not isinstance(legacy, dict):
        legacy = {}
    ai = merged.setdefault("ai", {})
    if not isinstance(ai, dict):
        ai = {}
        merged["ai"] = ai

    moderation = ai.setdefault("moderation", {})
    if not isinstance(moderation, dict):
        moderation = {}
        ai["moderation"] = moderation
    # Only migrate legacy values when the modern section was not explicitly present.
    raw_ai = raw.get("ai") if isinstance(raw, dict) else {}
    raw_ai = raw_ai if isinstance(raw_ai, dict) else {}
    if not isinstance(raw_ai.get("moderation"), dict):
        if "moderation_enabled" in legacy:
            moderation["enabled"] = bool(legacy.get("moderation_enabled"))
        if "timeout_minutes" in legacy:
            try:
                moderation["timeout_minutes"] = max(1, min(60, int(legacy.get("timeout_minutes"))))
            except (TypeError, ValueError, OverflowError):
                pass

    chat = ai.setdefault("chat", {})
    if not isinstance(chat, dict):
        chat = {}
        ai["chat"] = chat
    # Old installations had no chat policy. Keep it safely disabled until configured.
    chat.setdefault("enabled", False)
    chat.setdefault("streaming", True)
    chat.setdefault("max_output_tokens", 65536)
    chat.setdefault("context_messages", 100)
    chat.setdefault("speed_mode", "flash")
    chat.setdefault("smart_context_enabled", True)
    chat.setdefault("intent_router_enabled", True)
    chat.setdefault("flash_path_enabled", True)
    chat.setdefault("quality_guard_enabled", True)
    chat.setdefault("owner_actions_enabled", True)
    return merged


def load_settings() -> Dict[str, Any]:
    """يقرأ الإعدادات ويدمجها مع الافتراضيات، مع ترحيل إعدادات AI القديمة."""
    with _lock:
        data = _read_json(SETTINGS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    merged = _deep_merge(DEFAULT_SETTINGS, data)
    return _normalize_ai_settings(data, merged)


def save_settings(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise TypeError("settings must be a dictionary")
    with _lock:
        normalized = _normalize_ai_settings(data, _deep_merge(DEFAULT_SETTINGS, data))
        ai_mod = normalized.get("ai", {}).get("moderation", {})
        legacy = normalized.setdefault("atria", {})
        if isinstance(legacy, dict) and isinstance(ai_mod, dict):
            legacy["moderation_enabled"] = bool(ai_mod.get("enabled", False))
            legacy["timeout_minutes"] = int(ai_mod.get("timeout_minutes", 10))
        _atomic_write(SETTINGS_FILE, normalized)


def atomic_update_settings(updater: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
    """Apply one read-modify-write settings operation under the shared lock."""
    if not callable(updater):
        raise TypeError("updater must be callable")
    with _lock:
        data = _deep_merge(DEFAULT_SETTINGS, _read_json(SETTINGS_FILE, {}))
        original = copy.deepcopy(data)
        if updater(data) is False:
            return original
        if not isinstance(data, dict):
            raise TypeError("settings update must remain a dictionary")
        normalized = _normalize_ai_settings(data, _deep_merge(DEFAULT_SETTINGS, data))
        ai_mod = normalized.get("ai", {}).get("moderation", {})
        legacy = normalized.setdefault("atria", {})
        if isinstance(legacy, dict) and isinstance(ai_mod, dict):
            legacy["moderation_enabled"] = bool(ai_mod.get("enabled", False))
            legacy["timeout_minutes"] = int(ai_mod.get("timeout_minutes", 10))
        _atomic_write(SETTINGS_FILE, normalized)
        return copy.deepcopy(normalized)


# الإعدادات الافتراضية (تُدمج تلقائيًا لو نقص أي قسم من الملف)
DEFAULT_SETTINGS: Dict[str, Any] = {
    "bot": {
        "name": "Vixen EDR",
        "prefix": "!",
        "currency_name": "عملة",
        "currency_symbol": "🪙",
        "embed_color": "#F5A623",
        "success_color": "#2ECC71",
        "error_color": "#E74C3C",
    },
    "economy": {
        "starting_balance": 100,
        "max_wallet": 1000000,
        "max_bank": 5000000,
        "daily_min": 200,
        "daily_max": 500,
        "daily_streak_bonus": 25,
        "daily_streak_cap": 500,
        "work_min": 50,
        "work_max": 300,
        "work_cooldown_minutes": 60,
        "transfer_tax_percent": 2,
        "transfer_min_amount": 10,
    },
    "gambling": {
        "slots_min_bet": 10,
        "slots_max_bet": 5000,
        "slots_win_multiplier": 3,
        "slots_jackpot_multiplier": 10,
        "blackjack_min_bet": 10,
        "blackjack_max_bet": 10000,
        "coinflip_min_bet": 10,
        "coinflip_max_bet": 5000,
        "gambling_enabled": True,
    },
    "shop": {
        "items": [
            {"id": "vip_role", "name": "VIP", "description": "رتبة VIP مميزة داخل السيرفر", "price": 5000, "type": "role", "role_id": "", "emoji": "👑"},
            {"id": "color_role", "name": "لون مخصص", "description": "رتبة لون خاص باسمك", "price": 2000, "type": "role", "role_id": "", "emoji": "🎨"},
            {"id": "lucky_charm", "name": "تعويذة الحظ", "description": "تزيد فرصة الربح في القمار لمدة ساعة", "price": 1500, "type": "item", "role_id": "", "emoji": "🍀"},
        ]
    },
    "roles": {"level_roles": [], "autorole_enabled": False, "autorole_id": ""},
    "channels": {"log_channel_id": "", "welcome_channel_id": ""},
    "leaderboard": {"show_top": 10},
    "ai": {
        "moderation": {
            "enabled": False,
            "kill_switch": False,
            "channels": [],
            "detection_types": ["spam", "flood", "repeated_messages", "toxicity", "harassment"],
            "sensitivity": "medium",
            "automatic_actions": ["warn", "timeout"],
            "allow_ai_ban": False,
            "max_actions_per_hour": 20,
            "timeout_minutes": 10,
            "fast_path_enabled": True,
            "ai_timeout_seconds": 6.0,
            "window_seconds": 8,
            "repeat_threshold": 5,
            "flood_threshold": 8,
            "mention_threshold": 8,
            "mod_log_channel": "",
            "whitelist_users": [],
            "whitelist_roles": [],
            "delete_offending_messages": True,
            "allow_server_invites": False,
            "raid_window_seconds": 20,
            "raid_join_threshold": 8,
            "escalation_timeout_short": 5,
            "escalation_timeout_long": 15,
        },
        "chat": {
            "enabled": False,
            "allowed_channels": [],
            "ignored_channels": [],
            "response_mode": "every_message",
            "language": "tunisian",
            "tunisian_mode": True,
            "context_messages": 100,
            "retention_days": 30,
            "name": "Vixen",
            "tone": "smart, sharp, playful Tunisian with dry sarcasm and controlled roast energy",
            "style": "professional, natural Tunisian Discord chat with strong context and clean direct answers",
            "system_prompt": "",
            "streaming": True,
            "max_output_tokens": 65536,
            "speed_mode": "flash",
            "smart_context_enabled": True,
            "intent_router_enabled": True,
            "flash_path_enabled": True,
            "quality_guard_enabled": True,
            "owner_actions_enabled": True,
            "user_cooldown_seconds": 0,
            "channel_cooldown_seconds": 0,
            "global_cooldown_seconds": 0,
        },
    },
    "atria": {"enabled": True, "moderation_enabled": False, "moderation_prefixes": ["[mod]", "!modcheck"], "moderation_mode": "all", "timeout_minutes": 10},
    "dashboard": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8080,
        
    },
    "ranks": {
        "owner_id": "",
        "permissions": {},
        "daily_limits": {},
    },
    "levels": {
        "enabled": True,
        "xp_min": 3,
        "xp_max": 8,
        "xp_cooldown": 60,
        "level_up_reward": 100,
        "announce": True,
        "level_roles": [],
    },
    "bank_interest": {
        "enabled": True,
        "daily_rate": 2.0,
        "max_interest": 5000,
        "min_bank": 100,
    },
    "stocks": {
        "enabled": True,
        "update_interval_min": 5,
        "volatility": 12,
        "symbols": [
            {"symbol": "GOLD", "name": "الذهب", "emoji": "🥇", "base_price": 150},
            {"symbol": "OIL", "name": "النفط", "emoji": "🛢️", "base_price": 90},
            {"symbol": "TECH", "name": "التكنولوجيا", "emoji": "💻", "base_price": 250},
            {"symbol": "MEME", "name": "ميم كوين", "emoji": "🐸", "base_price": 25},
            {"symbol": "ROCKET", "name": "صاروخ للاستثمار", "emoji": "🚀", "base_price": 500},
        ]
    },
    "loans": {
        "enabled": True,
        "min_amount": 100,
        "max_amount": 50000,
        "default_interest": 10,
        "max_duration_days": 7,
        "max_active": 2,
    },
    "rob": {
        "enabled": True,
        "success_chance": 45,
        "fine_percent": 25,
        "cooldown_minutes": 30,
        "min_victim_wallet": 100,
        "max_steal_percent": 40,
        "fine_to_victim": True,
    },
    "career": {
        "enabled": True,
        "work_cooldown_minutes": 45,
        "promote_after": 10,
        "salary_seniority_bonus": 10,
        "jobs": [
            {"id": "delivery", "name": "موظف توصيل", "emoji": "🛵", "tier": 1, "salary": [80, 150], "req_level": 0},
            {"id": "cashier", "name": "كاشير", "emoji": "🧾", "tier": 2, "salary": [150, 260], "req_level": 3},
            {"id": "agent", "name": "موظف مبيعات", "emoji": "📊", "tier": 3, "salary": [280, 450], "req_level": 6},
            {"id": "manager", "name": "مدير فرع", "emoji": "💼", "tier": 4, "salary": [500, 800], "req_level": 10},
            {"id": "ceo", "name": "المدير التنفيذي", "emoji": "👔", "tier": 5, "salary": [900, 1500], "req_level": 15},
        ],
        "business": {
            "start_cost": 10000,
            "daily_min": 100,
            "daily_max": 400,
            "upgrade_cost": 5000,
            "upgrade_multiplier": 1.3,
            "max_level": 10,
        },
    },
}


def get_setting(path: str, default: Any = None) -> Any:
    """يقرأ إعداد باستخدام مسار منقط، مثال: get_setting('economy.daily_min')"""
    settings = load_settings()
    node = settings
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def now_ts() -> int:
    return int(time.time())


# ---------------------------------------------------------------------------
# إحصائيات عامة (تستخدمها لوحة التحكم مباشرة)
# ---------------------------------------------------------------------------

def get_economy_stats() -> Dict[str, Any]:
    """إحصائيات مالية شاملة لكل السيرفر."""
    with _lock:
        users = load_users()
        total_users = len(users)
        total_wallet = sum(u.get("wallet", 0) for u in users.values())
        total_bank = sum(u.get("bank", 0) for u in users.values())
        total_earned = sum(u.get("total_earned", 0) for u in users.values())
        total_xp = sum(u.get("xp", 0) for u in users.values())
        return {
            "total_users": total_users,
            "total_wallet": total_wallet,
            "total_bank": total_bank,
            "total_money": total_wallet + total_bank,
            "total_earned": total_earned,
            "total_xp": total_xp,
        }
