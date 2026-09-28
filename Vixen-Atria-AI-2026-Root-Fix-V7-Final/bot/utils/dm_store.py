"""Persistent storage for Dashboard DM templates, history, quotas and bulk jobs."""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "vixen_ai.sqlite3"


class DMStore:
    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=8)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=8000")
        if not self._initialized:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS dm_templates (
                    template_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    embed_json TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT 'General',
                    favorite INTEGER NOT NULL DEFAULT 0,
                    creator_id INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(guild_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_dm_templates_guild_updated
                    ON dm_templates(guild_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS dm_history (
                    dm_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    recipient_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    template_id INTEGER,
                    status TEXT NOT NULL,
                    error TEXT,
                    embed_json TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    timestamp REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dm_history_guild_time
                    ON dm_history(guild_id, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_dm_history_guild_status_time
                    ON dm_history(guild_id, status, timestamp DESC);
                CREATE TABLE IF NOT EXISTS dm_send_reservations (
                    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER NOT NULL,
                    timestamp REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dm_send_reservations
                    ON dm_send_reservations(sender_id, timestamp);
                CREATE TABLE IF NOT EXISTS dm_bulk_jobs (
                    job_id TEXT PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_name TEXT NOT NULL,
                    template_id INTEGER,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    embed_json TEXT,
                    options_json TEXT NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    attempted INTEGER NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    cancelled INTEGER NOT NULL DEFAULT 0,
                    uncertain INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_dm_bulk_jobs_guild_time
                    ON dm_bulk_jobs(guild_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_dm_bulk_jobs_status
                    ON dm_bulk_jobs(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS dm_bulk_items (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    recipient_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    dm_id INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    attempted_at REAL,
                    finished_at REAL,
                    UNIQUE(job_id, recipient_id)
                );
                CREATE INDEX IF NOT EXISTS idx_dm_bulk_items_job_status
                    ON dm_bulk_items(job_id, status, item_id);
                """
            )
            self._migrate_schema(connection)
            self._initialized = True
        return connection

    @staticmethod
    def _migrate_schema(connection):
        """Upgrade databases created by older Vixen releases without destructive changes."""
        for table, columns in {
            "dm_templates": {
                "content": "TEXT NOT NULL DEFAULT ''",
                "description": "TEXT NOT NULL DEFAULT ''",
                "category": "TEXT NOT NULL DEFAULT 'General'",
                "favorite": "INTEGER NOT NULL DEFAULT 0",
            },
            "dm_history": {"content": "TEXT NOT NULL DEFAULT ''"},
        }.items():
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_template(self, guild_id, name, embed, creator_id, content="", description="", category="General", favorite=False):
        now = time.time()
        data = json.dumps(embed or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO dm_templates(
                    guild_id, name, embed_json, content, description, category, favorite,
                    creator_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(guild_id), str(name).strip()[:100], data, str(content or "")[:2000],
                    str(description or "").strip()[:200], str(category or "General").strip()[:40],
                    1 if favorite else 0, int(creator_id), now, now,
                ),
            )
            return cursor.lastrowid

    def update_template(self, guild_id, template_id, name, embed, content=None, description=None, category=None, favorite=None):
        current = self.get_template(guild_id, template_id)
        if current is None:
            return False
        content = current.get("content", "") if content is None else str(content)
        description = current.get("description", "") if description is None else str(description)
        category = current.get("category", "General") if category is None else str(category)
        favorite = current.get("favorite", False) if favorite is None else bool(favorite)
        data = json.dumps(embed or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """UPDATE dm_templates SET name = ?, embed_json = ?, content = ?, description = ?,
                   category = ?, favorite = ?, updated_at = ? WHERE guild_id = ? AND template_id = ?""",
                (
                    str(name).strip()[:100], data, content[:2000], description.strip()[:200],
                    category.strip()[:40] or "General", 1 if favorite else 0, time.time(),
                    int(guild_id), int(template_id),
                ),
            )
            return cursor.rowcount == 1

    def set_template_favorite(self, guild_id, template_id, favorite):
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE dm_templates SET favorite = ?, updated_at = ? WHERE guild_id = ? AND template_id = ?",
                (1 if favorite else 0, time.time(), int(guild_id), int(template_id)),
            )
            return cursor.rowcount == 1

    def duplicate_template(self, guild_id, template_id, name, creator_id):
        source = self.get_template(guild_id, template_id)
        if source is None:
            return None
        return self.create_template(
            guild_id, name, source["embed"], creator_id,
            content=source.get("content", ""),
            description=source.get("description", ""),
            category=source.get("category", "General"),
            favorite=False,
        )

    def delete_template(self, guild_id, template_id):
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM dm_templates WHERE guild_id = ? AND template_id = ?",
                (int(guild_id), int(template_id)),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _template(row):
        result = dict(row)
        result["embed"] = json.loads(result.pop("embed_json"))
        result["favorite"] = bool(result.get("favorite", 0))
        return result

    def get_template(self, guild_id, template_id):
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM dm_templates WHERE guild_id = ? AND template_id = ?",
                (int(guild_id), int(template_id)),
            ).fetchone()
        return self._template(row) if row else None

    def list_templates(self, guild_id, query="", category="", favorites_only=False):
        query = str(query or "").strip().casefold()[:100]
        category = str(category or "").strip()[:40]
        sql = "SELECT * FROM dm_templates WHERE guild_id = ?"
        args = [int(guild_id)]
        if query:
            sql += " AND (LOWER(name) LIKE ? OR LOWER(description) LIKE ? OR LOWER(category) LIKE ?)"
            like = f"%{query}%"
            args.extend([like, like, like])
        if category:
            sql += " AND category = ?"
            args.append(category)
        if favorites_only:
            sql += " AND favorite = 1"
        sql += " ORDER BY favorite DESC, name COLLATE NOCASE"
        with self._lock, self._connection() as connection:
            rows = connection.execute(sql, tuple(args)).fetchall()
        return [self._template(row) for row in rows]

    def record_send(self, guild_id, recipient_id, sender_id, template_id, status, error, embed, content=""):
        data = json.dumps(embed or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO dm_history(
                    guild_id, recipient_id, sender_id, template_id, status, error, embed_json, content, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(guild_id), int(recipient_id), int(sender_id),
                    int(template_id) if template_id is not None else None,
                    str(status)[:30], str(error or "")[:500], data, str(content or "")[:2000], time.time(),
                ),
            )
            return cursor.lastrowid

    def recent_history(self, guild_id, limit=100, status="", query=""):
        limit = max(1, min(500, int(limit)))
        sql = """SELECT dm_id, guild_id, recipient_id, sender_id, template_id, status, error, content, timestamp
                 FROM dm_history WHERE guild_id = ?"""
        args = [int(guild_id)]
        if status:
            sql += " AND status = ?"
            args.append(str(status)[:30])
        if query:
            q = str(query).strip()[:100]
            sql += " AND (CAST(recipient_id AS TEXT) LIKE ? OR CAST(sender_id AS TEXT) LIKE ?)"
            like = f"%{q}%"
            args.extend([like, like])
        sql += " ORDER BY dm_id DESC LIMIT ?"
        args.append(limit)
        with self._lock, self._connection() as connection:
            rows = connection.execute(sql, tuple(args)).fetchall()
        return [dict(row) for row in rows]

    def reserve_send(self, sender_id, maximum=25, now=None):
        limit = max(1, min(100, int(maximum)))
        current = time.time() if now is None else float(now)
        cutoff = current - 3600
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM dm_send_reservations WHERE timestamp <= ?", (cutoff,))
            count = connection.execute(
                "SELECT COUNT(*) FROM dm_send_reservations WHERE sender_id = ? AND timestamp > ?",
                (int(sender_id), cutoff),
            ).fetchone()[0]
            if count >= limit:
                return False
            connection.execute(
                "INSERT INTO dm_send_reservations(sender_id, timestamp) VALUES (?, ?)",
                (int(sender_id), current),
            )
            return True

    # ------------------------------------------------------------------
    # Bulk DM campaigns
    # ------------------------------------------------------------------

    def create_bulk_job(self, job_id, guild_id, sender_id, sender_name, template_id, reason, content, embed, options, recipient_ids):
        now = time.time()
        embed_json = json.dumps(embed or {}, ensure_ascii=False, separators=(",", ":")) if embed else None
        recipient_ids = [int(value) for value in recipient_ids]
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO dm_bulk_jobs(
                    job_id, guild_id, sender_id, sender_name, template_id, status, reason,
                    content, embed_json, options_json, total, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(job_id), int(guild_id), int(sender_id), str(sender_name or "Dashboard")[:100],
                    int(template_id) if template_id is not None else None, str(reason or "")[:1000],
                    str(content or "")[:2000], embed_json,
                    json.dumps(options or {}, ensure_ascii=False, separators=(",", ":")),
                    len(recipient_ids), now, now,
                ),
            )
            connection.executemany(
                "INSERT INTO dm_bulk_items(job_id, recipient_id) VALUES (?, ?)",
                [(str(job_id), member_id) for member_id in recipient_ids],
            )
        return str(job_id)

    @staticmethod
    def _bulk_job(row):
        if not row:
            return None
        result = dict(row)
        try:
            result["options"] = json.loads(result.pop("options_json"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result["options"] = {}
        try:
            result["embed"] = json.loads(result.pop("embed_json")) if result.get("embed_json") else None
        except (TypeError, ValueError, json.JSONDecodeError):
            result["embed"] = None
        result.pop("embed_json", None)
        return result

    def get_bulk_job(self, job_id):
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM dm_bulk_jobs WHERE job_id = ?", (str(job_id),)).fetchone()
        return self._bulk_job(row)

    def list_bulk_jobs(self, guild_id, limit=30):
        limit = max(1, min(100, int(limit)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM dm_bulk_jobs WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
                (int(guild_id), limit),
            ).fetchall()
        return [self._bulk_job(row) for row in rows]

    def set_bulk_job_status(self, job_id, status, last_error=None):
        allowed = {"queued", "running", "paused", "cancel_requested", "cancelled", "completed", "failed", "interrupted"}
        if status not in allowed:
            raise ValueError("invalid bulk job status")
        now = time.time()
        with self._lock, self._connection() as connection:
            if status == "running":
                cursor = connection.execute(
                    "UPDATE dm_bulk_jobs SET status=?, started_at=COALESCE(started_at, ?), updated_at=? WHERE job_id=?",
                    (status, now, now, str(job_id)),
                )
            elif status in {"completed", "failed", "cancelled", "interrupted"}:
                cursor = connection.execute(
                    "UPDATE dm_bulk_jobs SET status=?, finished_at=?, updated_at=?, last_error=COALESCE(?, last_error) WHERE job_id=?",
                    (status, now, now, last_error, str(job_id)),
                )
            else:
                cursor = connection.execute(
                    "UPDATE dm_bulk_jobs SET status=?, updated_at=?, last_error=COALESCE(?, last_error) WHERE job_id=?",
                    (status, now, last_error, str(job_id)),
                )
            return cursor.rowcount == 1

    def increment_bulk_result(self, job_id, outcome, error=""):
        field = {
            "success": "success",
            "failed": "failed",
            "cancelled": "cancelled",
            "uncertain": "uncertain",
        }.get(str(outcome))
        if field is None:
            raise ValueError("invalid bulk result")
        with self._lock, self._connection() as connection:
            connection.execute(
                f"UPDATE dm_bulk_jobs SET attempted = attempted + 1, {field} = {field} + 1, last_error = ?, updated_at = ? WHERE job_id = ?",
                (str(error or "")[:500], time.time(), str(job_id)),
            )

    def claim_next_bulk_item(self, job_id):
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT item_id, recipient_id FROM dm_bulk_items WHERE job_id=? AND status='pending' ORDER BY item_id LIMIT 1",
                (str(job_id),),
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                "UPDATE dm_bulk_items SET status='sending', attempted_at=? WHERE item_id=? AND status='pending'",
                (time.time(), row["item_id"]),
            )
            if updated.rowcount != 1:
                return None
            return dict(row)

    def set_bulk_item_result(self, item_id, status, error="", dm_id=None):
        if status not in {"success", "failed", "cancelled", "uncertain", "skipped"}:
            raise ValueError("invalid bulk item status")
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE dm_bulk_items SET status=?, dm_id=?, error=?, finished_at=? WHERE item_id=?",
                (status, dm_id, str(error or "")[:500], time.time(), int(item_id)),
            )
            return cursor.rowcount == 1

    def cancel_pending_bulk_items(self, job_id):
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE dm_bulk_items SET status='cancelled', error='Cancelled before send', finished_at=? WHERE job_id=? AND status='pending'",
                (time.time(), str(job_id)),
            )
            count = cursor.rowcount
            if count:
                connection.execute(
                    "UPDATE dm_bulk_jobs SET cancelled = cancelled + ?, updated_at=? WHERE job_id=?",
                    (count, time.time(), str(job_id)),
                )
            return count

    def bulk_items(self, job_id, limit=100):
        limit = max(1, min(500, int(limit)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT item_id, recipient_id, status, dm_id, error, attempted_at, finished_at
                   FROM dm_bulk_items WHERE job_id=? ORDER BY item_id LIMIT ?""",
                (str(job_id), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def interrupt_bulk_job(self, job_id, error="Dashboard worker stopped before campaign completion."):
        """Finalize an active send as uncertain before marking the campaign interrupted.

        A Discord request can be accepted remotely just before the local process is
        cancelled. Retrying that recipient blindly could therefore create a duplicate.
        ``uncertain`` is a deliberate safety state that blocks automatic resume.
        """
        now = time.time()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """UPDATE dm_bulk_items
                   SET status='uncertain', error=?, finished_at=?
                   WHERE job_id=? AND status='sending'""",
                (str(error or "")[:500], now, str(job_id)),
            )
            uncertain_added = max(0, cursor.rowcount)
            connection.execute(
                """UPDATE dm_bulk_jobs
                   SET status='interrupted', finished_at=?, updated_at=?,
                       attempted=attempted+?, uncertain=uncertain+?, last_error=?
                   WHERE job_id=? AND status IN ('queued','running','cancel_requested','paused')""",
                (now, now, uncertain_added, uncertain_added, str(error or "")[:500], str(job_id)),
            )
            return uncertain_added

    def mark_running_jobs_interrupted(self):
        now = time.time()
        message = "Process restarted while campaign was active; review before resuming."
        with self._lock, self._connection() as connection:
            jobs = connection.execute(
                "SELECT job_id FROM dm_bulk_jobs WHERE status IN ('queued','running','cancel_requested')"
            ).fetchall()
            for row in jobs:
                job_id = str(row["job_id"])
                cursor = connection.execute(
                    """UPDATE dm_bulk_items
                       SET status='uncertain', error=?, finished_at=?
                       WHERE job_id=? AND status='sending'""",
                    (message, now, job_id),
                )
                uncertain_added = max(0, cursor.rowcount)
                connection.execute(
                    """UPDATE dm_bulk_jobs SET status='interrupted', finished_at=?, updated_at=?,
                       attempted=attempted+?, uncertain=uncertain+?, last_error=? WHERE job_id=?""",
                    (now, now, uncertain_added, uncertain_added, message, job_id),
                )


dm_store = DMStore()
