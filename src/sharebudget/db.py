from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT NOT NULL,
    payment_details TEXT NOT NULL DEFAULT '',
    bank_name TEXT NOT NULL DEFAULT '',
    preferred_currency TEXT NOT NULL DEFAULT 'BYN',
    private_started INTEGER NOT NULL DEFAULT 0 CHECK (private_started IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('BYN', 'RUB', 'EUR', 'USD')),
    admin_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived', 'closed')),
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_collections_chat ON collections(chat_id, status);

CREATE TABLE IF NOT EXISTS user_chats (
    user_id INTEGER NOT NULL REFERENCES users(id),
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS participants (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notifications_enabled INTEGER NOT NULL DEFAULT 0 CHECK (notifications_enabled IN (0, 1)),
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (collection_id, user_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('expense', 'repayment')),
    creator_id INTEGER NOT NULL REFERENCES users(id),
    counterparty_id INTEGER REFERENCES users(id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    comment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled')),
    confirmation_status TEXT NOT NULL DEFAULT 'not_required',
    confirmed_by INTEGER REFERENCES users(id),
    confirmed_at TEXT,
    confirmation_message_id INTEGER,
    cancelled_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_transactions_collection
ON transactions(collection_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_transactions_collection_history
ON transactions(collection_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_transactions_creator_history
ON transactions(creator_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_transactions_pending_repayments
ON transactions(collection_id, kind, status, confirmation_status, creator_id, counterparty_id);

CREATE TABLE IF NOT EXISTS expense_shares (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (transaction_id, user_id)
);

CREATE TABLE IF NOT EXISTS collection_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES users(id),
    target_user_id INTEGER REFERENCES users(id),
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_collection_events_collection
ON collection_events(collection_id, created_at);

CREATE INDEX IF NOT EXISTS idx_collection_events_history
ON collection_events(collection_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS bot_messages (
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chat_id, kind)
);

CREATE TABLE IF NOT EXISTS exchange_rate_cache (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rates_json TEXT NOT NULL,
    rate_date TEXT,
    fetched_at INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as connection:
            await connection.executescript(SCHEMA)
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA synchronous = NORMAL")
            columns = await connection.execute_fetchall("PRAGMA table_info(participants)")
            if "active" not in {column[1] for column in columns}:
                await connection.execute(
                    "ALTER TABLE participants ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            if "notifications_enabled" not in {column[1] for column in columns}:
                await connection.execute(
                    "ALTER TABLE participants ADD COLUMN notifications_enabled "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            await connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_participants_user_active
                ON participants(user_id, active, collection_id)
                """
            )
            user_columns = await connection.execute_fetchall("PRAGMA table_info(users)")
            user_column_names = {column[1] for column in user_columns}
            if "private_started" not in user_column_names:
                await connection.execute(
                    "ALTER TABLE users ADD COLUMN private_started INTEGER NOT NULL DEFAULT 0"
                )
            if "preferred_currency" not in user_column_names:
                await connection.execute(
                    "ALTER TABLE users ADD COLUMN preferred_currency TEXT NOT NULL DEFAULT 'BYN'"
                )
            if "bank_name" not in user_column_names:
                await connection.execute(
                    "ALTER TABLE users ADD COLUMN bank_name TEXT NOT NULL DEFAULT ''"
                )
            transaction_columns = await connection.execute_fetchall(
                "PRAGMA table_info(transactions)"
            )
            transaction_column_names = {column[1] for column in transaction_columns}
            if "confirmation_status" not in transaction_column_names:
                await connection.execute(
                    "ALTER TABLE transactions ADD COLUMN confirmation_status TEXT "
                    "NOT NULL DEFAULT 'not_required'"
                )
                await connection.execute(
                    "UPDATE transactions SET confirmation_status='confirmed' WHERE kind='repayment'"
                )
            if "confirmed_by" not in transaction_column_names:
                await connection.execute(
                    "ALTER TABLE transactions ADD COLUMN confirmed_by INTEGER REFERENCES users(id)"
                )
                await connection.execute(
                    "UPDATE transactions SET confirmed_by=counterparty_id "
                    "WHERE kind='repayment' AND confirmation_status='confirmed'"
                )
            if "confirmed_at" not in transaction_column_names:
                await connection.execute("ALTER TABLE transactions ADD COLUMN confirmed_at TEXT")
                await connection.execute(
                    "UPDATE transactions SET confirmed_at=created_at "
                    "WHERE kind='repayment' AND confirmation_status='confirmed'"
                )
            if "confirmation_message_id" not in transaction_column_names:
                await connection.execute(
                    "ALTER TABLE transactions ADD COLUMN confirmation_message_id INTEGER"
                )
            await connection.execute(
                """
                INSERT INTO collection_events(collection_id,kind,actor_id,created_at)
                SELECT c.id,'created',c.admin_id,c.created_at FROM collections c
                WHERE NOT EXISTS (
                    SELECT 1 FROM collection_events e
                    WHERE e.collection_id=c.id AND e.kind='created'
                )
                """
            )
            await connection.commit()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        await connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
        finally:
            await connection.close()

    async def load_exchange_rate_cache(self) -> dict | None:
        async with self.connect() as connection:
            cursor = await connection.execute(
                "SELECT rates_json,rate_date,fetched_at FROM exchange_rate_cache WHERE id=1"
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            rates = json.loads(row["rates_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(rates, dict):
            return None
        return {
            "rates": rates,
            "rate_date": row["rate_date"],
            "fetched_at": row["fetched_at"],
        }

    async def save_exchange_rate_cache(
        self,
        rates: dict[str, float],
        rate_date: str | None,
        fetched_at: int,
    ) -> None:
        payload = json.dumps(rates, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        async with self.connect() as connection:
            await connection.execute(
                """
                INSERT INTO exchange_rate_cache(id,rates_json,rate_date,fetched_at)
                VALUES(1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    rates_json=excluded.rates_json,
                    rate_date=excluded.rate_date,
                    fetched_at=excluded.fetched_at
                """,
                (payload, rate_date, fetched_at),
            )
            await connection.commit()
