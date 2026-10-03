"""SQLite at .data/paidyet.db: a read model for /due. Temporal is the source of truth.

Only confirmed fields and Telegram IDs are stored, never images or message text.
"""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          TEXT PRIMARY KEY,   -- Temporal workflow ID
    owner_id    INTEGER NOT NULL,   -- Telegram user who owes
    added_by    INTEGER NOT NULL,   -- Telegram user who added it
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    payee       TEXT,
    amount_inr  REAL NOT NULL,
    due_at      TEXT NOT NULL,
    has_time    INTEGER NOT NULL,
    next_at     TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    paid_at     TEXT
);
CREATE INDEX IF NOT EXISTS reminders_owner_open ON reminders (owner_id, paid_at);
"""


@dataclass
class Row:
    id: str
    owner_id: int
    added_by: int
    kind: str
    title: str
    payee: str | None
    amount_inr: float
    due_at: str
    has_time: bool
    next_at: str | None
    paid_at: str | None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def save(self, rid: str, owner_id: int, added_by: int, kind: str, title: str, payee: str | None,
             amount_inr: float, due_at: str, has_time: bool, next_at: str) -> None:  # fmt: skip
        with self._db:
            self._db.execute(
                """INSERT INTO reminders (id, owner_id, added_by, kind, title, payee, amount_inr, due_at, has_time, next_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET next_at = excluded.next_at""",
                (rid, owner_id, added_by, kind, title, payee, amount_inr, due_at, int(has_time), next_at),
            )

    def set_next(self, rid: str, next_at: str | None) -> None:
        with self._db:
            self._db.execute("UPDATE reminders SET next_at = ? WHERE id = ?", (next_at, rid))

    def mark_paid(self, rid: str, paid_at: str) -> None:
        with self._db:
            self._db.execute("UPDATE reminders SET paid_at = ?, next_at = NULL WHERE id = ?", (paid_at, rid))

    def get(self, rid: str) -> Row | None:
        r = self._db.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
        return _row(r) if r else None

    def open_for(self, owner_id: int) -> list[Row]:
        rows = self._db.execute(
            "SELECT * FROM reminders WHERE owner_id = ? AND paid_at IS NULL ORDER BY due_at", (owner_id,)
        ).fetchall()
        return [_row(r) for r in rows]

    def open_added_by(self, added_by: int) -> list[Row]:
        """Open reminders someone added for other people (the admin's view)."""
        rows = self._db.execute(
            "SELECT * FROM reminders WHERE added_by = ? AND owner_id != ? AND paid_at IS NULL ORDER BY due_at",
            (added_by, added_by),
        ).fetchall()
        return [_row(r) for r in rows]


def _row(r: sqlite3.Row) -> Row:
    d = dict(r)
    d.pop("created_at")
    return Row(**d | {"has_time": bool(d["has_time"])})
