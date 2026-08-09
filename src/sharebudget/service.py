from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from .db import Database
from .money import CURRENCIES, split_amount


class DomainError(Exception):
    """A safe, user-facing business rule violation."""


@dataclass(frozen=True)
class Debt:
    debtor_id: int
    creditor_id: int
    amount: int


@dataclass(frozen=True)
class CollectionSnapshot:
    participants: list
    balances: dict[int, int]
    debts: list[Debt]
    total: int


@dataclass(frozen=True)
class CollectionView:
    collection: object
    snapshot: CollectionSnapshot
    history: list
    events: list
    shares: dict[int, list[dict]]
    notifications_enabled: bool
    pending_repayments: dict[int, int]
    payment_methods: dict[int, list[dict]]
    inactive_transaction_ids: set[int]


async def _fetchone(connection, query: str, params=()):
    cursor = await connection.execute(query, params)
    return await cursor.fetchone()


class BudgetService:
    def __init__(self, database: Database):
        self.db = database
        self._write_slots = asyncio.Semaphore(4)

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        full_name: str,
        private_started: bool = False,
    ) -> bool:
        normalized_username = username.lower() if username else None
        async with self.db.connect() as connection:
            existing = await _fetchone(
                connection,
                "SELECT username,full_name,private_started,name_customized FROM users WHERE id=?",
                (user_id,),
            )
            if existing is not None:
                stored_name = existing["full_name"] if existing["name_customized"] else full_name
                if (
                    existing["username"] == normalized_username
                    and existing["full_name"] == stored_name
                    and (existing["private_started"] or not private_started)
                ):
                    return False
                await connection.execute(
                    """
                    UPDATE users SET username=?,full_name=?,
                        private_started=MAX(private_started,?),updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (normalized_username, stored_name, int(private_started), user_id),
                )
            else:
                await connection.execute(
                    "INSERT INTO users(id,username,full_name,private_started) VALUES (?,?,?,?)",
                    (user_id, normalized_username, full_name, int(private_started)),
                )
            if private_started:
                await connection.execute(
                    """
                    UPDATE participants SET notifications_enabled=1
                    WHERE user_id=? AND active=1 AND notifications_configured=0
                    """,
                    (user_id,),
                )
            await connection.commit()
            return existing is None

    async def sync_token(self, user_id: int, context_chat_id: int | None = None) -> str:
        """Return a compact version of data visible to one Mini App user."""
        async with self.db.connect() as connection:
            row = await _fetchone(
                connection,
                """
                WITH visible(collection_id) AS (
                    SELECT collection_id FROM participants WHERE user_id=?
                    UNION
                    SELECT id FROM collections WHERE chat_id=?
                )
                SELECT
                    (SELECT COUNT(*) FROM collections c JOIN visible v ON v.collection_id=c.id)
                        collection_count,
                    (SELECT SUM(c.status='active') FROM collections c
                        JOIN visible v ON v.collection_id=c.id) active_count,
                    (SELECT SUM(c.status='archived') FROM collections c
                        JOIN visible v ON v.collection_id=c.id) archived_count,
                    (SELECT SUM(c.admin_id) FROM collections c
                        JOIN visible v ON v.collection_id=c.id) admin_checksum,
                    (SELECT MAX(t.id) FROM transactions t
                        JOIN visible v ON v.collection_id=t.collection_id) transaction_id,
                    (SELECT MAX(t.updated_at) FROM transactions t
                        JOIN visible v ON v.collection_id=t.collection_id) transaction_updated,
                    (SELECT MAX(e.id) FROM collection_events e
                        JOIN visible v ON v.collection_id=e.collection_id) event_id,
                    (SELECT COUNT(*) FROM participants p
                        JOIN visible v ON v.collection_id=p.collection_id) participant_count,
                    (SELECT SUM(p.active) FROM participants p
                        JOIN visible v ON v.collection_id=p.collection_id) participant_active,
                    (SELECT SUM(p.notifications_enabled) FROM participants p
                        JOIN visible v ON v.collection_id=p.collection_id) notification_count,
                    (SELECT MAX(u.updated_at) FROM users u JOIN participants p ON p.user_id=u.id
                        JOIN visible v ON v.collection_id=p.collection_id) user_updated
                """,
                (user_id, context_chat_id),
            )
        return "|".join(str(value or "") for value in row)

    async def has_started_private_chat(self, user_id: int) -> bool:
        async with self.db.connect() as connection:
            row = await _fetchone(
                connection, "SELECT private_started FROM users WHERE id=?", (user_id,)
            )
            return bool(row and row["private_started"])

    async def replace_bot_message(self, chat_id: int, kind: str, message_id: int) -> int | None:
        """Remember one current bot message per chat and return the message it replaced."""
        async with self.db.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            previous = await _fetchone(
                connection,
                "SELECT message_id FROM bot_messages WHERE chat_id=? AND kind=?",
                (chat_id, kind),
            )
            await connection.execute(
                """
                INSERT INTO bot_messages(chat_id,kind,message_id) VALUES (?,?,?)
                ON CONFLICT(chat_id,kind) DO UPDATE SET
                    message_id=excluded.message_id,updated_at=CURRENT_TIMESTAMP
                """,
                (chat_id, kind, message_id),
            )
            await connection.commit()
        old_message_id = previous["message_id"] if previous else None
        return old_message_id if old_message_id != message_id else None

    async def take_bot_message(self, chat_id: int, kind: str) -> int | None:
        """Return and forget a tracked bot message in one transaction."""
        async with self.db.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            row = await _fetchone(
                connection,
                "SELECT message_id FROM bot_messages WHERE chat_id=? AND kind=?",
                (chat_id, kind),
            )
            await connection.execute(
                "DELETE FROM bot_messages WHERE chat_id=? AND kind=?", (chat_id, kind)
            )
            await connection.commit()
        return row["message_id"] if row else None

    async def take_bot_messages_by_prefix(self, kind_prefix: str) -> list[tuple[int, int]]:
        """Return and forget every tracked bot message whose kind starts with a prefix."""
        pattern = f"{kind_prefix}%"
        async with self.db.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            rows = await connection.execute_fetchall(
                "SELECT chat_id,message_id FROM bot_messages WHERE kind LIKE ?", (pattern,)
            )
            await connection.execute("DELETE FROM bot_messages WHERE kind LIKE ?", (pattern,))
            await connection.commit()
        return [(row["chat_id"], row["message_id"]) for row in rows]

    async def get_user(self, user_id: int):
        async with self.db.connect() as connection:
            return await _fetchone(connection, "SELECT * FROM users WHERE id=?", (user_id,))

    async def set_display_name(self, user_id: int, full_name: str) -> None:
        full_name = " ".join(full_name.split())
        if not 2 <= len(full_name) <= 80:
            raise DomainError("Имя должно содержать от 2 до 80 символов")
        async with self.db.connect() as connection:
            await connection.execute(
                """
                UPDATE users SET full_name=?,name_customized=1,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (full_name, user_id),
            )
            await connection.commit()

    async def list_payment_methods(self, user_id: int):
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(
                """
                SELECT id,bank_name,details,position FROM payment_methods
                WHERE user_id=? ORDER BY position,id
                """,
                (user_id,),
            )

    async def payment_methods_for_users(self, user_ids: Iterable[int]) -> dict[int, list[dict]]:
        ids = sorted(set(int(user_id) for user_id in user_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        async with self.db.connect() as connection:
            rows = await connection.execute_fetchall(
                f"""
                SELECT id,user_id,bank_name,details,position FROM payment_methods
                WHERE user_id IN ({placeholders}) ORDER BY user_id,position,id
                """,
                ids,
            )
        result: dict[int, list[dict]] = {user_id: [] for user_id in ids}
        for row in rows:
            result[row["user_id"]].append(dict(row))
        return result

    async def replace_payment_methods(self, user_id: int, methods: list[dict]) -> None:
        if len(methods) > 10:
            raise DomainError("Можно добавить не более 10 способов оплаты")
        cleaned: list[tuple[str, str, int]] = []
        for position, method in enumerate(methods):
            if not isinstance(method, dict):
                raise DomainError("Некорректные платежные данные")
            bank_name = str(method.get("bank_name", "")).strip()
            details = str(method.get("details", "")).strip()
            if not details:
                continue
            if len(bank_name) > 100 or len(details) > 500:
                raise DomainError("Проверьте длину платежных данных")
            cleaned.append((bank_name, details, position))
        async with self.db.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute("DELETE FROM payment_methods WHERE user_id=?", (user_id,))
            await connection.executemany(
                """
                INSERT INTO payment_methods(user_id,bank_name,details,position)
                VALUES (?,?,?,?)
                """,
                [(user_id, bank_name, details, position) for bank_name, details, position in cleaned],
            )
            primary_bank, primary_details = (cleaned[0][0], cleaned[0][1]) if cleaned else ("", "")
            await connection.execute(
                """
                UPDATE users SET bank_name=?,payment_details=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (primary_bank, primary_details, user_id),
            )
            await connection.commit()

    async def set_notification_preferences(self, user_id: int, preferences: dict) -> None:
        fields = (
            "notify_expenses",
            "notify_repayments",
            "notify_collection_events",
            "notify_reminders",
        )
        if set(preferences) != set(fields) or any(
            not isinstance(preferences[field], bool) for field in fields
        ):
            raise DomainError("Некорректные настройки уведомлений")
        async with self.db.connect() as connection:
            await connection.execute(
                """
                UPDATE users SET notify_expenses=?,notify_repayments=?,
                    notify_collection_events=?,notify_reminders=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (*(int(preferences[field]) for field in fields), user_id),
            )
            await connection.commit()

    async def notification_enabled_for_user(self, user_id: int, category: str) -> bool:
        allowed = {"expenses", "repayments", "collection_events", "reminders"}
        if category not in allowed:
            raise ValueError(f"Unknown notification category: {category}")
        async with self.db.connect() as connection:
            row = await _fetchone(
                connection, f"SELECT notify_{category} enabled FROM users WHERE id=?", (user_id,)
            )
        return bool(row and row["enabled"])

    async def get_shared_collection_user(self, requester_id: int, target_id: int):
        """Return a user only when both people occur in at least one collection."""
        async with self.db.connect() as connection:
            return await _fetchone(
                connection,
                """
                SELECT u.* FROM users u
                WHERE u.id=? AND EXISTS (
                    SELECT 1
                    FROM participants requester
                    JOIN participants target
                        ON target.collection_id=requester.collection_id
                    WHERE requester.user_id=? AND target.user_id=u.id
                )
                """,
                (target_id, requester_id),
            )

    async def set_payment_details(
        self, user_id: int, details: str, bank_name: str | None = None
    ) -> None:
        if len(details) > 500:
            raise DomainError("Платежные данные не должны быть длиннее 500 символов")
        if bank_name is not None and len(bank_name) > 100:
            raise DomainError("Название банка не должно быть длиннее 100 символов")
        current = await self.get_user(user_id)
        effective_bank = current["bank_name"] if bank_name is None and current else (bank_name or "")
        await self.replace_payment_methods(
            user_id,
            [{"bank_name": effective_bank, "details": details}] if details.strip() else [],
        )

    async def set_preferred_currency(self, user_id: int, currency: str) -> None:
        if currency not in CURRENCIES:
            raise DomainError("Неподдерживаемая валюта")
        async with self.db.connect() as connection:
            await connection.execute(
                "UPDATE users SET preferred_currency=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (currency, user_id),
            )
            await connection.commit()

    async def create_collection(
        self, chat_id: int, title: str, currency: str, admin_id: int
    ) -> int:
        title = title.strip()
        if not 2 <= len(title) <= 80:
            raise DomainError("Название должно содержать от 2 до 80 символов")
        if currency not in CURRENCIES:
            raise DomainError("Неподдерживаемая валюта")
        async with self.db.connect() as connection:
            cursor = await connection.execute(
                "INSERT INTO collections(chat_id,title,currency,admin_id) VALUES (?,?,?,?)",
                (chat_id, title, currency, admin_id),
            )
            collection_id = cursor.lastrowid
            await connection.execute(
                """
                INSERT INTO participants(
                    collection_id,user_id,notifications_enabled,notifications_configured
                )
                SELECT ?,id,private_started,0 FROM users WHERE id=?
                """,
                (collection_id, admin_id),
            )
            await connection.execute(
                "INSERT INTO collection_events(collection_id,kind,actor_id) VALUES (?,'created',?)",
                (collection_id, admin_id),
            )
            await connection.commit()
            return int(collection_id)

    async def get_collection(self, collection_id: int):
        async with self.db.connect() as connection:
            return await _fetchone(
                connection,
                """
                SELECT c.*, u.full_name AS admin_name
                FROM collections c JOIN users u ON u.id=c.admin_id WHERE c.id=?
                """,
                (collection_id,),
            )

    async def get_collection_for_member(self, collection_id: int, user_id: int):
        async with self.db.connect() as connection:
            return await _fetchone(
                connection,
                """
                SELECT c.*,u.full_name AS admin_name
                FROM collections c
                JOIN users u ON u.id=c.admin_id
                JOIN participants p ON p.collection_id=c.id
                    AND p.user_id=? AND p.active=1
                WHERE c.id=?
                """,
                (user_id, collection_id),
            )

    async def list_collections(
        self, user_id: int, chat_id: int | None = None, include_archived: bool = True
    ):
        statuses = "('active','archived')" if include_archived else "('active')"
        query = f"""
            SELECT c.*,
                (SELECT COUNT(*) FROM participants p2
                 WHERE p2.collection_id=c.id AND p2.active=1) participants_count,
                1 is_participant
            FROM collections c JOIN participants p ON p.collection_id=c.id
            WHERE p.user_id=? AND p.active=1 AND c.status IN {statuses}
        """
        params: list[int] = [user_id]
        if chat_id is not None:
            query += " AND c.chat_id=?"
            params.append(chat_id)
        query += " ORDER BY c.status, c.created_at DESC"
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(query, params)

    async def list_visible_collections(self, user_id: int, chat_id: int | None = None):
        query = """
            SELECT c.*,
                (SELECT COUNT(*) FROM participants p2
                 WHERE p2.collection_id=c.id AND p2.active=1) participants_count,
                EXISTS(
                    SELECT 1 FROM participants mine
                    WHERE mine.collection_id=c.id AND mine.user_id=? AND mine.active=1
                ) is_participant
            FROM collections c
            WHERE c.status IN ('active','archived')
              AND (
                EXISTS(
                    SELECT 1 FROM participants mine
                    WHERE mine.collection_id=c.id AND mine.user_id=? AND mine.active=1
                )
        """
        params: list[int] = [user_id, user_id]
        if chat_id is not None:
            query += " OR (c.chat_id=? AND c.status='active')"
            params.append(chat_id)
        query += ")"
        if chat_id is not None:
            query += " ORDER BY (c.chat_id=? AND c.status='active') DESC, "
            params.append(chat_id)
        else:
            query += " ORDER BY "
        query += "is_participant DESC, c.status, c.created_at DESC"
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(query, params)

    async def list_chat_collections(self, chat_id: int):
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(
                """
                SELECT c.*, (SELECT COUNT(*) FROM participants p
                    WHERE p.collection_id=c.id AND p.active=1) participants_count
                FROM collections c WHERE chat_id=? AND status='active'
                ORDER BY created_at DESC
                """,
                (chat_id,),
            )

    async def list_user_collection_chats(self, user_id: int):
        """Return chats where the user can create another collection."""
        async with self.db.connect() as connection:
            collection_rows = await connection.execute_fetchall(
                """
                SELECT c.chat_id, MAX(c.created_at) last_seen,
                       (SELECT c2.title FROM collections c2
                        JOIN participants p2 ON p2.collection_id=c2.id
                        WHERE c2.chat_id=c.chat_id AND p2.user_id=? AND p2.active=1
                        ORDER BY c2.created_at DESC LIMIT 1) reference_title
                FROM collections c
                JOIN participants p ON p.collection_id=c.id
                WHERE p.user_id=? AND p.active=1 AND c.chat_id<>0
                GROUP BY c.chat_id ORDER BY last_seen DESC
                """,
                (user_id, user_id),
            )
            shared_rows = await connection.execute_fetchall(
                """
                SELECT chat_id, title reference_title, updated_at last_seen
                FROM user_chats WHERE user_id=? ORDER BY updated_at DESC
                """,
                (user_id,),
            )
        merged = {row["chat_id"]: dict(row) for row in collection_rows}
        merged.update({row["chat_id"]: dict(row) for row in shared_rows})
        return sorted(merged.values(), key=lambda row: row["last_seen"], reverse=True)

    async def register_user_chat(self, user_id: int, chat_id: int, title: str) -> None:
        clean_title = title.strip()[:100] or "Telegram-группа"
        async with self.db.connect() as connection:
            await connection.execute(
                """
                INSERT INTO user_chats(user_id,chat_id,title) VALUES (?,?,?)
                ON CONFLICT(user_id,chat_id) DO UPDATE SET
                    title=excluded.title,updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, chat_id, clean_title),
            )
            await connection.commit()

    async def can_create_in_chat(self, user_id: int, chat_id: int) -> bool:
        async with self.db.connect() as connection:
            row = await _fetchone(
                connection,
                """
                SELECT 1 FROM user_chats WHERE chat_id=? AND user_id=?
                UNION ALL
                SELECT 1 FROM collections c JOIN participants p ON p.collection_id=c.id
                WHERE c.chat_id=? AND p.user_id=? AND p.active=1 LIMIT 1
                """,
                (chat_id, user_id, chat_id, user_id),
            )
            return row is not None

    async def is_participant(self, collection_id: int, user_id: int) -> bool:
        async with self.db.connect() as connection:
            row = await _fetchone(
                connection,
                """
                SELECT 1 FROM participants
                WHERE collection_id=? AND user_id=? AND active=1
                """,
                (collection_id, user_id),
            )
            return row is not None

    async def join(self, collection_id: int, user_id: int, subscribe: bool = False) -> bool:
        """Join or restore membership and return True only for a new active membership."""
        collection = await self._active_collection(collection_id)
        async with self.db.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            existing = await _fetchone(
                connection,
                """
                SELECT p.active,p.notifications_configured,u.private_started
                FROM users u LEFT JOIN participants p
                  ON p.user_id=u.id AND p.collection_id=?
                WHERE u.id=?
                """,
                (collection_id, user_id),
            )
            if not existing:
                await connection.rollback()
                raise DomainError("Пользователь не зарегистрирован")
            enable_notifications = bool(subscribe or existing["private_started"])
            if existing["active"]:
                if subscribe:
                    await connection.execute(
                        """
                        UPDATE participants SET notifications_enabled=1,
                            notifications_configured=1
                        WHERE collection_id=? AND user_id=?
                        """,
                        (collection_id, user_id),
                    )
                elif enable_notifications and not existing["notifications_configured"]:
                    await connection.execute(
                        """
                        UPDATE participants SET notifications_enabled=1
                        WHERE collection_id=? AND user_id=?
                        """,
                        (collection_id, user_id),
                    )
                await connection.commit()
                return False
            await connection.execute(
                """
                INSERT INTO participants(
                    collection_id,user_id,active,notifications_enabled,notifications_configured
                ) VALUES (?,?,1,?,?)
                ON CONFLICT(collection_id,user_id) DO UPDATE SET
                    active=1,
                    notifications_enabled=CASE
                        WHEN participants.notifications_configured=1
                        THEN participants.notifications_enabled
                        ELSE excluded.notifications_enabled
                    END
                """,
                (
                    collection["id"],
                    user_id,
                    int(enable_notifications),
                    int(bool(subscribe)),
                ),
            )
            if subscribe:
                await connection.execute(
                    """
                    UPDATE participants SET notifications_enabled=1,
                        notifications_configured=1
                    WHERE collection_id=? AND user_id=?
                    """,
                    (collection_id, user_id),
                )
            if not existing or not existing["active"]:
                await connection.execute(
                    "INSERT INTO collection_events(collection_id,kind,actor_id,target_user_id) "
                    "VALUES (?,'joined',?,?)",
                    (collection_id, user_id, user_id),
                )
            await connection.commit()
            return True

    async def notification_subscription(self, collection_id: int, user_id: int) -> bool:
        async with self.db.connect() as connection:
            row = await _fetchone(
                connection,
                """
                SELECT notifications_enabled FROM participants
                WHERE collection_id=? AND user_id=? AND active=1
                """,
                (collection_id, user_id),
            )
            return bool(row and row["notifications_enabled"])

    async def set_notification_subscription(
        self, collection_id: int, user_id: int, enabled: bool
    ) -> None:
        await self._collection(collection_id)
        if not await self.is_participant(collection_id, user_id):
            raise DomainError("Уведомления доступны только участникам сбора")
        async with self.db.connect() as connection:
            await connection.execute(
                """
                UPDATE participants SET notifications_enabled=?,notifications_configured=1
                WHERE collection_id=? AND user_id=? AND active=1
                """,
                (int(enabled), collection_id, user_id),
            )
            await connection.commit()

    async def notification_subscribers(
        self, collection_id: int, category: str = "collection_events"
    ):
        allowed = {"expenses", "repayments", "collection_events", "reminders"}
        if category not in allowed:
            raise ValueError(f"Unknown notification category: {category}")
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(
                f"""
                SELECT p.user_id,u.full_name FROM participants p
                JOIN users u ON u.id=p.user_id
                WHERE p.collection_id=? AND p.active=1 AND p.notifications_enabled=1
                  AND u.notify_{category}=1
                ORDER BY p.user_id
                """,
                (collection_id,),
            )

    async def list_known_group_chat_ids(self) -> list[int]:
        async with self.db.connect() as connection:
            rows = await connection.execute_fetchall(
                """
                SELECT DISTINCT chat_id FROM collections WHERE chat_id<0
                UNION SELECT DISTINCT chat_id FROM user_chats WHERE chat_id<0
                ORDER BY chat_id
                """
            )
        return [row["chat_id"] for row in rows]

    async def list_participants(self, collection_id: int):
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(
                """
                SELECT u.id,u.username,u.full_name,u.payment_details,u.bank_name,p.joined_at,
                       c.admin_id=u.id AS is_admin
                FROM participants p JOIN users u ON u.id=p.user_id
                JOIN collections c ON c.id=p.collection_id
                WHERE p.collection_id=? AND p.active=1 ORDER BY is_admin DESC,u.full_name
                """,
                (collection_id,),
            )

    async def _active_members_on(
        self,
        connection,
        collection_id: int,
        user_ids: Iterable[int],
    ) -> tuple[object, set[int]]:
        """Validate collection status and load active members on an existing connection."""
        collection = await _fetchone(
            connection,
            "SELECT * FROM collections WHERE id=?",
            (collection_id,),
        )
        if not collection:
            raise DomainError("Сбор не найден")
        if collection["status"] != "active":
            raise DomainError("Сбор завершен и находится в архиве")
        requested = set(user_ids)
        if not requested:
            return collection, set()
        placeholders = ",".join("?" for _ in requested)
        rows = await connection.execute_fetchall(
            f"""
            SELECT user_id FROM participants
            WHERE collection_id=? AND active=1 AND user_id IN ({placeholders})
            """,
            (collection_id, *requested),
        )
        return collection, {row["user_id"] for row in rows}

    async def _snapshot_on(self, connection, collection_id: int) -> CollectionSnapshot:
        participants = await connection.execute_fetchall(
            """
            SELECT u.id,u.username,u.full_name,u.payment_details,u.bank_name,p.joined_at,p.active,
                   c.admin_id=u.id AS is_admin
            FROM participants p JOIN users u ON u.id=p.user_id
            JOIN collections c ON c.id=p.collection_id
            WHERE p.collection_id=?
            ORDER BY is_admin DESC,u.full_name
            """,
            (collection_id,),
        )
        ledger = await connection.execute_fetchall(
            """
            WITH ledger(user_id, delta) AS (
                SELECT creator_id, amount FROM transactions
                WHERE collection_id=? AND kind='expense' AND status='active'
                UNION ALL
                SELECT s.user_id, -s.amount FROM expense_shares s
                JOIN transactions t ON t.id=s.transaction_id
                WHERE t.collection_id=? AND t.status='active'
                UNION ALL
                SELECT creator_id, amount FROM transactions
                WHERE collection_id=? AND kind='repayment' AND status='active'
                  AND confirmation_status='confirmed'
                UNION ALL
                SELECT counterparty_id, -amount FROM transactions
                WHERE collection_id=? AND kind='repayment' AND status='active'
                  AND confirmation_status='confirmed'
            )
            SELECT user_id, SUM(delta) balance FROM ledger GROUP BY user_id
            """,
            (collection_id, collection_id, collection_id, collection_id),
        )
        total_row = await _fetchone(
            connection,
            """
            SELECT COALESCE(SUM(amount),0) total FROM transactions
            WHERE collection_id=? AND kind='expense' AND status='active'
            """,
            (collection_id,),
        )
        ledger_balances = {row["user_id"]: row["balance"] for row in ledger}
        participants = [
            row for row in participants if row["active"] or ledger_balances.get(row["id"], 0)
        ]
        balances = {row["id"]: ledger_balances.get(row["id"], 0) for row in participants}
        if sum(balances.values()) != 0:
            raise RuntimeError(f"Нарушена целостность балансов сбора #{collection_id}")
        return CollectionSnapshot(
            participants=participants,
            balances=balances,
            debts=simplify_balances(balances),
            total=total_row["total"],
        )

    async def collection_snapshot(self, collection_id: int) -> CollectionSnapshot:
        async with self.db.connect() as connection:
            return await self._snapshot_on(connection, collection_id)

    async def collection_view(
        self, collection_id: int, user_id: int, history_limit: int = 10, events_limit: int = 10
    ) -> CollectionView | None:
        """Load the complete collection screen through one SQLite connection."""
        async with self.db.connect() as connection:
            collection = await _fetchone(
                connection,
                """
                SELECT c.*,u.full_name AS admin_name,p.notifications_enabled
                FROM collections c
                JOIN users u ON u.id=c.admin_id
                JOIN participants p ON p.collection_id=c.id
                    AND p.user_id=? AND p.active=1
                WHERE c.id=?
                """,
                (user_id, collection_id),
            )
            if not collection:
                return None
            snapshot = await self._snapshot_on(connection, collection_id)
            history = await connection.execute_fetchall(
                """
                SELECT t.*, creator.full_name creator_name, creator.username creator_username,
                    counterparty.full_name counterparty_name,
                    counterparty.username counterparty_username,
                    (SELECT GROUP_CONCAT(u.full_name, ', ')
                     FROM expense_shares s JOIN users u ON u.id=s.user_id
                     WHERE s.transaction_id=t.id) shared_with
                FROM transactions t
                JOIN users creator ON creator.id=t.creator_id
                LEFT JOIN users counterparty ON counterparty.id=t.counterparty_id
                WHERE t.collection_id=?
                ORDER BY t.created_at DESC,t.id DESC LIMIT ?
                """,
                (collection_id, history_limit),
            )
            events = await connection.execute_fetchall(
                """
                SELECT e.*,actor.full_name actor_name,actor.username actor_username,
                       target.full_name target_name,target.username target_username
                FROM collection_events e
                JOIN users actor ON actor.id=e.actor_id
                LEFT JOIN users target ON target.id=e.target_user_id
                WHERE e.collection_id=?
                ORDER BY e.created_at DESC,e.id DESC LIMIT ?
                """,
                (collection_id, events_limit),
            )
            expense_ids = [row["id"] for row in history if row["kind"] == "expense"]
            shares: dict[int, list[dict]] = {}
            if expense_ids:
                placeholders = ",".join("?" for _ in expense_ids)
                share_rows = await connection.execute_fetchall(
                    f"""
                    SELECT s.transaction_id,s.user_id,s.amount,u.full_name,u.username
                    FROM expense_shares s JOIN users u ON u.id=s.user_id
                    WHERE s.transaction_id IN ({placeholders})
                    ORDER BY s.transaction_id,u.full_name,u.id
                    """,
                    expense_ids,
                )
                for row in share_rows:
                    shares.setdefault(row["transaction_id"], []).append(dict(row))
            pending_rows = await connection.execute_fetchall(
                """
                SELECT counterparty_id,SUM(amount) amount FROM transactions
                WHERE collection_id=? AND kind='repayment' AND creator_id=?
                  AND status='active' AND confirmation_status='pending'
                GROUP BY counterparty_id
                """,
                (collection_id, user_id),
            )
            inactive_transaction_ids = await self._transactions_with_inactive_participants_on(
                connection, (row["id"] for row in history)
            )
            participant_ids = [row["id"] for row in snapshot.participants]
            payment_methods: dict[int, list[dict]] = {
                participant_id: [] for participant_id in participant_ids
            }
            if participant_ids:
                placeholders = ",".join("?" for _ in participant_ids)
                method_rows = await connection.execute_fetchall(
                    f"""
                    SELECT id,user_id,bank_name,details,position FROM payment_methods
                    WHERE user_id IN ({placeholders}) ORDER BY user_id,position,id
                    """,
                    participant_ids,
                )
                for row in method_rows:
                    payment_methods[row["user_id"]].append(dict(row))
        return CollectionView(
            collection=collection,
            snapshot=snapshot,
            history=history,
            events=events,
            shares=shares,
            notifications_enabled=bool(collection["notifications_enabled"]),
            pending_repayments={row["counterparty_id"]: row["amount"] for row in pending_rows},
            payment_methods=payment_methods,
            inactive_transaction_ids=inactive_transaction_ids,
        )

    async def add_expense(
        self,
        collection_id: int,
        creator_id: int,
        amount: int,
        participant_ids: Iterable[int],
        comment: str,
        *,
        exact_shares: dict[int, int] | None = None,
    ) -> int:
        if amount <= 0:
            raise DomainError("Сумма должна быть больше нуля")
        if exact_shares is None:
            shares = split_amount(amount, list(participant_ids))
        else:
            shares = {int(user_id): int(share) for user_id, share in exact_shares.items()}
            if not shares or any(share <= 0 for share in shares.values()):
                raise DomainError("Сумма каждого участника должна быть больше нуля")
        if sum(shares.values()) != amount:
            raise DomainError("Общая сумма не совпадает с индивидуальными суммами")
        if len(comment.strip()) > 200:
            raise DomainError("Комментарий не должен быть длиннее 200 символов")
        async with self._write_slots, self.db.connect() as connection:
            _, active_members = await self._active_members_on(
                connection,
                collection_id,
                {*shares, creator_id},
            )
            if creator_id not in active_members:
                raise DomainError("Сначала нажмите «Участвовать в сборе»")
            if not set(shares).issubset(active_members):
                raise DomainError("Все выбранные люди должны участвовать в сборе")
            await connection.execute("BEGIN IMMEDIATE")
            cursor = await connection.execute(
                """
                INSERT INTO transactions(collection_id,kind,creator_id,amount,comment)
                VALUES (?,'expense',?,?,?)
                """,
                (collection_id, creator_id, amount, comment.strip()),
            )
            transaction_id = int(cursor.lastrowid)
            await connection.executemany(
                "INSERT INTO expense_shares(transaction_id,user_id,amount) VALUES (?,?,?)",
                [(transaction_id, user_id, share) for user_id, share in shares.items()],
            )
            await connection.commit()
            return transaction_id

    async def add_repayment(
        self,
        collection_id: int,
        debtor_id: int,
        creditor_id: int,
        amount: int,
        comment: str = "",
    ) -> int:
        if amount <= 0:
            raise DomainError("Сумма должна быть больше нуля")
        if debtor_id == creditor_id:
            raise DomainError("Нельзя вернуть долг самому себе")
        if len(comment.strip()) > 200:
            raise DomainError("Комментарий не должен быть длиннее 200 символов")
        async with self._write_slots, self.db.connect() as connection:
            _, active_members = await self._active_members_on(
                connection,
                collection_id,
                {debtor_id, creditor_id},
            )
            if debtor_id not in active_members:
                raise DomainError("Сначала нажмите «Участвовать в сборе»")
            if creditor_id not in active_members:
                raise DomainError("Все выбранные люди должны участвовать в сборе")
            await connection.execute("BEGIN IMMEDIATE")
            snapshot = await self._snapshot_on(connection, collection_id)
            direct_debt = next(
                (
                    debt
                    for debt in snapshot.debts
                    if debt.debtor_id == debtor_id and debt.creditor_id == creditor_id
                ),
                None,
            )
            if direct_debt is None:
                await connection.rollback()
                raise DomainError("По текущему балансу вы не должны этому участнику")
            pending_row = await _fetchone(
                connection,
                """
                SELECT COALESCE(SUM(amount),0) amount FROM transactions
                WHERE collection_id=? AND kind='repayment' AND creator_id=?
                  AND counterparty_id=? AND status='active'
                  AND confirmation_status='pending'
                """,
                (collection_id, debtor_id, creditor_id),
            )
            available = direct_debt.amount - pending_row["amount"]
            if amount > available:
                await connection.rollback()
                raise DomainError("Сумма возврата больше текущего долга этому участнику")
            cursor = await connection.execute(
                """
                INSERT INTO transactions(
                    collection_id,kind,creator_id,counterparty_id,amount,comment,
                    confirmation_status
                ) VALUES (?,'repayment',?,?,?,?, 'pending')
                """,
                (collection_id, debtor_id, creditor_id, amount, comment.strip()),
            )
            await connection.commit()
            return int(cursor.lastrowid)

    async def pending_repayments(self, collection_id: int, debtor_id: int) -> dict[int, int]:
        """Return unconfirmed repayment totals grouped by creditor."""
        async with self.db.connect() as connection:
            rows = await connection.execute_fetchall(
                """
                SELECT counterparty_id,SUM(amount) amount FROM transactions
                WHERE collection_id=? AND kind='repayment' AND creator_id=?
                  AND status='active' AND confirmation_status='pending'
                GROUP BY counterparty_id
                """,
                (collection_id, debtor_id),
            )
        return {row["counterparty_id"]: row["amount"] for row in rows}

    async def confirm_repayment(self, transaction_id: int, actor_id: int) -> int:
        async with self.db.connect() as connection:
            # The debt check and confirmation must see one SQLite state. Otherwise two
            # simultaneous confirmations can both validate an already changed balance.
            await connection.execute("BEGIN IMMEDIATE")
            transaction = await _fetchone(
                connection, "SELECT * FROM transactions WHERE id=?", (transaction_id,)
            )
            if not transaction or transaction["kind"] != "repayment":
                raise DomainError("Возврат долга не найден")
            if transaction["status"] != "active":
                raise DomainError("Возврат долга отменён")
            if transaction["confirmation_status"] == "confirmed":
                raise DomainError("Получение уже подтверждено")
            if transaction["counterparty_id"] != actor_id:
                raise DomainError("Подтвердить получение может только получатель")
            membership = await _fetchone(
                connection,
                """
                SELECT 1 FROM participants p JOIN collections c ON c.id=p.collection_id
                WHERE p.collection_id=? AND p.user_id=? AND p.active=1 AND c.status='active'
                """,
                (transaction["collection_id"], actor_id),
            )
            if not membership:
                raise DomainError("Сначала нажмите «Участвовать в сборе»")
            snapshot = await self._snapshot_on(connection, transaction["collection_id"])
            direct_debt = next(
                (
                    debt
                    for debt in snapshot.debts
                    if debt.debtor_id == transaction["creator_id"]
                    and debt.creditor_id == transaction["counterparty_id"]
                ),
                None,
            )
            if direct_debt is None or transaction["amount"] > direct_debt.amount:
                raise DomainError("Баланс изменился: этот возврат больше нельзя подтвердить")
            cursor = await connection.execute(
                """
                UPDATE transactions SET confirmation_status='confirmed',confirmed_by=?,
                    confirmed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='active' AND confirmation_status='pending'
                """,
                (actor_id, transaction_id),
            )
            if cursor.rowcount != 1:
                raise DomainError("Получение уже подтверждено или возврат отменён")
            await connection.commit()
        return transaction["collection_id"]

    async def reject_repayment(self, transaction_id: int, actor_id: int) -> int:
        transaction = await self.transaction(transaction_id)
        if not transaction or transaction["kind"] != "repayment":
            raise DomainError("Возврат долга не найден")
        if transaction["status"] != "active":
            raise DomainError("Возврат долга уже отклонён или отменён")
        if transaction["confirmation_status"] != "pending":
            raise DomainError("Подтверждённый возврат нельзя отклонить")
        if transaction["counterparty_id"] != actor_id:
            raise DomainError("Отклонить получение может только получатель")
        await self._require_member_active(transaction["collection_id"], actor_id)
        async with self.db.connect() as connection:
            cursor = await connection.execute(
                """
                UPDATE transactions SET status='cancelled',cancelled_by=?,
                    cancelled_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='active' AND confirmation_status='pending'
                """,
                (actor_id, transaction_id),
            )
            if cursor.rowcount != 1:
                raise DomainError("Возврат долга уже подтверждён, отклонён или отменён")
            await connection.commit()
        return transaction["collection_id"]

    async def get_balances(self, collection_id: int) -> dict[int, int]:
        return (await self.collection_snapshot(collection_id)).balances

    async def settlement(self, collection_id: int) -> list[Debt]:
        return (await self.collection_snapshot(collection_id)).debts

    async def request_funds(
        self, collection_id: int, creditor_id: int, cooldown_minutes: int = 15
    ) -> list[Debt]:
        await self._require_member_active(collection_id, creditor_id)
        debts = [
            debt for debt in await self.settlement(collection_id) if debt.creditor_id == creditor_id
        ]
        if not debts:
            raise DomainError("Сейчас вам никто не должен(а) по этому сбору")

        async with self.db.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            previous = await _fetchone(
                connection,
                """
                SELECT created_at FROM collection_events
                WHERE collection_id=? AND actor_id=? AND kind='funds_requested'
                ORDER BY id DESC LIMIT 1
                """,
                (collection_id, creditor_id),
            )
            if previous:
                requested_at = datetime.fromisoformat(previous["created_at"]).replace(tzinfo=UTC)
                remaining = timedelta(minutes=cooldown_minutes) - (datetime.now(UTC) - requested_at)
                if remaining.total_seconds() > 0:
                    await connection.rollback()
                    minutes = max(1, int((remaining.total_seconds() + 59) // 60))
                    raise DomainError(
                        f"Напоминание уже отправлено. Повторить можно через {minutes} мин."
                    )
            await connection.execute(
                """
                INSERT INTO collection_events(collection_id,kind,actor_id,details)
                VALUES (?,'funds_requested',?,?)
                """,
                (collection_id, creditor_id, str(len(debts))),
            )
            await connection.commit()
        return debts

    async def history(self, collection_id: int, limit: int = 50, offset: int = 0):
        await self._collection(collection_id)
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(
                """
                SELECT t.*, creator.full_name creator_name, creator.username creator_username,
                    counterparty.full_name counterparty_name,
                    counterparty.username counterparty_username,
                    (SELECT GROUP_CONCAT(u.full_name, ', ')
                     FROM expense_shares s JOIN users u ON u.id=s.user_id
                     WHERE s.transaction_id=t.id) shared_with
                FROM transactions t
                JOIN users creator ON creator.id=t.creator_id
                LEFT JOIN users counterparty ON counterparty.id=t.counterparty_id
                WHERE t.collection_id=?
                ORDER BY t.created_at DESC,t.id DESC LIMIT ? OFFSET ?
                """,
                (collection_id, limit, offset),
            )

    async def expense_shares_for_transactions(self, transaction_ids: Iterable[int]):
        ids = list(dict.fromkeys(int(item) for item in transaction_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        async with self.db.connect() as connection:
            rows = await connection.execute_fetchall(
                f"""
                    SELECT s.transaction_id,s.user_id,s.amount,u.full_name,u.username
                FROM expense_shares s JOIN users u ON u.id=s.user_id
                WHERE s.transaction_id IN ({placeholders})
                ORDER BY s.transaction_id,u.full_name,u.id
                """,
                ids,
            )
        result: dict[int, list[dict]] = {}
        for row in rows:
            result.setdefault(row["transaction_id"], []).append(dict(row))
        return result

    async def _transactions_with_inactive_participants_on(
        self, connection, transaction_ids: Iterable[int]
    ) -> set[int]:
        ids = list(dict.fromkeys(int(item) for item in transaction_ids))
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        rows = await connection.execute_fetchall(
            f"""
            SELECT t.id FROM transactions t
            WHERE t.id IN ({placeholders}) AND (
                (t.kind='expense' AND EXISTS (
                    SELECT 1 FROM expense_shares s
                    LEFT JOIN participants p
                      ON p.collection_id=t.collection_id AND p.user_id=s.user_id
                    WHERE s.transaction_id=t.id AND COALESCE(p.active,0)=0
                ))
                OR
                (t.kind='repayment' AND (
                    NOT EXISTS (
                        SELECT 1 FROM participants creator
                        WHERE creator.collection_id=t.collection_id
                          AND creator.user_id=t.creator_id AND creator.active=1
                    )
                    OR NOT EXISTS (
                        SELECT 1 FROM participants recipient
                        WHERE recipient.collection_id=t.collection_id
                          AND recipient.user_id=t.counterparty_id AND recipient.active=1
                    )
                ))
            )
            """,
            ids,
        )
        return {row["id"] for row in rows}

    async def transactions_with_inactive_participants(
        self, transaction_ids: Iterable[int]
    ) -> set[int]:
        async with self.db.connect() as connection:
            return await self._transactions_with_inactive_participants_on(
                connection, transaction_ids
            )

    async def pending_repayment_confirmation(self, user_id: int):
        """Return the newest repayment waiting for this recipient's decision."""
        async with self.db.connect() as connection:
            cursor = await connection.execute(
                """
                SELECT t.id,t.collection_id,t.creator_id,t.counterparty_id,t.amount,
                       t.comment,t.created_at,c.title collection_title,c.currency,
                       creator.full_name creator_name,creator.username creator_username,
                       COUNT(*) OVER() pending_count
                FROM transactions t
                JOIN collections c ON c.id=t.collection_id
                JOIN participants recipient
                  ON recipient.collection_id=c.id AND recipient.user_id=? AND recipient.active=1
                JOIN participants sender
                  ON sender.collection_id=c.id AND sender.user_id=t.creator_id AND sender.active=1
                JOIN users creator ON creator.id=t.creator_id
                WHERE t.kind='repayment' AND t.status='active'
                  AND t.confirmation_status='pending' AND t.counterparty_id=?
                  AND c.status='active'
                ORDER BY t.created_at DESC,t.id DESC
                LIMIT 1
                """,
                (user_id, user_id),
            )
            return await cursor.fetchone()

    async def due_repayment_reminders(
        self, now: datetime | None = None, *, limit: int = 100
    ) -> list[dict]:
        """Return pending confirmations whose first or final reminder is due."""
        now_utc = (now or datetime.now(UTC)).astimezone(UTC)
        # Belarus uses UTC+3 year-round; a fixed offset avoids a runtime tzdata dependency.
        local_zone = timezone(timedelta(hours=3), "Europe/Minsk")
        async with self.db.connect() as connection:
            rows = await connection.execute_fetchall(
                """
                SELECT t.id,t.collection_id,t.creator_id,t.counterparty_id,t.amount,
                       t.comment,t.created_at,t.confirmation_reminder_1_sent_at,
                       t.confirmation_reminder_2_sent_at,c.title collection_title,
                       c.currency,creator.full_name creator_name,
                       creator.username creator_username
                FROM transactions t
                JOIN collections c ON c.id=t.collection_id AND c.status='active'
                JOIN participants recipient
                  ON recipient.collection_id=c.id AND recipient.user_id=t.counterparty_id
                 AND recipient.active=1
                JOIN participants sender
                  ON sender.collection_id=c.id AND sender.user_id=t.creator_id
                 AND sender.active=1
                JOIN users recipient_user
                  ON recipient_user.id=t.counterparty_id AND recipient_user.notify_reminders=1
                JOIN users creator ON creator.id=t.creator_id
                WHERE t.kind='repayment' AND t.status='active'
                  AND t.confirmation_status='pending'
                  AND (t.confirmation_reminder_1_sent_at IS NULL
                       OR t.confirmation_reminder_2_sent_at IS NULL)
                ORDER BY t.created_at,t.id
                LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            )
        due: list[dict] = []
        for source in rows:
            row = dict(source)
            created_at = datetime.fromisoformat(row["created_at"].replace(" ", "T")).replace(
                tzinfo=UTC
            )
            first_due = created_at + timedelta(hours=1)
            created_local = created_at.astimezone(local_zone)
            second_due = datetime.combine(
                created_local.date() + timedelta(days=1),
                datetime.min.time().replace(hour=10),
                tzinfo=local_zone,
            ).astimezone(UTC)
            if row["confirmation_reminder_1_sent_at"] is None and now_utc >= first_due:
                row["reminder_stage"] = 1
                due.append(row)
            elif (
                row["confirmation_reminder_1_sent_at"] is not None
                and row["confirmation_reminder_2_sent_at"] is None
                and now_utc >= second_due
            ):
                first_sent_at = datetime.fromisoformat(
                    row["confirmation_reminder_1_sent_at"].replace(" ", "T")
                ).replace(tzinfo=UTC)
                if now_utc >= first_sent_at + timedelta(hours=1):
                    row["reminder_stage"] = 2
                    due.append(row)
        return due

    async def mark_repayment_reminder_sent(self, transaction_id: int, stage: int) -> bool:
        if stage not in (1, 2):
            raise ValueError("Reminder stage must be 1 or 2")
        column = f"confirmation_reminder_{stage}_sent_at"
        async with self.db.connect() as connection:
            cursor = await connection.execute(
                f"""
                UPDATE transactions SET {column}=CURRENT_TIMESTAMP
                WHERE id=? AND kind='repayment' AND status='active'
                  AND confirmation_status='pending' AND {column} IS NULL
                """,
                (transaction_id,),
            )
            await connection.commit()
        return cursor.rowcount == 1

    async def global_history(
        self,
        user_id: int,
        limit: int = 10,
        transaction_offset: int = 0,
        event_offset: int = 0,
        *,
        include_transactions: bool = True,
        include_events: bool = True,
    ):
        async with self.db.connect() as connection:
            transactions = []
            if include_transactions:
                transactions = await connection.execute_fetchall(
                    """
                    SELECT t.*,c.title collection_title,c.currency,
                           c.admin_id collection_admin_id,c.status collection_status,
                           p.active is_participant,
                           creator.full_name creator_name,creator.username creator_username,
                           counterparty.full_name counterparty_name,
                           counterparty.username counterparty_username
                    FROM transactions t
                    JOIN collections c ON c.id=t.collection_id
                    JOIN participants p ON p.collection_id=c.id AND p.user_id=?
                    JOIN users creator ON creator.id=t.creator_id
                    LEFT JOIN users counterparty ON counterparty.id=t.counterparty_id
                    ORDER BY t.created_at DESC,t.id DESC LIMIT ? OFFSET ?
                    """,
                    (user_id, limit, transaction_offset),
                )
            events = []
            if include_events:
                events = await connection.execute_fetchall(
                    """
                    SELECT e.*,c.title collection_title,c.currency,
                           p.active is_participant,
                           actor.full_name actor_name,actor.username actor_username,
                           target.full_name target_name,target.username target_username
                    FROM collection_events e
                    JOIN collections c ON c.id=e.collection_id
                    JOIN participants p ON p.collection_id=c.id AND p.user_id=?
                    JOIN users actor ON actor.id=e.actor_id
                    LEFT JOIN users target ON target.id=e.target_user_id
                    ORDER BY e.created_at DESC,e.id DESC LIMIT ? OFFSET ?
                    """,
                    (user_id, limit, event_offset),
                )
        return transactions, events

    async def expense_statistics(
        self, user_id: int, *, include_collections: bool = True
    ) -> dict:
        async with self.db.connect() as connection:
            currency_rows = await connection.execute_fetchall(
                """
                WITH movements AS (
                    SELECT t.collection_id,c.currency,t.created_at,'personal' metric,s.amount
                    FROM expense_shares s
                    JOIN transactions t ON t.id=s.transaction_id AND t.status='active'
                    JOIN collections c ON c.id=t.collection_id
                    WHERE s.user_id=?
                    UNION ALL
                    SELECT t.collection_id,c.currency,t.created_at,'paid',t.amount
                    FROM transactions t JOIN collections c ON c.id=t.collection_id
                    WHERE t.creator_id=? AND t.kind='expense' AND t.status='active'
                    UNION ALL
                    SELECT t.collection_id,c.currency,t.confirmed_at,'repaid',t.amount
                    FROM transactions t JOIN collections c ON c.id=t.collection_id
                    WHERE t.creator_id=? AND t.kind='repayment' AND t.status='active'
                      AND t.confirmation_status='confirmed'
                    UNION ALL
                    SELECT t.collection_id,c.currency,t.confirmed_at,'received',t.amount
                    FROM transactions t JOIN collections c ON c.id=t.collection_id
                    WHERE t.counterparty_id=? AND t.kind='repayment' AND t.status='active'
                      AND t.confirmation_status='confirmed'
                )
                SELECT currency,
                       SUM(CASE WHEN metric='personal' THEN amount ELSE 0 END) personal_amount,
                       SUM(CASE WHEN metric='paid' THEN amount ELSE 0 END) paid_amount,
                       SUM(CASE WHEN metric='repaid' THEN amount ELSE 0 END) repaid_amount,
                       SUM(CASE WHEN metric='received' THEN amount ELSE 0 END) received_amount,
                       SUM(CASE WHEN metric='personal' AND created_at>=datetime('now','start of month') THEN amount ELSE 0 END) monthly_personal_amount,
                       SUM(CASE WHEN metric='paid' AND created_at>=datetime('now','start of month') THEN amount ELSE 0 END) monthly_paid_amount,
                       SUM(CASE WHEN metric='repaid' AND created_at>=datetime('now','start of month') THEN amount ELSE 0 END) monthly_repaid_amount,
                       SUM(CASE WHEN metric='received' AND created_at>=datetime('now','start of month') THEN amount ELSE 0 END) monthly_received_amount,
                       SUM(metric='personal') personal_count,
                       SUM(metric='paid') paid_count,
                       SUM(metric='repaid') repaid_count,
                       SUM(metric='received') received_count,
                       SUM(metric='personal' AND created_at>=datetime('now','start of month')) monthly_personal_count,
                       SUM(metric='paid' AND created_at>=datetime('now','start of month')) monthly_paid_count,
                       SUM(metric='repaid' AND created_at>=datetime('now','start of month')) monthly_repaid_count,
                       SUM(metric='received' AND created_at>=datetime('now','start of month')) monthly_received_count
                FROM movements GROUP BY currency ORDER BY currency
                """,
                (user_id, user_id, user_id, user_id),
            )
            collection_rows = []
            if include_collections:
                collection_rows = await connection.execute_fetchall(
                    """
                WITH movements AS (
                    SELECT t.id transaction_id,t.collection_id,'personal' metric,s.amount
                    FROM expense_shares s JOIN transactions t ON t.id=s.transaction_id
                    WHERE s.user_id=? AND t.status='active'
                    UNION ALL
                    SELECT t.id,t.collection_id,'paid',t.amount FROM transactions t
                    WHERE t.creator_id=? AND t.kind='expense' AND t.status='active'
                    UNION ALL
                    SELECT t.id,t.collection_id,'repaid',t.amount FROM transactions t
                    WHERE t.creator_id=? AND t.kind='repayment' AND t.status='active'
                      AND t.confirmation_status='confirmed'
                    UNION ALL
                    SELECT t.id,t.collection_id,'received',t.amount FROM transactions t
                    WHERE t.counterparty_id=? AND t.kind='repayment' AND t.status='active'
                      AND t.confirmation_status='confirmed'
                )
                SELECT c.id collection_id,c.title,c.currency,
                       SUM(CASE WHEN m.metric='personal' THEN m.amount ELSE 0 END) personal_amount,
                       SUM(CASE WHEN m.metric='paid' THEN m.amount ELSE 0 END) paid_amount,
                       SUM(CASE WHEN m.metric='repaid' THEN m.amount ELSE 0 END) repaid_amount,
                       SUM(CASE WHEN m.metric='received' THEN m.amount ELSE 0 END) received_amount,
                       COUNT(DISTINCT m.transaction_id) operation_count
                FROM movements m JOIN collections c ON c.id=m.collection_id
                GROUP BY c.id,c.title,c.currency
                ORDER BY personal_amount DESC,repaid_amount DESC,c.title LIMIT 20
                """,
                    (user_id, user_id, user_id, user_id),
                )

        def amounts(field: str) -> dict[str, int]:
            return {row["currency"]: row[field] or 0 for row in currency_rows if row[field]}

        result = {
            "monthly_personal_by_currency": amounts("monthly_personal_amount"),
            "monthly_paid_by_currency": amounts("monthly_paid_amount"),
            "monthly_repaid_by_currency": amounts("monthly_repaid_amount"),
            "monthly_received_by_currency": amounts("monthly_received_amount"),
            "total_personal_by_currency": amounts("personal_amount"),
            "total_paid_by_currency": amounts("paid_amount"),
            "total_repaid_by_currency": amounts("repaid_amount"),
            "total_received_by_currency": amounts("received_amount"),
            "monthly_personal_count": sum(row["monthly_personal_count"] or 0 for row in currency_rows),
            "monthly_paid_count": sum(row["monthly_paid_count"] or 0 for row in currency_rows),
            "monthly_repaid_count": sum(row["monthly_repaid_count"] or 0 for row in currency_rows),
            "monthly_received_count": sum(row["monthly_received_count"] or 0 for row in currency_rows),
            "personal_count": sum(row["personal_count"] or 0 for row in currency_rows),
            "paid_count": sum(row["paid_count"] or 0 for row in currency_rows),
            "repaid_count": sum(row["repaid_count"] or 0 for row in currency_rows),
            "received_count": sum(row["received_count"] or 0 for row in currency_rows),
            "by_collection": [dict(row) for row in collection_rows],
            "by_collection_loaded": include_collections,
        }
        # Backward-compatible aliases now represent expenses assigned to the user.
        result["monthly_by_currency"] = result["monthly_personal_by_currency"]
        result["total_by_currency"] = result["total_personal_by_currency"]
        result["monthly_count"] = result["monthly_personal_count"]
        result["total_count"] = result["personal_count"]
        return result

    async def balance_overview(self, user_id: int) -> dict:
        """Return balances with a constant number of queries, regardless of collection count."""
        async with self.db.connect() as connection:
            collections = await connection.execute_fetchall(
                """
                SELECT c.* FROM collections c
                JOIN participants p ON p.collection_id=c.id
                WHERE p.user_id=? AND p.active=1 AND c.status='active'
                ORDER BY c.created_at DESC,c.id DESC
                """,
                (user_id,),
            )
            if not collections:
                return {"collections": [], "personal_debts": []}
            collection_ids = [row["id"] for row in collections]
            selected_values = ",".join("(?)" for _ in collection_ids)
            participants = await connection.execute_fetchall(
                f"""
                WITH selected(collection_id) AS (VALUES {selected_values})
                SELECT p.collection_id,p.active,u.id,u.full_name,u.username
                FROM participants p
                JOIN selected s ON s.collection_id=p.collection_id
                JOIN users u ON u.id=p.user_id
                """,
                collection_ids,
            )
            ledger = await connection.execute_fetchall(
                f"""
                WITH selected(collection_id) AS (VALUES {selected_values}),
                ledger(collection_id,user_id,delta) AS (
                    SELECT t.collection_id,t.creator_id,t.amount FROM transactions t
                    JOIN selected s ON s.collection_id=t.collection_id
                    WHERE t.kind='expense' AND t.status='active'
                    UNION ALL
                    SELECT t.collection_id,es.user_id,-es.amount FROM expense_shares es
                    JOIN transactions t ON t.id=es.transaction_id
                    JOIN selected s ON s.collection_id=t.collection_id
                    WHERE t.status='active'
                    UNION ALL
                    SELECT t.collection_id,t.creator_id,t.amount FROM transactions t
                    JOIN selected s ON s.collection_id=t.collection_id
                    WHERE t.kind='repayment' AND t.status='active'
                      AND t.confirmation_status='confirmed'
                    UNION ALL
                    SELECT t.collection_id,t.counterparty_id,-t.amount FROM transactions t
                    JOIN selected s ON s.collection_id=t.collection_id
                    WHERE t.kind='repayment' AND t.status='active'
                      AND t.confirmation_status='confirmed'
                )
                SELECT collection_id,user_id,SUM(delta) balance
                FROM ledger GROUP BY collection_id,user_id
                """,
                collection_ids,
            )
            pending_rows = await connection.execute_fetchall(
                f"""
                WITH selected(collection_id) AS (VALUES {selected_values})
                SELECT t.collection_id,t.creator_id debtor_id,t.counterparty_id creditor_id,
                       SUM(t.amount) amount
                FROM transactions t JOIN selected s ON s.collection_id=t.collection_id
                WHERE t.kind='repayment' AND t.status='active'
                  AND t.confirmation_status='pending'
                GROUP BY t.collection_id,t.creator_id,t.counterparty_id
                """,
                collection_ids,
            )

        people_by_collection: dict[int, dict[int, dict]] = {}
        for row in participants:
            people_by_collection.setdefault(row["collection_id"], {})[row["id"]] = dict(row)
        balances_by_collection: dict[int, dict[int, int]] = {}
        for row in ledger:
            balances_by_collection.setdefault(row["collection_id"], {})[row["user_id"]] = row[
                "balance"
            ]
        pending_by_pair = {
            (row["collection_id"], row["debtor_id"], row["creditor_id"]): row["amount"]
            for row in pending_rows
        }

        collection_balances = []
        personal_debts = []
        for collection in collections:
            collection_id = collection["id"]
            people = people_by_collection.get(collection_id, {})
            ledger_balances = balances_by_collection.get(collection_id, {})
            balances = {
                member_id: ledger_balances.get(member_id, 0)
                for member_id, person in people.items()
                if person["active"] or ledger_balances.get(member_id, 0)
            }
            if sum(balances.values()) != 0:
                raise RuntimeError(f"Нарушена целостность балансов сбора #{collection_id}")
            collection_balances.append(
                {"collection": dict(collection), "amount": balances.get(user_id, 0)}
            )
            for debt in simplify_balances(balances):
                if user_id not in (debt.debtor_id, debt.creditor_id):
                    continue
                personal_debts.append(
                    {
                        "collection_id": collection_id,
                        "collection_title": collection["title"],
                        "currency": collection["currency"],
                        "debtor_id": debt.debtor_id,
                        "debtor_name": people[debt.debtor_id]["full_name"],
                        "debtor_username": people[debt.debtor_id]["username"],
                        "creditor_id": debt.creditor_id,
                        "creditor_name": people[debt.creditor_id]["full_name"],
                        "creditor_username": people[debt.creditor_id]["username"],
                        "amount": debt.amount,
                        "repayable_amount": max(
                            0,
                            debt.amount
                            - pending_by_pair.get(
                                (collection_id, debt.debtor_id, debt.creditor_id), 0
                            ),
                        ),
                        "pending_amount": min(
                            debt.amount,
                            pending_by_pair.get(
                                (collection_id, debt.debtor_id, debt.creditor_id), 0
                            ),
                        ),
                    }
                )
        return {"collections": collection_balances, "personal_debts": personal_debts}

    async def collection_events(self, collection_id: int, limit: int = 200):
        await self._collection(collection_id)
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(
                """
                SELECT e.*,actor.full_name actor_name,actor.username actor_username,
                       target.full_name target_name,target.username target_username
                FROM collection_events e
                JOIN users actor ON actor.id=e.actor_id
                LEFT JOIN users target ON target.id=e.target_user_id
                WHERE e.collection_id=?
                ORDER BY e.created_at DESC,e.id DESC LIMIT ?
                """,
                (collection_id, limit),
            )

    async def transaction(self, transaction_id: int):
        async with self.db.connect() as connection:
            return await _fetchone(
                connection, "SELECT * FROM transactions WHERE id=?", (transaction_id,)
            )

    async def set_repayment_confirmation_message(
        self, transaction_id: int, message_id: int
    ) -> None:
        async with self.db.connect() as connection:
            await connection.execute(
                """
                UPDATE transactions SET confirmation_message_id=?
                WHERE id=? AND kind='repayment' AND confirmation_status='pending'
                """,
                (message_id, transaction_id),
            )
            await connection.commit()

    async def cancel_transaction(self, transaction_id: int, actor_id: int) -> int:
        transaction = await self.transaction(transaction_id)
        if not transaction or transaction["status"] != "active":
            raise DomainError("Транзакция уже отменена или не найдена")
        collection = await self._collection(transaction["collection_id"])
        if actor_id not in (transaction["creator_id"], collection["admin_id"]):
            raise DomainError("Можно отменять только свои транзакции")
        if transaction["kind"] == "repayment" and transaction["confirmation_status"] == "confirmed":
            raise DomainError("Подтверждённый возврат нельзя удалить")
        if collection["status"] != "active":
            raise DomainError("Архивный сбор нельзя изменять")
        async with self.db.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            blocked = await self._transactions_with_inactive_participants_on(
                connection, [transaction_id]
            )
            if transaction_id in blocked:
                await connection.rollback()
                raise DomainError(
                    "Нельзя удалить транзакцию: один из её участников вышел из сбора"
                )
            cursor = await connection.execute(
                """
                UPDATE transactions SET status='cancelled',cancelled_by=?,
                    cancelled_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='active'
                """,
                (actor_id, transaction_id),
            )
            if cursor.rowcount != 1:
                await connection.rollback()
                raise DomainError("Транзакция уже отменена или не найдена")
            await connection.commit()
        return transaction["collection_id"]

    async def edit_transaction(
        self,
        transaction_id: int,
        actor_id: int,
        amount: int,
        comment: str,
        participant_ids: Iterable[int] | None = None,
        *,
        exact_shares: dict[int, int] | None = None,
    ) -> int:
        transaction = await self.transaction(transaction_id)
        if not transaction or transaction["status"] != "active":
            raise DomainError("Транзакция не найдена или отменена")
        collection = await self._active_collection(transaction["collection_id"])
        if actor_id not in (transaction["creator_id"], collection["admin_id"]):
            raise DomainError("Можно редактировать только свои транзакции")
        if amount <= 0:
            raise DomainError("Сумма должна быть больше нуля")
        if transaction["kind"] == "repayment" and transaction["confirmation_status"] == "confirmed":
            raise DomainError("Подтверждённый возврат нельзя редактировать")
        if len(comment.strip()) > 200:
            raise DomainError("Комментарий не должен быть длиннее 200 символов")
        expense_shares = None
        if transaction["kind"] == "expense" and exact_shares is not None:
            if participant_ids is not None:
                raise DomainError("Выберите один способ распределения")
            expense_shares = {
                int(user_id): int(share) for user_id, share in exact_shares.items()
            }
            if not expense_shares or any(share <= 0 for share in expense_shares.values()):
                raise DomainError("Сумма каждого участника должна быть больше нуля")
            await self._require_registered_members(
                transaction["collection_id"], expense_shares
            )
            if sum(expense_shares.values()) != amount:
                raise DomainError("Общая сумма не совпадает с индивидуальными суммами")
        elif transaction["kind"] == "expense" and participant_ids is not None:
            selected_ids = list(participant_ids)
            await self._require_registered_members(transaction["collection_id"], selected_ids)
            expense_shares = split_amount(amount, selected_ids)
        elif transaction["kind"] == "repayment" and (
            participant_ids is not None or exact_shares is not None
        ):
            raise DomainError("Участников можно менять только у затрат")
        if transaction["kind"] == "repayment":
            direct_debt = next(
                (
                    debt
                    for debt in await self.settlement(transaction["collection_id"])
                    if debt.debtor_id == transaction["creator_id"]
                    and debt.creditor_id == transaction["counterparty_id"]
                ),
                None,
            )
            async with self.db.connect() as connection:
                other_pending = await _fetchone(
                    connection,
                    """
                    SELECT COALESCE(SUM(amount),0) amount FROM transactions
                    WHERE collection_id=? AND kind='repayment' AND creator_id=?
                      AND counterparty_id=? AND status='active'
                      AND confirmation_status='pending' AND id<>?
                    """,
                    (
                        transaction["collection_id"],
                        transaction["creator_id"],
                        transaction["counterparty_id"],
                        transaction_id,
                    ),
                )
            available = (direct_debt.amount if direct_debt else 0) - other_pending["amount"]
            if amount > available:
                raise DomainError("Сумма возврата больше текущего долга этому участнику")
        async with self.db.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            blocked = await self._transactions_with_inactive_participants_on(
                connection, [transaction_id]
            )
            if transaction_id in blocked:
                await connection.rollback()
                raise DomainError(
                    "Нельзя изменить транзакцию: один из её участников вышел из сбора"
                )
            await connection.execute(
                "UPDATE transactions SET amount=?,comment=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (amount, comment.strip(), transaction_id),
            )
            if transaction["kind"] == "expense":
                shares = expense_shares
                if shares is None:
                    rows = await connection.execute_fetchall(
                        "SELECT user_id,amount FROM expense_shares WHERE transaction_id=? ORDER BY user_id",
                        (transaction_id,),
                    )
                    shares = (
                        {row["user_id"]: row["amount"] for row in rows}
                        if amount == transaction["amount"]
                        else split_amount(amount, [row["user_id"] for row in rows])
                    )
                if sum(shares.values()) != amount:
                    raise RuntimeError("Нарушена целостность распределения затраты")
                await connection.execute(
                    "DELETE FROM expense_shares WHERE transaction_id=?", (transaction_id,)
                )
                await connection.executemany(
                    "INSERT INTO expense_shares(transaction_id,user_id,amount) VALUES (?,?,?)",
                    [(transaction_id, user_id, share) for user_id, share in shares.items()],
                )
            await connection.commit()
        return transaction["collection_id"]

    async def archive(self, collection_id: int, actor_id: int) -> None:
        collection = await self._active_collection(collection_id)
        self._require_admin(collection, actor_id)
        async with self.db.connect() as connection:
            await connection.execute(
                "UPDATE collections SET status='archived',archived_at=CURRENT_TIMESTAMP WHERE id=?",
                (collection_id,),
            )
            await connection.execute(
                "INSERT INTO collection_events(collection_id,kind,actor_id) VALUES (?,'archived',?)",
                (collection_id, actor_id),
            )
            await connection.commit()

    async def restore(self, collection_id: int, actor_id: int) -> None:
        collection = await self._collection(collection_id)
        self._require_admin(collection, actor_id)
        if collection["status"] != "archived":
            raise DomainError("Этот сбор не находится в архиве")
        archived_at = datetime.fromisoformat(collection["archived_at"]).replace(tzinfo=UTC)
        if datetime.now(UTC) - archived_at > timedelta(days=30):
            raise DomainError("Срок восстановления (30 дней) уже истек")
        async with self.db.connect() as connection:
            await connection.execute(
                "UPDATE collections SET status='active',archived_at=NULL WHERE id=?",
                (collection_id,),
            )
            await connection.execute(
                "INSERT INTO collection_events(collection_id,kind,actor_id) VALUES (?,'restored',?)",
                (collection_id, actor_id),
            )
            await connection.commit()

    async def delete_archived(self, collection_id: int, actor_id: int) -> None:
        collection = await self._collection(collection_id)
        self._require_admin(collection, actor_id)
        if collection["status"] != "archived":
            raise DomainError("Удалить можно только сбор из архива")
        async with self.db.connect() as connection:
            cursor = await connection.execute(
                "DELETE FROM collections WHERE id=? AND status='archived'", (collection_id,)
            )
            if cursor.rowcount != 1:
                raise DomainError("Архивный сбор уже удалён или восстановлен")
            await connection.commit()

    async def expire_archives(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.connect() as connection:
            cursor = await connection.execute(
                "UPDATE collections SET status='closed' WHERE status='archived' AND archived_at<?",
                (cutoff,),
            )
            await connection.commit()
            return cursor.rowcount

    async def transfer_admin(self, collection_id: int, actor_id: int, new_admin_id: int) -> None:
        collection = await self._active_collection(collection_id)
        self._require_admin(collection, actor_id)
        await self._require_members(collection_id, [new_admin_id])
        async with self.db.connect() as connection:
            await connection.execute(
                "UPDATE collections SET admin_id=? WHERE id=?", (new_admin_id, collection_id)
            )
            await connection.execute(
                "INSERT INTO collection_events(collection_id,kind,actor_id,target_user_id) "
                "VALUES (?,'admin_transferred',?,?)",
                (collection_id, actor_id, new_admin_id),
            )
            await connection.commit()

    async def remove_participant(self, collection_id: int, actor_id: int, user_id: int) -> None:
        collection = await self._active_collection(collection_id)
        if actor_id != user_id:
            self._require_admin(collection, actor_id)
        if user_id == collection["admin_id"]:
            raise DomainError("Сначала передайте роль администратора другому участнику")
        balances = await self.get_balances(collection_id)
        if balances.get(user_id, 0) != 0:
            raise DomainError("Нельзя выйти или удалить участника с ненулевым балансом")
        async with self.db.connect() as connection:
            await connection.execute(
                """
                UPDATE participants SET active=0,notifications_enabled=0
                WHERE collection_id=? AND user_id=?
                """,
                (collection_id, user_id),
            )
            await connection.execute(
                "INSERT INTO collection_events(collection_id,kind,actor_id,target_user_id) "
                "VALUES (?,?,?,?)",
                (
                    collection_id,
                    "left" if actor_id == user_id else "member_removed",
                    actor_id,
                    user_id,
                ),
            )
            await connection.commit()

    async def collection_total(self, collection_id: int) -> int:
        async with self.db.connect() as connection:
            row = await _fetchone(
                connection,
                """
                SELECT COALESCE(SUM(amount),0) total FROM transactions
                WHERE collection_id=? AND kind='expense' AND status='active'
                """,
                (collection_id,),
            )
            return row["total"]

    async def user_by_username(self, username: str):
        async with self.db.connect() as connection:
            return await _fetchone(
                connection, "SELECT * FROM users WHERE username=?", (username.lower().lstrip("@"),)
            )

    async def _collection(self, collection_id: int):
        collection = await self.get_collection(collection_id)
        if not collection:
            raise DomainError("Сбор не найден")
        return collection

    async def _active_collection(self, collection_id: int):
        collection = await self._collection(collection_id)
        if collection["status"] != "active":
            raise DomainError("Сбор завершен и находится в архиве")
        return collection

    async def _require_member_active(self, collection_id: int, user_id: int):
        collection = await self._active_collection(collection_id)
        if not await self.is_participant(collection_id, user_id):
            raise DomainError("Сначала нажмите «Участвовать в сборе»")
        return collection

    async def _require_members(self, collection_id: int, user_ids: Iterable[int]) -> None:
        requested = set(user_ids)
        participants = {row["id"] for row in await self.list_participants(collection_id)}
        if not requested or not requested.issubset(participants):
            raise DomainError("Все выбранные люди должны участвовать в сборе")

    async def _require_registered_members(
        self, collection_id: int, user_ids: Iterable[int]
    ) -> None:
        requested = set(user_ids)
        async with self.db.connect() as connection:
            rows = await connection.execute_fetchall(
                "SELECT user_id FROM participants WHERE collection_id=?", (collection_id,)
            )
        registered = {row["user_id"] for row in rows}
        if not requested or not requested.issubset(registered):
            raise DomainError("Выберите участников этого сбора")

    @staticmethod
    def _require_admin(collection, actor_id: int) -> None:
        if collection["admin_id"] != actor_id:
            raise DomainError("Это действие доступно только администратору сбора")


def simplify_balances(balances: dict[int, int]) -> list[Debt]:
    """Turn zero-sum net balances into a short deterministic list of debts."""
    debtors = [[user_id, -amount] for user_id, amount in balances.items() if amount < 0]
    creditors = [[user_id, amount] for user_id, amount in balances.items() if amount > 0]
    debtors.sort(key=lambda item: (-item[1], item[0]))
    creditors.sort(key=lambda item: (-item[1], item[0]))
    result: list[Debt] = []
    debtor_index = creditor_index = 0
    while debtor_index < len(debtors) and creditor_index < len(creditors):
        debtor_id, debt = debtors[debtor_index]
        creditor_id, credit = creditors[creditor_index]
        amount = min(debt, credit)
        if amount:
            result.append(Debt(debtor_id, creditor_id, amount))
        debtors[debtor_index][1] -= amount
        creditors[creditor_index][1] -= amount
        if debtors[debtor_index][1] == 0:
            debtor_index += 1
        if creditors[creditor_index][1] == 0:
            creditor_index += 1
    return result
