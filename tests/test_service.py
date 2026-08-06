from pathlib import Path

import pytest

from sharebudget.db import Database
from sharebudget.money import split_amount
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
async def test_user_is_authorized_on_first_telegram_interaction(service):
    assert await service.upsert_user(99, "new_user", "Новый участник") is True
    assert await service.upsert_user(99, "new_user", "Новый участник") is False


@pytest.mark.asyncio
async def test_preferred_balance_currency_is_validated_and_saved(service):
    await service.set_preferred_currency(1, "EUR")
    assert (await service.get_user(1))["preferred_currency"] == "EUR"
    with pytest.raises(DomainError, match="Неподдерживаемая валюта"):
        await service.set_preferred_currency(1, "BTC")


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

    repayment_id = await service.add_repayment(collection_id, 2, 1, 3000)
    assert await service.get_balances(collection_id) == {1: 6000, 2: -3000, 3: -3000}
    await service.confirm_repayment(repayment_id, 1)
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
async def test_private_notification_subscription_follows_active_membership(service):
    collection_id = await make_collection(service)

    assert await service.notification_subscription(collection_id, 2) is False
    await service.set_notification_subscription(collection_id, 2, True)
    assert await service.notification_subscription(collection_id, 2) is True
    assert [row["user_id"] for row in await service.notification_subscribers(collection_id)] == [2]

    await service.remove_participant(collection_id, 2, 2)
    assert await service.notification_subscription(collection_id, 2) is False
    await service.join(collection_id, 2, subscribe=True)
    assert await service.notification_subscription(collection_id, 2) is True


@pytest.mark.asyncio
async def test_personal_collection_is_not_exposed_as_telegram_chat(service):
    personal_id = await service.create_collection(0, "Личный подарок", "BYN", 1)

    collection = await service.get_collection(personal_id)
    chats = await service.list_user_collection_chats(1)

    assert collection["chat_id"] == 0
    assert all(row["chat_id"] != 0 for row in chats)


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
async def test_edit_expense_changes_participants_and_resplits_exactly(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 1, 1000, [1, 2, 3], "Еда")

    await service.edit_transaction(
        transaction_id,
        1,
        1001,
        "Ужин",
        participant_ids=[2, 3],
    )

    assert await service.get_balances(collection_id) == {1: 1001, 2: -501, 3: -500}
    shares = (await service.expense_shares_for_transactions([transaction_id]))[transaction_id]
    assert [(row["user_id"], row["amount"]) for row in shares] == [(2, 501), (3, 500)]


@pytest.mark.asyncio
async def test_edit_expense_rejects_unknown_participant_without_mutation(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 1, 1000, [1, 2], "Еда")

    with pytest.raises(DomainError):
        await service.edit_transaction(
            transaction_id,
            1,
            1500,
            "Ужин",
            participant_ids=[1, 999],
        )

    transaction = await service.transaction(transaction_id)
    assert transaction["amount"] == 1000
    assert transaction["comment"] == "Еда"


@pytest.mark.asyncio
async def test_request_funds_targets_only_current_debtors_and_has_cooldown(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 900, [1, 2, 3], "Ужин")

    debts = await service.request_funds(collection_id, 1)

    assert [(debt.debtor_id, debt.creditor_id, debt.amount) for debt in debts] == [
        (2, 1, 300),
        (3, 1, 300),
    ]
    assert (await service.collection_events(collection_id))[0]["kind"] == "funds_requested"
    with pytest.raises(DomainError, match="Повторить можно"):
        await service.request_funds(collection_id, 1)
    with pytest.raises(DomainError, match="никто не должен"):
        await service.request_funds(collection_id, 2)


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
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500)
    await service.confirm_repayment(repayment_id, 1)
    await service.remove_participant(collection_id, 2, 2)
    assert [row["id"] for row in await service.list_participants(collection_id)] == [1, 3]
    assert len(await service.history(collection_id)) == 2


@pytest.mark.asyncio
async def test_cancel_after_member_left_keeps_former_member_in_balances(service):
    collection_id = await make_collection(service)
    expense_id = await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500)
    await service.confirm_repayment(repayment_id, 1)
    await service.remove_participant(collection_id, 2, 2)

    await service.cancel_transaction(expense_id, 1)
    snapshot = await service.collection_snapshot(collection_id)

    former = next(row for row in snapshot.participants if row["id"] == 2)
    assert former["active"] == 0
    assert snapshot.balances == {1: -500, 2: 500, 3: 0}
    assert snapshot.debts[0].debtor_id == 1
    assert snapshot.debts[0].creditor_id == 2


@pytest.mark.asyncio
async def test_repayment_cannot_exceed_current_debt(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    with pytest.raises(DomainError):
        await service.add_repayment(collection_id, 2, 1, 501)


@pytest.mark.asyncio
async def test_only_receiver_can_confirm_and_pending_does_not_change_balance(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1001, [1, 2, 3], "Ужин")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 334)

    assert await service.get_balances(collection_id) == {1: 667, 2: -334, 3: -333}
    with pytest.raises(DomainError, match="только получатель"):
        await service.confirm_repayment(repayment_id, 2)

    await service.confirm_repayment(repayment_id, 1)
    assert await service.get_balances(collection_id) == {1: 333, 2: 0, 3: -333}
    transaction = await service.transaction(repayment_id)
    assert transaction["confirmation_status"] == "confirmed"


@pytest.mark.asyncio
async def test_pending_repayments_cannot_overbook_debt(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    await service.add_repayment(collection_id, 2, 1, 300)
    with pytest.raises(DomainError, match="больше текущего долга"):
        await service.add_repayment(collection_id, 2, 1, 201)


@pytest.mark.asyncio
async def test_expense_share_breakdown_is_exact(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 1, 1001, [3, 1, 2], "Билеты")
    shares = (await service.expense_shares_for_transactions([transaction_id]))[transaction_id]
    assert sum(row["amount"] for row in shares) == 1001
    assert sorted(row["amount"] for row in shares) == [333, 334, 334]
    assert sum((await service.get_balances(collection_id)).values()) == 0


@pytest.mark.asyncio
async def test_payer_can_exclude_self_without_losing_paid_amount(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1001, [2, 3], "Билеты друзьям")

    assert await service.get_balances(collection_id) == {1: 1001, 2: -501, 3: -500}
    assert sum((await service.get_balances(collection_id)).values()) == 0


def test_split_amount_is_exact_for_all_small_rounding_cases():
    for amount in range(1, 250):
        for participant_count in range(1, 8):
            shares = split_amount(amount, range(1, participant_count + 1))
            assert sum(shares.values()) == amount
            assert max(shares.values()) - min(shares.values()) <= 1


@pytest.mark.asyncio
async def test_global_history_contains_transactions_and_collection_events(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1200, [1, 2, 3], "Музей")

    transactions, events = await service.global_history(2)

    assert transactions[0]["collection_title"] == "Берлин"
    assert transactions[0]["amount"] == 1200
    assert {row["kind"] for row in events} >= {"created", "joined"}

    collection_events = await service.collection_events(collection_id)
    assert {row["kind"] for row in collection_events} >= {"created", "joined"}
    assert all(row["actor_name"] for row in collection_events)


def test_simplify_balances_is_deterministic_and_exact():
    debts = simplify_balances({1: 700, 2: 300, 3: -400, 4: -600})
    assert sum(debt.amount for debt in debts) == 1000
    assert [(d.debtor_id, d.creditor_id, d.amount) for d in debts] == [
        (4, 1, 600),
        (3, 1, 100),
        (3, 2, 300),
    ]
