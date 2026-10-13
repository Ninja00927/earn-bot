"""SQLite storage for the earn bot: users, balances, submissions and withdrawals.

All money is stored as integer cents (1/100 of CURRENCY) so there are no rounding errors.
Every balance change goes through a single transaction that also flips the record's status
from 'pending', so pressing a button twice can never pay twice.
"""

import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    balance INTEGER NOT NULL DEFAULT 0,      -- cents
    earned INTEGER NOT NULL DEFAULT 0,       -- cents, total ever approved
    blocked INTEGER NOT NULL DEFAULT 0,
    joined_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,                      -- the text, or the file's caption ('' if none)
    file_id TEXT,                            -- Telegram file id of an attached file, if any
    file_type TEXT,                          -- photo / document / video / audio / voice / animation / video_note
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    amount INTEGER NOT NULL DEFAULT 0,       -- cents credited on approval
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by INTEGER
);
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,                 -- cents, reserved from balance on request
    method TEXT NOT NULL,                    -- card / bep20
    details TEXT NOT NULL,                   -- card number or BEP20 address
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / paid / rejected
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by INTEGER
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,                -- the same link is never stored twice
    added_by INTEGER NOT NULL,
    added_at INTEGER NOT NULL,
    given_to INTEGER,                        -- NULL until a user receives it; each link goes to one user only
    given_at INTEGER
);
CREATE TABLE IF NOT EXISTS admin_msgs (
    kind TEXT NOT NULL,                      -- 'sub' or 'wd'
    item_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
"""


class DB:
    def __init__(self, path: str):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions below
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Databases created before file uploads existed lack these columns.
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(submissions)")}
        for col in ("file_id", "file_type"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE submissions ADD COLUMN {col} TEXT")

    # ---------- users ----------
    def touch_user(self, user_id: int, username: str | None, full_name: str) -> None:
        self.conn.execute(
            """INSERT INTO users (user_id, username, full_name, joined_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
            (user_id, username, full_name, int(time.time())),
        )

    def get_user(self, user_id: int):
        return self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def set_blocked(self, user_id: int, blocked: bool) -> bool:
        cur = self.conn.execute("UPDATE users SET blocked=? WHERE user_id=?", (int(blocked), user_id))
        return cur.rowcount == 1

    def all_user_ids(self) -> list[int]:
        return [r["user_id"] for r in self.conn.execute("SELECT user_id FROM users WHERE blocked=0")]

    def adjust_balance(self, user_id: int, delta: int) -> int | None:
        """Manual admin correction. Returns the new balance, or None if the user is unknown
        or the balance would go negative."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.get_user(user_id)
            if not row or row["balance"] + delta < 0:
                return None
            self.conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (delta, user_id))
            return row["balance"] + delta

    # ---------- submissions ----------
    def create_submission(self, user_id: int, text: str, file_id: str | None = None,
                          file_type: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO submissions (user_id, text, file_id, file_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, text, file_id, file_type, int(time.time())),
        )
        return cur.lastrowid

    def get_submission(self, sub_id: int):
        return self.conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()

    def approve_submission(self, sub_id: int, amount: int, admin_id: int):
        """Credits the user once. Returns the updated row, or None if it was already decided."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                """UPDATE submissions SET status='approved', amount=?, decided_at=?, decided_by=?
                   WHERE id=? AND status='pending'""",
                (amount, int(time.time()), admin_id, sub_id),
            )
            if cur.rowcount != 1:
                return None
            sub = self.get_submission(sub_id)
            self.conn.execute(
                "UPDATE users SET balance=balance+?, earned=earned+? WHERE user_id=?",
                (amount, amount, sub["user_id"]),
            )
            return sub

    def reject_submission(self, sub_id: int, admin_id: int):
        cur = self.conn.execute(
            """UPDATE submissions SET status='rejected', decided_at=?, decided_by=?
               WHERE id=? AND status='pending'""",
            (int(time.time()), admin_id, sub_id),
        )
        return self.get_submission(sub_id) if cur.rowcount == 1 else None

    def pending_submissions(self, limit: int = 20):
        return self.conn.execute(
            "SELECT * FROM submissions WHERE status='pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    # ---------- withdrawals ----------
    def create_withdrawal(self, user_id: int, amount: int, method: str, details: str) -> int | None:
        """Reserves the amount from the balance. Returns None if the balance is too low."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                (amount, user_id, amount),
            )
            if cur.rowcount != 1:
                return None
            cur = self.conn.execute(
                "INSERT INTO withdrawals (user_id, amount, method, details, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, amount, method, details, int(time.time())),
            )
            return cur.lastrowid

    def get_withdrawal(self, wd_id: int):
        return self.conn.execute("SELECT * FROM withdrawals WHERE id=?", (wd_id,)).fetchone()

    def mark_withdrawal_paid(self, wd_id: int, admin_id: int):
        cur = self.conn.execute(
            """UPDATE withdrawals SET status='paid', decided_at=?, decided_by=?
               WHERE id=? AND status='pending'""",
            (int(time.time()), admin_id, wd_id),
        )
        return self.get_withdrawal(wd_id) if cur.rowcount == 1 else None

    def reject_withdrawal(self, wd_id: int, admin_id: int):
        """Rejects and returns the reserved money to the user's balance."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.execute(
                """UPDATE withdrawals SET status='rejected', decided_at=?, decided_by=?
                   WHERE id=? AND status='pending'""",
                (int(time.time()), admin_id, wd_id),
            )
            if cur.rowcount != 1:
                return None
            wd = self.get_withdrawal(wd_id)
            self.conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (wd["amount"], wd["user_id"]))
            return wd

    def pending_withdrawals(self, limit: int = 20):
        return self.conn.execute(
            "SELECT * FROM withdrawals WHERE status='pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()

    def user_has_pending_withdrawal(self, user_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM withdrawals WHERE user_id=? AND status='pending'", (user_id,)
        ).fetchone() is not None

    # ---------- links handed out by the "🔗 Link olish" button ----------
    def add_links(self, urls: list[str], admin_id: int) -> int:
        """Stores new links, skipping ones already in the database. Returns how many were added."""
        now = int(time.time())
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            added = 0
            for url in urls:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO links (url, added_by, added_at) VALUES (?, ?, ?)", (url, admin_id, now)
                )
                added += cur.rowcount
            return added

    def take_link(self, user_id: int) -> str | None:
        """Hands the oldest unused link to this user and marks it used. None when none are left."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT id, url FROM links WHERE given_to IS NULL ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            self.conn.execute(
                "UPDATE links SET given_to=?, given_at=? WHERE id=?", (user_id, int(time.time()), row["id"])
            )
            return row["url"]

    def link_counts(self) -> tuple[int, int]:
        """(available, given)"""
        row = self.conn.execute(
            "SELECT SUM(given_to IS NULL), SUM(given_to IS NOT NULL) FROM links"
        ).fetchone()
        return row[0] or 0, row[1] or 0

    def clear_unused_links(self) -> int:
        return self.conn.execute("DELETE FROM links WHERE given_to IS NULL").rowcount

    # ---------- admin message links (so reply-to-message works) ----------
    def add_admin_msg(self, kind: str, item_id: int, chat_id: int, message_id: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO admin_msgs VALUES (?, ?, ?, ?)", (kind, item_id, chat_id, message_id))

    def find_admin_msg(self, chat_id: int, message_id: int):
        return self.conn.execute(
            "SELECT kind, item_id FROM admin_msgs WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        ).fetchone()

    def admin_msgs_for(self, kind: str, item_id: int):
        return self.conn.execute(
            "SELECT chat_id, message_id FROM admin_msgs WHERE kind=? AND item_id=?", (kind, item_id)
        ).fetchall()

    # ---------- stats ----------
    def stats(self) -> dict:
        one = lambda sql: self.conn.execute(sql).fetchone()[0] or 0  # noqa: E731
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "sub_pending": one("SELECT COUNT(*) FROM submissions WHERE status='pending'"),
            "sub_approved": one("SELECT COUNT(*) FROM submissions WHERE status='approved'"),
            "sub_rejected": one("SELECT COUNT(*) FROM submissions WHERE status='rejected'"),
            "credited": one("SELECT SUM(amount) FROM submissions WHERE status='approved'"),
            "balances": one("SELECT SUM(balance) FROM users"),
            "wd_pending": one("SELECT COUNT(*) FROM withdrawals WHERE status='pending'"),
            "wd_pending_sum": one("SELECT SUM(amount) FROM withdrawals WHERE status='pending'"),
            "paid": one("SELECT SUM(amount) FROM withdrawals WHERE status='paid'"),
        }
