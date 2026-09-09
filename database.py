"""
database.py

Handles all persistent storage for the NFT tracker bot using SQLite.

Tables:
- wallets: which wallets each Discord user is tracking, per chain
- holdings: NFTs currently held by a tracked wallet (used to compute P&L on sell)
- seen_txns: last-seen transaction markers per wallet, so the poller
             doesn't re-alert on the same activity every cycle
"""

import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Uses DB_PATH env var if set (e.g. pointing at a Railway persistent volume
# mount like /data/tracker.db). Falls back to a local file for dev.
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "tracker.db"))


def init_db():
    """Create tables if they don't already exist. Safe to call every startup."""
    print(f"[database] Using DB_PATH = {DB_PATH}", flush=True)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                chain TEXT NOT NULL,           -- 'ethereum', 'solana', 'robinhood'
                address TEXT NOT NULL,
                added_at TEXT NOT NULL,
                UNIQUE(user_id, chain, address)
            );

            CREATE TABLE IF NOT EXISTS holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_id INTEGER NOT NULL,
                contract_address TEXT NOT NULL,
                token_id TEXT NOT NULL,
                collection_name TEXT,
                token_name TEXT,
                purchase_price REAL,
                purchase_currency TEXT,
                purchased_at TEXT,
                FOREIGN KEY (wallet_id) REFERENCES wallets(id),
                UNIQUE(wallet_id, contract_address, token_id)
            );

            CREATE TABLE IF NOT EXISTS seen_txns (
                wallet_id INTEGER PRIMARY KEY,
                last_signature TEXT,           -- tx hash / signature of last processed event
                last_checked_at TEXT,
                FOREIGN KEY (wallet_id) REFERENCES wallets(id)
            );
            """
        )


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------- Wallets ----------

def add_wallet(user_id: str, chain: str, address: str) -> bool:
    """Returns True if added, False if it already existed."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO wallets (user_id, chain, address, added_at) VALUES (?, ?, ?, ?)",
                (str(user_id), chain.lower(), address, _now()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def remove_wallet(user_id: str, chain: str, address: str) -> bool:
    """Returns True if a row was removed."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM wallets WHERE user_id = ? AND chain = ? AND address = ?",
            (str(user_id), chain.lower(), address),
        )
        return cur.rowcount > 0


def get_wallets_for_user(user_id: str):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ? ORDER BY added_at", (str(user_id),)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_wallets():
    """Used by the poller to iterate every tracked wallet across all users."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM wallets").fetchall()
        return [dict(r) for r in rows]


# ---------- Holdings (for P&L on sell) ----------

def add_holding(wallet_id: int, contract_address: str, token_id: str,
                 collection_name: str, token_name: str,
                 purchase_price: float, purchase_currency: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO holdings
               (wallet_id, contract_address, token_id, collection_name, token_name,
                purchase_price, purchase_currency, purchased_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(wallet_id, contract_address, token_id) DO UPDATE SET
                 purchase_price=excluded.purchase_price,
                 purchase_currency=excluded.purchase_currency,
                 purchased_at=excluded.purchased_at""",
            (wallet_id, contract_address, token_id, collection_name, token_name,
             purchase_price, purchase_currency, _now()),
        )


def pop_holding(wallet_id: int, contract_address: str, token_id: str):
    """Remove a holding on sell and return its stored purchase info (or None)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM holdings WHERE wallet_id = ? AND contract_address = ? AND token_id = ?",
            (wallet_id, contract_address, token_id),
        ).fetchone()
        if row:
            conn.execute(
                "DELETE FROM holdings WHERE wallet_id = ? AND contract_address = ? AND token_id = ?",
                (wallet_id, contract_address, token_id),
            )
            return dict(row)
        return None


def get_holdings_for_wallet(wallet_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM holdings WHERE wallet_id = ?", (wallet_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- Seen transaction markers ----------

def get_last_signature(wallet_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_signature FROM seen_txns WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
        return row["last_signature"] if row else None


def set_last_signature(wallet_id: int, signature: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO seen_txns (wallet_id, last_signature, last_checked_at)
               VALUES (?, ?, ?)
               ON CONFLICT(wallet_id) DO UPDATE SET
                 last_signature=excluded.last_signature,
                 last_checked_at=excluded.last_checked_at""",
            (wallet_id, signature, _now()),
        )
