from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
                "SELECT username,full_name,private_started FROM users WHERE id=?",
                (user_id,),
            )
            if existing is not None:
                if (
                    existing["username"] == normalized_username
                    and existing["full_name"] == full_name
                    and (existing["private_started"] or not private_started)
                ):
                    return False
                await connection.execute(
                    """
                    UPDATE users SET username=?,full_name=?,
                        private_started=MAX(private_started,?),updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (normalized_username, full_name, int(private_started), user_id),
                )
            else:
                await connection.execute(
                    "INSERT INTO users(id,username,full_name,private_started) VALUES (?,?,?,?)",
                    (user_id, normalized_username, full_name, int(private_started)),
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

    async def get_user(self, user_id: int):
        async with self.db.connect() as connection:
            return await _fetchone(connection, "SELECT * FROM users WHERE id=?", (user_id,))

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
        async with self.db.connect() as connection:
            if bank_name is None:
                await connection.execute(
                    "UPDATE users SET payment_details=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (details.strip(), user_id),
                )
            else:
                await connection.execute(
                    """
                    UPDATE users SET payment_details=?,bank_name=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (details.strip(), bank_name.strip(), user_id),
                )
            await connection.commit()

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
                "INSERT INTO participants(collection_id,user_id) VALUES (?,?)",
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

    async def join(self, collection_id: int, user_id: int, subscribe: bool = False) -> None:
        collection = await self._active_collection(collection_id)
        async with self.db.connect() as connection:
            existing = await _fetchone(
                connection,
                "SELECT active FROM participants WHERE collection_id=? AND user_id=?",
                (collection_id, user_id),
            )
            await connection.execute(
                """
                INSERT INTO participants(
                    collection_id,user_id,active,notifications_enabled
                ) VALUES (?,?,1,?)
                ON CONFLICT(collection_id,user_id) DO UPDATE SET
                    active=1,
                    notifications_enabled=MAX(
                        participants.notifications_enabled,excluded.notifications_enabled
                    )
                """,
                (collection["id"], user_id, int(subscribe)),
            )
            if not existing or not existing["active"]:
                await connection.execute(
                    "INSERT INTO collection_events(collection_id,kind,actor_id,target_user_id) "
                    "VALUES (?,'joined',?,?)",
                    (collection_id, user_id, user_id),
                )
            await connection.commit()

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
                UPDATE participants SET notifications_enabled=?
                WHERE collection_id=? AND user_id=? AND active=1
                """,
                (int(enabled), collection_id, user_id),
            )
            await connection.commit()

    async def notification_subscribers(self, collection_id: int):
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(
                """
                SELECT p.user_id,u.full_name FROM participants p
                JOIN users u ON u.id=p.user_id
                WHERE p.collection_id=? AND p.active=1 AND p.notifications_enabled=1
                ORDER BY p.user_id
                """,
                (collection_id,),
            )

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
        self, collection_id: int, user_id: int, history_limit: int = 100, events_limit: int = 200
    ) -> CollectionView | None:
        """Load the complete collection screen through one SQLite connection."""
        async with self.db.connect() as connection:
            collection = await _fetchone(
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
            subscription = await _fetchone(
                connection,
                """
                SELECT notifications_enabled FROM participants
                WHERE collection_id=? AND user_id=? AND active=1
                """,
                (collection_id, user_id),
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
        return CollectionView(
            collection=collection,
            snapshot=snapshot,
            history=history,
            events=events,
            shares=shares,
            notifications_enabled=bool(subscription and subscription["notifications_enabled"]),
            pending_repayments={row["counterparty_id"]: row["amount"] for row in pending_rows},
        )

    async def add_expense(
        self,
        collection_id: int,
        creator_id: int,
        amount: int,
        participant_ids: Iterable[int],
        comment: str,
    ) -> int:
        if amount <= 0:
            raise DomainError("Сумма должна быть больше нуля")
        shares = split_amount(amount, list(participant_ids))
        if sum(shares.values()) != amount:
            raise RuntimeError("Нарушена целостность распределения затраты")
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
        transaction = await self.transaction(transaction_id)
        if not transaction or transaction["kind"] != "repayment":
            raise DomainError("Возврат долга не найден")
        if transaction["status"] != "active":
            raise DomainError("Возврат долга отменён")
        if transaction["confirmation_status"] == "confirmed":
            raise DomainError("Получение уже подтверждено")
        if transaction["counterparty_id"] != actor_id:
            raise DomainError("Подтвердить получение может только получатель")
        await self._require_member_active(transaction["collection_id"], actor_id)
        direct_debt = next(
            (
                debt
                for debt in await self.settlement(transaction["collection_id"])
                if debt.debtor_id == transaction["creator_id"]
                and debt.creditor_id == transaction["counterparty_id"]
            ),
            None,
        )
        if direct_debt is None or transaction["amount"] > direct_debt.amount:
            raise DomainError("Баланс изменился: этот возврат больше нельзя подтвердить")
        async with self.db.connect() as connection:
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

    async def global_history(
        self,
        user_id: int,
        limit: int = 20,
        transaction_offset: int = 0,
        event_offset: int = 0,
    ):
        async with self.db.connect() as connection:
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

    async def expense_statistics(self, user_id: int) -> dict:
        async with self.db.connect() as connection:
            currency_rows = await connection.execute_fetchall(
                """
                SELECT c.currency,
                       SUM(t.amount) total_amount,
                       SUM(CASE WHEN t.created_at >= datetime('now','start of month')
                                THEN t.amount ELSE 0 END) monthly_amount,
                       COUNT(*) total_count,
                       SUM(t.created_at >= datetime('now','start of month')) monthly_count
                FROM transactions t JOIN collections c ON c.id=t.collection_id
                WHERE t.creator_id=? AND t.kind='expense' AND t.status='active'
                GROUP BY c.currency ORDER BY c.currency
                """,
                (user_id,),
            )
            collection_rows = await connection.execute_fetchall(
                """
                SELECT c.id collection_id,c.title,c.currency,SUM(t.amount) amount,COUNT(*) count
                FROM transactions t JOIN collections c ON c.id=t.collection_id
                WHERE t.creator_id=? AND t.kind='expense' AND t.status='active'
                GROUP BY c.id,c.title,c.currency
                ORDER BY amount DESC,c.title LIMIT 10
                """,
                (user_id,),
            )
        return {
            "monthly_by_currency": {
                row["currency"]: row["monthly_amount"] or 0 for row in currency_rows
            },
            "total_by_currency": {
                row["currency"]: row["total_amount"] or 0 for row in currency_rows
            },
            "monthly_count": sum(row["monthly_count"] or 0 for row in currency_rows),
            "total_count": sum(row["total_count"] or 0 for row in currency_rows),
            "by_collection": [dict(row) for row in collection_rows],
        }

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

        people_by_collection: dict[int, dict[int, dict]] = {}
        for row in participants:
            people_by_collection.setdefault(row["collection_id"], {})[row["id"]] = dict(row)
        balances_by_collection: dict[int, dict[int, int]] = {}
        for row in ledger:
            balances_by_collection.setdefault(row["collection_id"], {})[row["user_id"]] = row[
                "balance"
            ]

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
        if (
            transaction["kind"] == "repayment"
            and transaction["confirmation_status"] == "confirmed"
            and actor_id != collection["admin_id"]
        ):
            raise DomainError("Подтверждённый возврат может отменить только администратор сбора")
        if collection["status"] != "active":
            raise DomainError("Архивный сбор нельзя изменять")
        async with self.db.connect() as connection:
            await connection.execute(
                """
                UPDATE transactions SET status='cancelled',cancelled_by=?,
                    cancelled_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (actor_id, transaction_id),
            )
            await connection.commit()
        return transaction["collection_id"]

    async def edit_transaction(
        self,
        transaction_id: int,
        actor_id: int,
        amount: int,
        comment: str,
        participant_ids: Iterable[int] | None = None,
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
        if transaction["kind"] == "expense" and participant_ids is not None:
            selected_ids = list(participant_ids)
            await self._require_registered_members(transaction["collection_id"], selected_ids)
            expense_shares = split_amount(amount, selected_ids)
        elif transaction["kind"] == "repayment" and participant_ids is not None:
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
            await connection.execute(
                "UPDATE transactions SET amount=?,comment=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (amount, comment.strip(), transaction_id),
            )
            if transaction["kind"] == "expense":
                shares = expense_shares
                if shares is None:
                    rows = await connection.execute_fetchall(
                        "SELECT user_id FROM expense_shares WHERE transaction_id=? ORDER BY user_id",
                        (transaction_id,),
                    )
                    shares = split_amount(amount, [row["user_id"] for row in rows])
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
