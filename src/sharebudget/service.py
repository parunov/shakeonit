from __future__ import annotations

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


async def _fetchone(connection, query: str, params=()):
    cursor = await connection.execute(query, params)
    return await cursor.fetchone()


class BudgetService:
    def __init__(self, database: Database):
        self.db = database

    async def upsert_user(self, user_id: int, username: str | None, full_name: str) -> None:
        async with self.db.connect() as connection:
            await connection.execute(
                """
                INSERT INTO users(id, username, full_name) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET username=excluded.username,
                    full_name=excluded.full_name, updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, username.lower() if username else None, full_name),
            )
            await connection.commit()

    async def set_payment_details(self, user_id: int, details: str) -> None:
        if len(details) > 500:
            raise DomainError("Платежные данные не должны быть длиннее 500 символов")
        async with self.db.connect() as connection:
            await connection.execute(
                "UPDATE users SET payment_details=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (details.strip(), user_id),
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

    async def list_collections(
        self, user_id: int, chat_id: int | None = None, include_archived: bool = True
    ):
        statuses = "('active','archived')" if include_archived else "('active')"
        query = f"""
            SELECT c.*,
                (SELECT COUNT(*) FROM participants p2
                 WHERE p2.collection_id=c.id AND p2.active=1) participants_count
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

    async def join(self, collection_id: int, user_id: int) -> None:
        collection = await self._active_collection(collection_id)
        async with self.db.connect() as connection:
            await connection.execute(
                """
                INSERT INTO participants(collection_id,user_id,active) VALUES (?,?,1)
                ON CONFLICT(collection_id,user_id) DO UPDATE SET active=1
                """,
                (collection["id"], user_id),
            )
            await connection.commit()

    async def list_participants(self, collection_id: int):
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(
                """
                SELECT u.id,u.username,u.full_name,u.payment_details,p.joined_at,
                       c.admin_id=u.id AS is_admin
                FROM participants p JOIN users u ON u.id=p.user_id
                JOIN collections c ON c.id=p.collection_id
                WHERE p.collection_id=? AND p.active=1 ORDER BY is_admin DESC,u.full_name
                """,
                (collection_id,),
            )

    async def add_expense(
        self,
        collection_id: int,
        creator_id: int,
        amount: int,
        participant_ids: Iterable[int],
        comment: str,
    ) -> int:
        await self._require_member_active(collection_id, creator_id)
        shares = split_amount(amount, list(participant_ids))
        await self._require_members(collection_id, shares)
        if len(comment.strip()) > 200:
            raise DomainError("Комментарий не должен быть длиннее 200 символов")
        async with self.db.connect() as connection:
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
        await self._require_member_active(collection_id, debtor_id)
        await self._require_members(collection_id, [creditor_id])
        if debtor_id == creditor_id:
            raise DomainError("Нельзя вернуть долг самому себе")
        direct_debt = next(
            (
                debt
                for debt in await self.settlement(collection_id)
                if debt.debtor_id == debtor_id and debt.creditor_id == creditor_id
            ),
            None,
        )
        if direct_debt is None:
            raise DomainError("По текущему балансу вы не должны этому участнику")
        if amount > direct_debt.amount:
            raise DomainError("Сумма возврата больше текущего долга этому участнику")
        if len(comment.strip()) > 200:
            raise DomainError("Комментарий не должен быть длиннее 200 символов")
        async with self.db.connect() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO transactions(
                    collection_id,kind,creator_id,counterparty_id,amount,comment
                ) VALUES (?,'repayment',?,?,?,?)
                """,
                (collection_id, debtor_id, creditor_id, amount, comment.strip()),
            )
            await connection.commit()
            return int(cursor.lastrowid)

    async def get_balances(self, collection_id: int) -> dict[int, int]:
        participants = await self.list_participants(collection_id)
        balances = {row["id"]: 0 for row in participants}
        async with self.db.connect() as connection:
            expenses = await connection.execute_fetchall(
                """
                SELECT t.creator_id,t.amount FROM transactions t
                WHERE t.collection_id=? AND t.kind='expense' AND t.status='active'
                """,
                (collection_id,),
            )
            shares = await connection.execute_fetchall(
                """
                SELECT s.user_id,s.amount FROM expense_shares s
                JOIN transactions t ON t.id=s.transaction_id
                WHERE t.collection_id=? AND t.status='active'
                """,
                (collection_id,),
            )
            repayments = await connection.execute_fetchall(
                """
                SELECT creator_id,counterparty_id,amount FROM transactions
                WHERE collection_id=? AND kind='repayment' AND status='active'
                """,
                (collection_id,),
            )
        for row in expenses:
            balances.setdefault(row["creator_id"], 0)
            balances[row["creator_id"]] += row["amount"]
        for row in shares:
            balances.setdefault(row["user_id"], 0)
            balances[row["user_id"]] -= row["amount"]
        for row in repayments:
            balances.setdefault(row["creator_id"], 0)
            balances.setdefault(row["counterparty_id"], 0)
            balances[row["creator_id"]] += row["amount"]
            balances[row["counterparty_id"]] -= row["amount"]
        return balances

    async def settlement(self, collection_id: int) -> list[Debt]:
        balances = await self.get_balances(collection_id)
        return simplify_balances(balances)

    async def history(self, collection_id: int, limit: int = 50, offset: int = 0):
        await self._collection(collection_id)
        async with self.db.connect() as connection:
            return await connection.execute_fetchall(
                """
                SELECT t.*, creator.full_name creator_name, creator.username creator_username,
                    counterparty.full_name counterparty_name,
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

    async def transaction(self, transaction_id: int):
        async with self.db.connect() as connection:
            return await _fetchone(
                connection, "SELECT * FROM transactions WHERE id=?", (transaction_id,)
            )

    async def cancel_transaction(self, transaction_id: int, actor_id: int) -> int:
        transaction = await self.transaction(transaction_id)
        if not transaction or transaction["status"] != "active":
            raise DomainError("Транзакция уже отменена или не найдена")
        collection = await self._collection(transaction["collection_id"])
        if actor_id not in (transaction["creator_id"], collection["admin_id"]):
            raise DomainError("Можно отменять только свои транзакции")
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
        self, transaction_id: int, actor_id: int, amount: int, comment: str
    ) -> int:
        transaction = await self.transaction(transaction_id)
        if not transaction or transaction["status"] != "active":
            raise DomainError("Транзакция не найдена или отменена")
        collection = await self._active_collection(transaction["collection_id"])
        if actor_id not in (transaction["creator_id"], collection["admin_id"]):
            raise DomainError("Можно редактировать только свои транзакции")
        if len(comment.strip()) > 200:
            raise DomainError("Комментарий не должен быть длиннее 200 символов")
        async with self.db.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "UPDATE transactions SET amount=?,comment=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (amount, comment.strip(), transaction_id),
            )
            if transaction["kind"] == "expense":
                rows = await connection.execute_fetchall(
                    "SELECT user_id FROM expense_shares WHERE transaction_id=? ORDER BY user_id",
                    (transaction_id,),
                )
                shares = split_amount(amount, [row["user_id"] for row in rows])
                await connection.executemany(
                    "UPDATE expense_shares SET amount=? WHERE transaction_id=? AND user_id=?",
                    [(share, transaction_id, user_id) for user_id, share in shares.items()],
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
                "UPDATE participants SET active=0 WHERE collection_id=? AND user_id=?",
                (collection_id, user_id),
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
