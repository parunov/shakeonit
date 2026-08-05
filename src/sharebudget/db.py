from __future__ import annotations

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

CREATE TABLE IF NOT EXISTS participants (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
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
    cancelled_by INTEGER REFERENCES users(id),
    cancelled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_transactions_collection
ON transactions(collection_id, status, created_at);

CREATE TABLE IF NOT EXISTS expense_shares (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    amount INTEGER NOT NULL CHECK (amount >= 0),
    PRIMARY KEY (transaction_id, user_id)
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as connection:
            await connection.executescript(SCHEMA)
            columns = await connection.execute_fetchall("PRAGMA table_info(participants)")
            if "active" not in {column[1] for column in columns}:
                await connection.execute(
                    "ALTER TABLE participants ADD COLUMN active INTEGER NOT NULL DEFAULT 1"
                )
            await connection.commit()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            await connection.close()
