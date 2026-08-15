"""SQLite storage layer.

Money is stored as integer cents everywhere. Dollars only exist at the edges
(the parser reading a receipt, and the browser formatting a number).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per visit. A manual entry is a receipt with a single item.
CREATE TABLE IF NOT EXISTS receipts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no     TEXT,
    purchased_on   TEXT NOT NULL,          -- YYYY-MM-DD
    purchased_at   TEXT,                   -- full local timestamp when known
    source         TEXT NOT NULL,          -- 'manual' | 'pdf'
    merchant       TEXT,
    subtotal_cents INTEGER,
    tax_cents      INTEGER,
    tip_cents      INTEGER,
    total_cents    INTEGER,
    note           TEXT,
    filename       TEXT,
    file_sha256    TEXT,
    created_at     TEXT NOT NULL,
    created_by     INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_no
    ON receipts(receipt_no) WHERE receipt_no IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_sha
    ON receipts(file_sha256) WHERE file_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_receipts_date ON receipts(purchased_on);

-- Line items. `qualifying` marks the founders-discounted wine/beer that is the
-- only thing counting toward breakeven; food and other undiscounted items are
-- kept for context but excluded from every total.
CREATE TABLE IF NOT EXISTS items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id       INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL DEFAULT 0,
    description      TEXT NOT NULL,
    description_norm TEXT NOT NULL DEFAULT '',   -- lowercased, vintage stripped: groups repeat orders
    detail           TEXT,
    category         TEXT NOT NULL DEFAULT 'other',   -- wine | beer | other
    serving          TEXT,                            -- Glass | Bottle | Can | ...
    reg_price_cents  INTEGER NOT NULL DEFAULT 0,
    discount_cents   INTEGER NOT NULL DEFAULT 0,
    paid_cents       INTEGER NOT NULL DEFAULT 0,
    qualifying       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_receipt ON items(receipt_id);
CREATE INDEX IF NOT EXISTS idx_items_qualifying ON items(qualifying);
CREATE INDEX IF NOT EXISTS idx_items_norm ON items(description_norm);
"""

DEFAULT_SETTINGS = {
    "membership_fee_cents": "150000",
    "membership_tax_cents": "0",
    "term_start": "2026-08-07",
    "term_end": "2027-08-06",
    "discount_percent": "50",
    "member_name": "Obscure Wine Co. Auburndale",
}


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to SCHEMA_VERSION.

    The CREATE TABLE statements are all IF NOT EXISTS, so a database created by
    an older version keeps its original columns until they are added here.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "description_norm" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN description_norm TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_norm ON items(description_norm)")

    # Backfill any row that predates the column.
    from .search import normalize_description

    stale = conn.execute(
        "SELECT id, description FROM items WHERE description_norm = ''"
    ).fetchall()
    for row in stale:
        conn.execute(
            "UPDATE items SET description_norm = ? WHERE id = ?",
            (normalize_description(row["description"]), row["id"]),
        )

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db() -> None:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    merged = dict(DEFAULT_SETTINGS)
    merged.update({r["key"]: r["value"] for r in rows})
    return merged


def set_settings(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    for key, value in values.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
