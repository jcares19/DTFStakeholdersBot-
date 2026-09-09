"""
Database layer for the DTF Stakeholders League bot.

SQLite for v1. All access goes through this module so it can be swapped
for Postgres later without touching bot logic.
"""

import sqlite3
import time
from contextlib import contextmanager

from config import DATABASE_PATH, DEFAULT_SETTINGS


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    wallet_address TEXT,
    twitter_handle TEXT,
    registered INTEGER NOT NULL DEFAULT 0,
    registered_at INTEGER,
    first_seen INTEGER NOT NULL,
    total_points INTEGER NOT NULL DEFAULT 0,
    monthly_points INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS point_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    tier TEXT,
    points INTEGER NOT NULL,
    note TEXT,
    awarded_by INTEGER,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled | open | closed
    chat_id INTEGER,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    winner_id INTEGER,
    created_at INTEGER NOT NULL,
    closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id INTEGER NOT NULL,
    telegram_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    created_at INTEGER NOT NULL,
    FOREIGN KEY (challenge_id) REFERENCES challenges(id),
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS monthly_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    username TEXT,
    points INTEGER NOT NULL,
    period_label TEXT NOT NULL,
    archived_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    wallet_address TEXT,
    amount_dtf REAL NOT NULL,
    cycle TEXT NOT NULL,          -- weekly | monthly
    period_label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | finalized
    note TEXT,
    created_at INTEGER NOT NULL,
    finalized_at INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Safe migration for installs created before twitter_handle existed —
        # harmless no-op if the column is already there.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN twitter_handle TEXT")
        except sqlite3.OperationalError:
            pass
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


# --- settings ---------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )


# --- users / registration ---------------------------------------------

def upsert_user(telegram_id: int, username: str, display_name: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, display_name, first_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name
            """,
            (telegram_id, username, display_name, int(time.time())),
        )


def register_wallet(telegram_id: int, wallet_address: str, twitter_handle: str = ""):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET wallet_address = ?, twitter_handle = ?, registered = 1, registered_at = ?
            WHERE telegram_id = ?
            """,
            (wallet_address, twitter_handle, int(time.time()), telegram_id),
        )


def find_user_by_username(username: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()


def get_user(telegram_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()


def get_registered_users():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE registered = 1"
        ).fetchall()


def get_registered_count() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM users WHERE registered = 1"
        ).fetchone()
        return row["n"]


def get_submission_count(challenge_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM submissions WHERE challenge_id = ?",
            (challenge_id,),
        ).fetchone()
        return row["n"]


# --- points -------------------------------------------------------------

def award_points(telegram_id: int, category: str, tier: str, points: int,
                  awarded_by: int, note: str = ""):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO point_events (telegram_id, category, tier, points, note, awarded_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, category, tier, points, note, awarded_by, int(time.time())),
        )
        conn.execute(
            """
            UPDATE users
            SET total_points = total_points + ?,
                monthly_points = monthly_points + ?
            WHERE telegram_id = ?
            """,
            (points, points, telegram_id),
        )


def get_points_today_for_category(telegram_id: int, category: str) -> int:
    day_start = int(time.time()) - 86400
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(points), 0) as total
            FROM point_events
            WHERE telegram_id = ? AND category = ? AND created_at >= ?
            """,
            (telegram_id, category, day_start),
        ).fetchone()
        return row["total"]


def get_user_breakdown(telegram_id: int):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT category, SUM(points) as total, COUNT(*) as events
            FROM point_events
            WHERE telegram_id = ?
            GROUP BY category
            ORDER BY total DESC
            """,
            (telegram_id,),
        ).fetchall()


def get_leaderboard(monthly: bool, limit: int):
    col = "monthly_points" if monthly else "total_points"
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT telegram_id, username, display_name, registered, {col} as points
            FROM users
            WHERE {col} > 0
            ORDER BY {col} DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def reset_month(period_label: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT telegram_id, username, monthly_points FROM users WHERE monthly_points > 0"
        ).fetchall()
        now = int(time.time())
        for r in rows:
            conn.execute(
                """
                INSERT INTO monthly_archive (telegram_id, username, points, period_label, archived_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (r["telegram_id"], r["username"], r["monthly_points"], period_label, now),
            )
        conn.execute("UPDATE users SET monthly_points = 0")
        return rows


# --- challenges (independent, scheduled) --------------------------------

def create_challenge(title: str, description: str, chat_id: int,
                      start_time: int, end_time: int, status: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO challenges (title, description, status, chat_id, start_time, end_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (title, description, status, chat_id, start_time, end_time, int(time.time())),
        )
        return cur.lastrowid


def get_challenge(challenge_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM challenges WHERE id = ?", (challenge_id,)
        ).fetchone()


def get_challenges_by_status(*statuses):
    placeholders = ",".join("?" for _ in statuses)
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM challenges WHERE status IN ({placeholders}) ORDER BY start_time ASC",
            statuses,
        ).fetchall()


def set_challenge_status(challenge_id: int, status: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE challenges SET status = ? WHERE id = ?", (status, challenge_id)
        )


def close_challenge(challenge_id: int, winner_id: int = None):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE challenges
            SET status = 'closed', winner_id = ?, closed_at = ?
            WHERE id = ?
            """,
            (winner_id, int(time.time()), challenge_id),
        )


def add_submission(challenge_id: int, telegram_id: int, content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO submissions (challenge_id, telegram_id, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (challenge_id, telegram_id, content, int(time.time())),
        )
        return cur.lastrowid


def get_submissions_for_challenge(challenge_id: int, oldest_first: bool = False):
    """All submissions for a challenge, joined with the submitter's
    username/display name so admins can review without a separate lookup.
    Newest-first by default; pass oldest_first=True to review in the
    order entries actually came in (e.g. judging "first N correct")."""
    order = "ASC" if oldest_first else "DESC"
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT s.id, s.telegram_id, s.content, s.status, s.created_at,
                   u.username, u.display_name
            FROM submissions s
            LEFT JOIN users u ON u.telegram_id = s.telegram_id
            WHERE s.challenge_id = ?
            ORDER BY s.created_at {order}
            """,
            (challenge_id,),
        ).fetchall()


def get_submission(submission_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()


def set_submission_status(submission_id: int, status: str) -> bool:
    """Returns True if a row was actually updated."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE submissions SET status = ? WHERE id = ?",
            (status, submission_id),
        )
        return cur.rowcount > 0


# --- payouts --------------------------------------------------------------

def upsert_payout(telegram_id: int, wallet_address: str, amount_dtf: float,
                   cycle: str, period_label: str, note: str = ""):
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT id FROM payouts
            WHERE telegram_id = ? AND cycle = ? AND period_label = ? AND status = 'pending'
            """,
            (telegram_id, cycle, period_label),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE payouts SET amount_dtf = ?, wallet_address = ?, note = ? WHERE id = ?",
                (amount_dtf, wallet_address, note, existing["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO payouts (telegram_id, wallet_address, amount_dtf, cycle, period_label, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, wallet_address, amount_dtf, cycle, period_label, note, int(time.time())),
            )


def get_payouts(cycle: str, period_label: str, status: str = None):
    with get_conn() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM payouts WHERE cycle = ? AND period_label = ? AND status = ? ORDER BY amount_dtf DESC",
                (cycle, period_label, status),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM payouts WHERE cycle = ? AND period_label = ? ORDER BY amount_dtf DESC",
            (cycle, period_label),
        ).fetchall()


def finalize_payouts(cycle: str, period_label: str):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE payouts SET status = 'finalized', finalized_at = ?
            WHERE cycle = ? AND period_label = ? AND status = 'pending'
            """,
            (int(time.time()), cycle, period_label),
        )


def clear_pending_payouts(cycle: str, period_label: str):
    """Used when regenerating an auto payout list from scratch."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM payouts WHERE cycle = ? AND period_label = ? AND status = 'pending'",
            (cycle, period_label),
        )
