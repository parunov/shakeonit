import asyncio
from datetime import UTC, datetime, timedelta
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
async def test_shared_collection_user_cannot_be_enumerated_outside_common_collection(service):
    collection_id = await service.create_collection(-100, "Общий сбор", "EUR", 1)
    await service.join(collection_id, 2)

    assert (await service.get_shared_collection_user(1, 2))["full_name"] == "Борис"
    assert not await service.get_shared_collection_user(1, 3)


@pytest.mark.asyncio
async def test_preferred_balance_currency_is_validated_and_saved(service):
    await service.set_preferred_currency(1, "EUR")
    assert (await service.get_user(1))["preferred_currency"] == "EUR"
    with pytest.raises(DomainError, match="Неподдерживаемая валюта"):
        await service.set_preferred_currency(1, "BTC")


@pytest.mark.asyncio
async def test_legacy_payment_update_preserves_bank_name(service):
    await service.set_payment_details(1, "Телефон", "Мой банк")
    await service.set_payment_details(1, "Новая карта")

    user = await service.get_user(1)
    assert user["bank_name"] == "Мой банк"
    assert user["payment_details"] == "Новая карта"


@pytest.mark.asyncio
async def test_custom_name_survives_later_telegram_sync(service):
    await service.set_display_name(1, "Анна для друзей")
    await service.upsert_user(1, "anna_new", "Имя из Telegram")

    user = await service.get_user(1)
    assert user["full_name"] == "Анна для друзей"
    assert user["username"] == "anna_new"


@pytest.mark.asyncio
async def test_multiple_payment_methods_replace_legacy_primary(service):
    await service.replace_payment_methods(
        1,
        [
            {"bank_name": "Банк A", "details": "Карта 1111"},
            {"bank_name": "Банк B", "details": "+375291234567"},
        ],
    )

    methods = [dict(row) for row in await service.list_payment_methods(1)]
    assert [(row["bank_name"], row["details"]) for row in methods] == [
        ("Банк A", "Карта 1111"),
        ("Банк B", "+375291234567"),
    ]
    user = await service.get_user(1)
    assert (user["bank_name"], user["payment_details"]) == ("Банк A", "Карта 1111")


@pytest.mark.asyncio
async def test_notification_categories_filter_subscribers(service):
    collection_id = await make_collection(service)
    await service.set_notification_subscription(collection_id, 2, True)
    await service.set_notification_preferences(
        2,
        {
            "notify_expenses": False,
            "notify_repayments": True,
            "notify_collection_events": True,
            "notify_reminders": False,
        },
    )

    assert await service.notification_subscribers(collection_id, "expenses") == []
    assert [row["user_id"] for row in await service.notification_subscribers(
        collection_id, "repayments"
    )] == [2]


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
async def test_pending_repayment_confirmation_is_exposed_only_until_decision(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Билеты")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500, "Перевод")

    prompt = await service.pending_repayment_confirmation(1)
    assert prompt["id"] == repayment_id
    assert prompt["creator_name"] == "Борис"
    assert prompt["collection_title"] == "Берлин"
    assert await service.pending_repayment_confirmation(2) is None

    await service.confirm_repayment(repayment_id, 1)
    assert await service.pending_repayment_confirmation(1) is None


@pytest.mark.asyncio
async def test_repayment_reminders_are_due_once_at_one_hour_and_next_day(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Билеты")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500, "Перевод")
    created_at = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
    async with service.db.connect() as connection:
        await connection.execute(
            "UPDATE transactions SET created_at=? WHERE id=?",
            (created_at.strftime("%Y-%m-%d %H:%M:%S"), repayment_id),
        )
        await connection.commit()

    assert await service.due_repayment_reminders(created_at + timedelta(minutes=59)) == []
    first = await service.due_repayment_reminders(created_at + timedelta(hours=1))
    assert [(row["id"], row["reminder_stage"]) for row in first] == [(repayment_id, 1)]
    assert await service.mark_repayment_reminder_sent(repayment_id, 1) is True
    assert await service.mark_repayment_reminder_sent(repayment_id, 1) is False
    async with service.db.connect() as connection:
        await connection.execute(
            "UPDATE transactions SET confirmation_reminder_1_sent_at=? WHERE id=?",
            ((created_at + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"), repayment_id),
        )
        await connection.commit()

    # 10:00 in Minsk is 07:00 UTC on the next calendar day.
    assert await service.due_repayment_reminders(datetime(2026, 1, 6, 6, 59, tzinfo=UTC)) == []
    second = await service.due_repayment_reminders(datetime(2026, 1, 6, 7, 0, tzinfo=UTC))
    assert [(row["id"], row["reminder_stage"]) for row in second] == [(repayment_id, 2)]
    assert await service.mark_repayment_reminder_sent(repayment_id, 2) is True
    assert await service.due_repayment_reminders(datetime(2026, 1, 7, 7, 0, tzinfo=UTC)) == []


@pytest.mark.asyncio
async def test_expense_statistics_separates_personal_share_paid_and_repaid(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 2, 1200, [1, 2, 3], "Обед")
    repayment_id = await service.add_repayment(collection_id, 1, 2, 400, "Моя доля")
    await service.confirm_repayment(repayment_id, 2)
    await service.add_expense(collection_id, 1, 600, [2, 3], "Такси")

    stats = await service.expense_statistics(1)

    assert stats["total_personal_by_currency"] == {"EUR": 400}
    assert stats["total_paid_by_currency"] == {"EUR": 600}
    assert stats["total_repaid_by_currency"] == {"EUR": 400}
    assert stats["by_collection"][0]["personal_amount"] == 400
    assert stats["by_collection"][0]["paid_amount"] == 600
    assert stats["by_collection"][0]["repaid_amount"] == 400


@pytest.mark.asyncio
async def test_expense_statistics_uses_minsk_month_and_confirmed_repayments(service):
    collection_id = await make_collection(service)
    july_expense = await service.add_expense(collection_id, 2, 500, [1], "До полуночи")
    august_expense = await service.add_expense(collection_id, 2, 300, [1], "После полуночи")
    repayment_id = await service.add_repayment(collection_id, 1, 2, 100, "Возврат")
    await service.confirm_repayment(repayment_id, 2)
    pending_id = await service.add_repayment(collection_id, 1, 2, 100, "Ожидает")

    async with service.db.connect() as connection:
        await connection.execute(
            "UPDATE transactions SET created_at=? WHERE id=?",
            ("2026-07-31 20:30:00", july_expense),
        )
        await connection.execute(
            "UPDATE transactions SET created_at=? WHERE id=?",
            ("2026-07-31 21:30:00", august_expense),
        )
        await connection.execute(
            "UPDATE transactions SET confirmed_at=? WHERE id=?",
            ("2026-07-31 21:45:00", repayment_id),
        )
        await connection.execute(
            "UPDATE transactions SET created_at=? WHERE id=?",
            ("2026-07-31 22:00:00", pending_id),
        )
        await connection.commit()

    stats = await service.expense_statistics(
        1, now=datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    )

    assert stats["total_personal_by_currency"] == {"EUR": 800}
    assert stats["monthly_personal_by_currency"] == {"EUR": 300}
    assert stats["monthly_personal_count"] == 1
    assert stats["monthly_repaid_by_currency"] == {"EUR": 100}
    assert stats["monthly_repaid_count"] == 1


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
async def test_group_join_enables_notifications_after_private_chat_started(service):
    collection_id = await make_collection(service)
    await service.remove_participant(collection_id, 2, 2)
    await service.upsert_user(2, "boris", "Борис", private_started=True)

    await service.join(collection_id, 2, subscribe=False)

    assert await service.notification_subscription(collection_id, 2) is True


@pytest.mark.asyncio
async def test_starting_private_chat_enables_existing_unconfigured_subscriptions(service):
    collection_id = await make_collection(service)
    assert await service.notification_subscription(collection_id, 2) is False

    await service.upsert_user(2, "boris", "Борис", private_started=True)

    assert await service.notification_subscription(collection_id, 2) is True


@pytest.mark.asyncio
async def test_explicitly_disabled_collection_notifications_stay_disabled_on_rejoin(service):
    collection_id = await make_collection(service)
    await service.upsert_user(2, "boris", "Борис", private_started=True)
    await service.set_notification_subscription(collection_id, 2, False)
    await service.remove_participant(collection_id, 2, 2)

    await service.join(collection_id, 2, subscribe=False)

    assert await service.notification_subscription(collection_id, 2) is False


@pytest.mark.asyncio
async def test_join_is_idempotent_and_records_one_membership_event(service):
    collection_id = await service.create_collection(-100, "Подарок", "BYN", 1)

    assert await service.join(collection_id, 2) is True
    assert await service.join(collection_id, 2) is False

    events = await service.collection_events(collection_id, limit=20)
    assert sum(row["kind"] == "joined" and row["actor_id"] == 2 for row in events) == 1


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
async def test_custom_expense_shares_are_kept_exactly(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(
        collection_id,
        1,
        1000,
        [1, 2, 3],
        "Разные суммы",
        exact_shares={1: 100, 2: 300, 3: 600},
    )

    assert await service.get_balances(collection_id) == {1: 900, 2: -300, 3: -600}
    shares = (await service.expense_shares_for_transactions([transaction_id]))[transaction_id]
    assert sorted((row["user_id"], row["amount"]) for row in shares) == [
        (1, 100),
        (2, 300),
        (3, 600),
    ]


@pytest.mark.asyncio
async def test_confirmed_repayment_cannot_be_cancelled_even_by_admin(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500)
    await service.confirm_repayment(repayment_id, 1)

    with pytest.raises(DomainError, match="нельзя удалить"):
        await service.cancel_transaction(repayment_id, 1)


@pytest.mark.asyncio
async def test_archived_or_deleted_collection_transactions_cannot_change(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    await service.archive(collection_id, 1)

    with pytest.raises(DomainError, match="Архивный сбор"):
        await service.cancel_transaction(transaction_id, 1)
    with pytest.raises(DomainError, match="архиве"):
        await service.edit_transaction(transaction_id, 1, 1200, "Новое такси")

    await service.delete_archived(collection_id, 1)
    assert await service.transaction(transaction_id) is None
    with pytest.raises(DomainError, match="не найдена"):
        await service.cancel_transaction(transaction_id, 1)


@pytest.mark.asyncio
async def test_member_cannot_cancel_someone_elses_transaction(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 2, 1000, [1, 2], "Такси")
    with pytest.raises(DomainError):
        await service.cancel_transaction(transaction_id, 3)


@pytest.mark.asyncio
async def test_transaction_cannot_be_edited_or_cancelled_after_participant_left(service):
    collection_id = await make_collection(service)
    expense_id = await service.add_expense(collection_id, 1, 1000, [2], "Билеты")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 1000, "Расчёт")
    await service.confirm_repayment(repayment_id, 1)
    await service.remove_participant(collection_id, 2, 2)

    assert await service.transactions_with_inactive_participants([expense_id]) == {expense_id}
    with pytest.raises(DomainError, match="участников вышел"):
        await service.edit_transaction(expense_id, 1, 1200, "Новая сумма")
    with pytest.raises(DomainError, match="участников вышел"):
        await service.cancel_transaction(expense_id, 1)


@pytest.mark.asyncio
async def test_edit_expense_resplits_exactly(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 1, 1000, [1, 2, 3], "Еда")
    await service.edit_transaction(transaction_id, 1, 1001, "Ужин")
    assert await service.get_balances(collection_id) == {1: 667, 2: -334, 3: -333}
    assert sum((await service.get_balances(collection_id)).values()) == 0


@pytest.mark.asyncio
async def test_edit_custom_expense_comment_preserves_exact_shares(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(
        collection_id,
        1,
        1000,
        [],
        "Еда",
        exact_shares={1: 100, 2: 300, 3: 600},
    )

    await service.edit_transaction(transaction_id, 1, 1000, "Ужин")

    shares = (await service.expense_shares_for_transactions([transaction_id]))[transaction_id]
    assert [(row["user_id"], row["amount"]) for row in shares] == [
        (1, 100),
        (2, 300),
        (3, 600),
    ]


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
async def test_cancel_after_member_left_is_blocked_and_balances_stay_closed(service):
    collection_id = await make_collection(service)
    expense_id = await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500)
    await service.confirm_repayment(repayment_id, 1)
    await service.remove_participant(collection_id, 2, 2)

    with pytest.raises(DomainError, match="участников вышел"):
        await service.cancel_transaction(expense_id, 1)
    snapshot = await service.collection_snapshot(collection_id)

    assert all(row["id"] != 2 for row in snapshot.participants)
    assert snapshot.balances == {1: 0, 3: 0}
    assert snapshot.debts == []


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
async def test_only_receiver_can_reject_pending_repayment(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500, "Перевод на карту")

    with pytest.raises(DomainError, match="только получатель"):
        await service.reject_repayment(repayment_id, 2)

    await service.reject_repayment(repayment_id, 1)
    transaction = await service.transaction(repayment_id)
    assert transaction["status"] == "cancelled"
    assert transaction["cancelled_by"] == 1
    assert await service.get_balances(collection_id) == {1: 500, 2: -500, 3: 0}

    with pytest.raises(DomainError, match="отклонён или отменён"):
        await service.reject_repayment(repayment_id, 1)


@pytest.mark.asyncio
async def test_pending_repayments_cannot_overbook_debt(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    await service.add_repayment(collection_id, 2, 1, 300)
    with pytest.raises(DomainError, match="больше текущего долга"):
        await service.add_repayment(collection_id, 2, 1, 201)


@pytest.mark.asyncio
async def test_concurrent_confirmations_cannot_overpay_changed_debt(service):
    collection_id = await make_collection(service)
    expense_id = await service.add_expense(collection_id, 1, 1000, [2], "Такси")
    first = await service.add_repayment(collection_id, 2, 1, 400)
    second = await service.add_repayment(collection_id, 2, 1, 400)
    await service.edit_transaction(expense_id, 1, 500, "Такси")

    results = await asyncio.gather(
        service.confirm_repayment(first, 1),
        service.confirm_repayment(second, 1),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, DomainError) for result in results) == 1
    assert await service.get_balances(collection_id) == {1: 100, 2: -100, 3: 0}


@pytest.mark.asyncio
async def test_balance_overview_separates_pending_and_repayable_amount(service):
    collection_id = await make_collection(service)
    await service.add_expense(collection_id, 1, 1000, [2], "Такси")
    await service.add_repayment(collection_id, 2, 1, 400)

    overview = await service.balance_overview(2)
    debt = overview["personal_debts"][0]

    assert debt["amount"] == 1000
    assert debt["pending_amount"] == 400
    assert debt["repayable_amount"] == 600


@pytest.mark.asyncio
async def test_sync_token_changes_only_when_visible_data_changes(service):
    collection_id = await make_collection(service)
    initial = await service.sync_token(1)

    await service.upsert_user(1, "anna", "Анна")
    unchanged = await service.sync_token(1)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    changed = await service.sync_token(1)

    assert unchanged == initial
    assert changed != initial


@pytest.mark.asyncio
async def test_only_admin_can_permanently_delete_archived_collection(service):
    collection_id = await make_collection(service)
    transaction_id = await service.add_expense(collection_id, 1, 1200, [1, 2, 3], "Музей")

    with pytest.raises(DomainError, match="только сбор из архива"):
        await service.delete_archived(collection_id, 1)

    await service.archive(collection_id, 1)
    with pytest.raises(DomainError, match="администратору"):
        await service.delete_archived(collection_id, 2)

    await service.delete_archived(collection_id, 1)

    assert await service.get_collection(collection_id) is None
    assert await service.transaction(transaction_id) is None


@pytest.mark.asyncio
async def test_current_bot_message_replaces_previous_message(service):
    assert await service.replace_bot_message(-100, "app_link", 10) is None
    assert await service.replace_bot_message(-100, "app_link", 15) == 10
    assert await service.replace_bot_message(-200, "app_link", 7) is None
    assert await service.take_bot_message(-100, "app_link") == 15
    assert await service.take_bot_message(-100, "app_link") is None


@pytest.mark.asyncio
async def test_bot_messages_can_be_taken_by_kind_prefix(service):
    await service.replace_bot_message(1, "repayment_prompt:7:sender", 10)
    await service.replace_bot_message(1, "repayment_prompt:7:event", 11)
    await service.replace_bot_message(2, "repayment_prompt:7:confirmation", 12)
    await service.replace_bot_message(1, "repayment_prompt:8:event", 20)

    messages = await service.take_bot_messages_by_prefix("repayment_prompt:7:")

    assert sorted(messages) == [(1, 10), (1, 11), (2, 12)]
    assert await service.take_bot_message(1, "repayment_prompt:8:event") == 20


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
    assert transactions[0]["collection_admin_id"] == 1
    assert transactions[0]["collection_status"] == "active"
    assert transactions[0]["is_participant"] == 1
    assert events[0]["collection_id"] == collection_id
    assert events[0]["is_participant"] == 1
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
