from pathlib import Path

import pytest

from sharebudget.db import Database
from sharebudget.service import BudgetService, DomainError, simplify_balances


@pytest.fixture
async def service(tmp_path: Path):
    database = Database(tmp_path / "test.db")
    await database.initialize()
    result = BudgetService(database)
    await result.upsert_user(1, "anna", "Анна")
    await result.upsert_user(2, "boris", "Борис")
    await result.upsert_user(3, "clara", "Клара")
    return result


async def make_collection(service):
    collection_id = await service.create_collection(-100, "Берлин", "EUR", 1)
    await service.join(collection_id, 2)
    await service.join(collection_id, 3)
    return collection_id


@pytest.mark.asyncio
async def test_private_chat_authorization_is_explicit_and_monotonic(service):
    await service.upsert_user(1, "anna", "Анна", private_started=False)
    assert not await service.has_started_private_chat(1)

    await service.upsert_user(1, "anna", "Анна", private_started=True)
    assert await service.has_started_private_chat(1)


@pytest.mark.asyncio
async def test_shared_group_becomes_available_for_mini_app_collection(service):
    await service.upsert_user(1, "anna", "Анна", private_started=True)
    await service.register_user_chat(1, -100123, "Друзья")

    chats = await service.list_user_collection_chats(1)

    assert chats == [
        {
            "chat_id": -100123,
            "reference_title": "Друзья",
            "last_seen": chats[0]["last_seen"],
        }
    ]
    assert await service.can_create_in_chat(1, -100123)

    await service.upsert_user(1, "anna", "Анна", private_started=False)
    assert await service.has_started_private_chat(1)


@pytest.mark.asyncio
async def test_expense_and_repayment_update_balances(service):
    collection_id = await make_collection(service)
    expense_id = await service.add_expense(collection_id, 1, 9000, [1, 2, 3], "Отель")
    assert expense_id > 0
    assert await service.get_balances(collection_id) == {1: 6000, 2: -3000, 3: -3000}

    await service.add_repayment(collection_id, 2, 1, 3000)
    assert await service.get_balances(collection_id) == {1: 3000, 2: 0, 3: -3000}
    debts = await service.settlement(collection_id)
    assert [(d.debtor_id, d.creditor_id, d.amount) for d in debts] == [(3, 1, 3000)]

    snapshot = await service.collection_snapshot(collection_id)
    assert snapshot.total == 9000
    assert snapshot.balances == {1: 3000, 2: 0, 3: -3000}
    assert [(d.debtor_id, d.creditor_id, d.amount) for d in snapshot.debts] == [(3, 1, 3000)]


@pytest.mark.asyncio
async def test_visible_collections_include_group_catalog_and_user_collections(service):
    own_group_collection = await service.create_collection(-100, "Берлин", "EUR", 1)
    available_group_collection = await service.create_collection(-100, "Подарок", "EUR", 2)
    own_other_group_collection = await service.create_collection(-200, "Варшава", "USD", 3)
    await service.join(own_other_group_collection, 1)

    group_rows = await service.list_visible_collections(1, -100)
    assert [row["id"] for row in group_rows] == [
        own_group_collection,
        available_group_collection,
        own_other_group_collection,
    ]
    assert [row["is_participant"] for row in group_rows] == [1, 0, 1]

    private_rows = await service.list_visible_collections(1)
    assert {row["id"] for row in private_rows} == {
        own_group_collection,
        own_other_group_collection,
    }


@pytest.mark.asyncio
async def test_cancel_removes_effect_but_keeps_history(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 2, 1000, [1, 2], "Такси")
    await service.cancel_transaction(transaction_id, 1)  # collection admin can cancel any
    assert await service.get_balances(collection_id) == {1: 0, 2: 0, 3: 0}
    history = await service.history(collection_id)
    assert history[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_member_cannot_cancel_someone_elses_transaction(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 2, 1000, [1, 2], "Такси")
    with pytest.raises(DomainError):
        await service.cancel_transaction(transaction_id, 3)


@pytest.mark.asyncio
async def test_edit_expense_resplits_exactly(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 1, 1000, [1, 2, 3], "Еда")
    await service.edit_transaction(transaction_id, 1, 1001, "Ужин")
    assert await service.get_balances(collection_id) == {1: 667, 2: -334, 3: -333}
    assert sum((await service.get_balances(collection_id)).values()) == 0


@pytest.mark.asyncio
async def test_cannot_leave_with_balance(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    with pytest.raises(DomainError):
        await service.remove_participant(collection_id, 2, 2)


@pytest.mark.asyncio
async def test_leave_keeps_transaction_history(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    await service.add_repayment(collection_id, 2, 1, 500)
    await service.remove_participant(collection_id, 2, 2)
    assert [row["id"] for row in await service.list_participants(collection_id)] == [1, 3]
    assert len(await service.history(collection_id)) == 2


@pytest.mark.asyncio
async def test_repayment_cannot_exceed_current_debt(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    with pytest.raises(DomainError):
        await service.add_repayment(collection_id, 2, 1, 501)


def test_simplify_balances_is_deterministic_and_exact():
    debts = simplify_balances({1: 700, 2: 300, 3: -400, 4: -600})
    assert sum(debt.amount for debt in debts) == 1000
    assert [(d.debtor_id, d.creditor_id, d.amount) for d in debts] == [
        (4, 1, 600),
        (3, 1, 100),
        (3, 2, 300),
    ]
