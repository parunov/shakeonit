import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sharebudget.config import Settings
from sharebudget.db import Database
from sharebudget.links import group_start_param
from sharebudget.service import BudgetService
from sharebudget.webapp import (
    FX_CACHE_KEY,
    FX_CACHE_SECONDS,
    FX_LOCK_KEY,
    ApiError,
    _collection_invite_markup,
    exchange_rates,
    setup_webapp_routes,
    validate_init_data,
)

TOKEN = "123456:test-token"


def signed_init_data(
    *, auth_date: int | None = None, user: dict | None = None, start_param: str | None = None
) -> str:
    data = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAEAAAE",
        "signature": "telegram-ed25519-signature",
        "user": json.dumps(
            user or {"id": 42, "first_name": "Анна", "username": "anna"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if start_param:
        data["start_param"] = start_param
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_validate_init_data_accepts_authentic_telegram_payload():
    result = validate_init_data(signed_init_data(), TOKEN)

    assert result["user"]["id"] == 42
    assert result["user"]["first_name"] == "Анна"


def test_validate_init_data_rejects_tampering():
    raw = signed_init_data().replace("anna", "ivan")

    with pytest.raises(ApiError, match="подтвердить вход"):
        validate_init_data(raw, TOKEN)


def test_validate_init_data_rejects_expired_session():
    raw = signed_init_data(auth_date=int(time.time()) - 90000)

    with pytest.raises(ApiError, match="Сессия устарела"):
        validate_init_data(raw, TOKEN, max_age=86400)


def test_validate_init_data_rejects_duplicate_fields():
    raw = signed_init_data() + "&user=%7B%7D"

    with pytest.raises(ApiError, match="Некорректные данные"):
        validate_init_data(raw, TOKEN)


def test_collection_invite_uses_safe_fallback_or_enabled_main_app():
    settings = Settings(bot_token=TOKEN, main_app_enabled=False)
    buttons = [
        button for row in _collection_invite_markup(settings, 42).inline_keyboard for button in row
    ]

    assert not any(button.url and "startapp" in button.url for button in buttons)
    assert any(button.url and "start=app" in button.url for button in buttons)
    assert any(button.callback_data == "join:42" for button in buttons)

    settings.main_app_enabled = True
    enabled_buttons = [
        button for row in _collection_invite_markup(settings, 42).inline_keyboard for button in row
    ]
    assert any(button.url and "startapp=collection_42" in button.url for button in enabled_buttons)
    assert not any(button.url and "start=app" in button.url for button in enabled_buttons)


@pytest.mark.asyncio
async def test_webapp_serves_ui_and_authenticates_api(tmp_path):
    database = Database(tmp_path / "miniapp.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Организатор")
    collection_id = await service.create_collection(-100500, "Поездка", "EUR", 1)
    settings = Settings(bot_token=TOKEN, database_path=database.path)
    application = web.Application()
    setup_webapp_routes(application, object(), service, settings)

    async with TestClient(TestServer(application)) as client:
        page = await client.get("/app")
        assert page.status == 200
        page_text = await page.text()
        assert "ShakeOnIt" in page_text
        assert 'name="telegram-bot-username" content="ShakeOnIt_bot"' in page_text
        assert "__BOT_USERNAME__" not in page_text

        script = await client.get("/app/static/app.js")
        assert script.status == 200
        assert script.headers["Cache-Control"] == "no-cache"
        assert "delete-collection" in await script.text()

        unauthorized = await client.get("/api/bootstrap")
        assert unauthorized.status == 401

        authorized = await client.get(
            "/api/bootstrap",
            headers={"X-Telegram-Init-Data": signed_init_data()},
        )
        payload = await authorized.json()
        assert authorized.status == 200
        assert payload["user"]["id"] == 42
        assert payload["user"]["preferred_currency"] == "BYN"
        assert payload["main_app_enabled"] is False
        assert payload["is_new_user"] is True
        assert payload["sync_version"]

        sync = await client.get(
            "/api/sync",
            headers={"X-Telegram-Init-Data": signed_init_data()},
        )
        sync_payload = await sync.json()
        assert sync.status == 200
        assert sync_payload["sync_version"] == payload["sync_version"]

        currency = await client.patch(
            "/api/me/currency",
            json={"currency": "USD"},
            headers={"X-Telegram-Init-Data": signed_init_data()},
        )
        assert currency.status == 200
        assert (await service.get_user(42))["preferred_currency"] == "USD"

        group_context = await client.get(
            "/api/bootstrap",
            headers={
                "X-Telegram-Init-Data": signed_init_data(
                    start_param=group_start_param(-100500, TOKEN)
                )
            },
        )
        group_payload = await group_context.json()
        assert group_payload["context_chat_id"] == -100500
        assert group_payload["collections"][0]["id"] == collection_id
        assert group_payload["collections"][0]["is_participant"] is False

        forged_context = await client.get(
            "/api/bootstrap",
            headers={
                "X-Telegram-Init-Data": signed_init_data(start_param="chat_n100500_deadbeef0000")
            },
        )
        forged_payload = await forged_context.json()
        assert forged_payload["context_chat_id"] is None
        assert forged_payload["collections"] == []

        invitation = await client.get(
            "/api/bootstrap",
            headers={
                "X-Telegram-Init-Data": signed_init_data(start_param=f"collection_{collection_id}")
            },
        )
        invitation_payload = await invitation.json()
        assert invitation_payload["invitation"]["collection"]["id"] == collection_id
        assert invitation_payload["invitation"]["is_participant"] is False


@pytest.mark.asyncio
async def test_collection_api_survives_cancel_after_member_left(tmp_path):
    database = Database(tmp_path / "former-member.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Организатор")
    await service.upsert_user(2, "former", "Бывший участник")
    collection_id = await service.create_collection(-100500, "Поездка", "EUR", 1)
    await service.join(collection_id, 2)
    expense_id = await service.add_expense(collection_id, 1, 1000, [1, 2], "Такси")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500)
    await service.confirm_repayment(repayment_id, 1)
    await service.remove_participant(collection_id, 2, 2)
    await service.cancel_transaction(expense_id, 1)

    settings = Settings(bot_token=TOKEN, database_path=database.path)
    application = web.Application()
    setup_webapp_routes(application, object(), service, settings)
    owner_auth = signed_init_data(user={"id": 1, "first_name": "Организатор"})

    async with TestClient(TestServer(application)) as client:
        response = await client.get(
            f"/api/collections/{collection_id}",
            headers={"X-Telegram-Init-Data": owner_auth},
        )
        payload = await response.json()

    assert response.status == 200
    former = next(member for member in payload["participants"] if member["id"] == 2)
    assert former["active"] is False
    assert payload["debts"][0]["creditor_name"] == "Бывший участник"


@pytest.mark.asyncio
async def test_edit_expense_api_replaces_participants(tmp_path):
    database = Database(tmp_path / "edit-expense.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Организатор")
    await service.upsert_user(2, "anna", "Анна")
    await service.upsert_user(3, "max", "Максим")
    collection_id = await service.create_collection(-100500, "Поездка", "EUR", 1)
    await service.join(collection_id, 2)
    await service.join(collection_id, 3)
    transaction_id = await service.add_expense(collection_id, 1, 1000, [1, 2, 3], "Ужин")
    settings = Settings(bot_token=TOKEN, database_path=database.path)
    bot = SimpleNamespace(send_message=AsyncMock())
    application = web.Application()
    setup_webapp_routes(application, bot, service, settings)
    owner_auth = signed_init_data(user={"id": 1, "first_name": "Организатор"})

    async with TestClient(TestServer(application)) as client:
        response = await client.patch(
            f"/api/transactions/{transaction_id}",
            json={
                "amount": "10,01",
                "comment": "Поздний ужин",
                "participant_ids": [2, 3],
            },
            headers={"X-Telegram-Init-Data": owner_auth},
        )
        response_payload = await response.json()
        details = await client.get(
            f"/api/collections/{collection_id}",
            headers={"X-Telegram-Init-Data": owner_auth},
        )
        details_payload = await details.json()

    assert response.status == 200
    assert response_payload["report_sent"] is True
    edited = next(row for row in details_payload["history"] if row["id"] == transaction_id)
    assert [(row["user_id"], row["amount"]) for row in edited["shares"]] == [
        (2, 501),
        (3, 500),
    ]
    report = bot.send_message.await_args_list[0].args[1]
    assert "Поездка" in report
    assert "Было:" in report and "Стало:" in report
    assert "Ужин" in report and "Поздний ужин" in report
    assert "Организатор" in report and "Анна" in report and "Максим" in report
    assert f"#{transaction_id}" not in report


@pytest.mark.asyncio
async def test_repayment_can_be_confirmed_from_global_history(tmp_path):
    database = Database(tmp_path / "confirm-from-history.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Получатель")
    await service.upsert_user(2, "debtor", "Отправитель")
    collection_id = await service.create_collection(-100500, "Поездка", "EUR", 1)
    await service.join(collection_id, 2)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Билеты")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500, "За билеты")
    settings = Settings(bot_token=TOKEN, database_path=database.path)
    bot = SimpleNamespace(send_message=AsyncMock())
    application = web.Application()
    setup_webapp_routes(application, bot, service, settings)
    recipient_auth = signed_init_data(user={"id": 1, "first_name": "Получатель"})

    async with TestClient(TestServer(application)) as client:
        before = await client.get(
            "/api/history",
            headers={"X-Telegram-Init-Data": recipient_auth},
        )
        before_payload = await before.json()
        pending = next(row for row in before_payload["transactions"] if row["id"] == repayment_id)

        response = await client.post(
            f"/api/transactions/{repayment_id}/confirm",
            json={},
            headers={"X-Telegram-Init-Data": recipient_auth},
        )
        response_payload = await response.json()

        after = await client.get(
            "/api/history",
            headers={"X-Telegram-Init-Data": recipient_auth},
        )
        after_payload = await after.json()
        confirmed = next(row for row in after_payload["transactions"] if row["id"] == repayment_id)

    assert before.status == 200
    assert pending["confirmation_status"] == "pending"
    assert pending["counterparty_id"] == 1
    assert pending["is_participant"] == 1
    assert response.status == 200
    assert response_payload["report_sent"] is True
    assert confirmed["confirmation_status"] == "confirmed"
    confirmation_message = bot.send_message.await_args_list[0].args[1]
    assert "Отправитель" in confirmation_message
    assert "За билеты" in confirmation_message
    assert "Поездка" in confirmation_message
    assert f"#{repayment_id}" not in confirmation_message


@pytest.mark.asyncio
async def test_repayment_notification_can_be_rejected_by_recipient(tmp_path):
    database = Database(tmp_path / "reject-repayment.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "recipient", "Получатель")
    await service.upsert_user(2, "sender", "Отправитель")
    collection_id = await service.create_collection(-100500, "Поездка", "EUR", 1)
    await service.join(collection_id, 2)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Билеты")
    settings = Settings(bot_token=TOKEN, database_path=database.path)
    bot = SimpleNamespace(send_message=AsyncMock())
    application = web.Application()
    setup_webapp_routes(application, bot, service, settings)
    sender_auth = signed_init_data(user={"id": 2, "first_name": "Отправитель"})
    recipient_auth = signed_init_data(user={"id": 1, "first_name": "Получатель"})

    async with TestClient(TestServer(application)) as client:
        created = await client.post(
            f"/api/collections/{collection_id}/repayments",
            json={"creditor_id": 1, "amount": "5", "comment": "Перевод на карту"},
            headers={"X-Telegram-Init-Data": sender_auth},
        )
        created_payload = await created.json()
        repayment_id = created_payload["transaction_id"]
        private_call = next(call for call in bot.send_message.await_args_list if call.args[0] == 1)
        private_message = private_call.args[1]
        callbacks = {
            button.callback_data
            for row in private_call.kwargs["reply_markup"].inline_keyboard
            for button in row
        }

        rejected = await client.post(
            f"/api/transactions/{repayment_id}/reject",
            json={},
            headers={"X-Telegram-Init-Data": recipient_auth},
        )
        rejected_payload = await rejected.json()
        history = await client.get(
            "/api/history",
            headers={"X-Telegram-Init-Data": recipient_auth},
        )
        history_payload = await history.json()
        transaction = next(
            row for row in history_payload["transactions"] if row["id"] == repayment_id
        )

    assert created.status == 200
    assert "От: Отправитель" in private_message
    assert "Комментарий: Перевод на карту" in private_message
    assert "Сбор: Поездка" in private_message
    assert f"#{repayment_id}" not in private_message
    assert callbacks == {f"repayconfirm:{repayment_id}", f"repayreject:{repayment_id}"}
    assert rejected.status == 200
    assert rejected_payload["report_sent"] is True
    assert transaction["status"] == "cancelled"


@pytest.mark.asyncio
async def test_request_funds_notifies_each_debtor_and_reports_to_group(tmp_path):
    database = Database(tmp_path / "request-funds.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Организатор")
    await service.upsert_user(2, "anna", "Анна")
    await service.upsert_user(3, "max", "Максим")
    await service.set_payment_details(1, "Карта •• 1234")
    collection_id = await service.create_collection(-100500, "Поездка", "EUR", 1)
    await service.join(collection_id, 2)
    await service.join(collection_id, 3)
    await service.add_expense(collection_id, 1, 900, [1, 2, 3], "Ужин")
    settings = Settings(bot_token=TOKEN, database_path=database.path)
    settings.main_app_enabled = True
    bot = SimpleNamespace(send_message=AsyncMock())
    application = web.Application()
    setup_webapp_routes(application, bot, service, settings)
    owner_auth = signed_init_data(user={"id": 1, "first_name": "Организатор"})

    async with TestClient(TestServer(application)) as client:
        response = await client.post(
            f"/api/collections/{collection_id}/request-funds",
            json={},
            headers={"X-Telegram-Init-Data": owner_auth},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload == {
        "ok": True,
        "debtors_count": 2,
        "notifications_sent": 2,
        "failed_count": 0,
        "report_sent": True,
    }
    recipients = [call.args[0] for call in bot.send_message.await_args_list]
    assert recipients == [2, 3, -100500]
    assert "Карта •• 1234" in bot.send_message.await_args_list[0].args[1]
    assert (
        "startapp=collection_"
        in bot.send_message.await_args_list[0].kwargs["reply_markup"].inline_keyboard[0][0].url
    )


@pytest.mark.asyncio
async def test_nbrb_rates_are_cached_for_thirty_minutes(monkeypatch):
    calls = 0

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def raise_for_status(self):
            return None

        async def json(self):
            return [
                {"Cur_Abbreviation": "USD", "Cur_OfficialRate": 3.1, "Cur_Scale": 1},
                {"Cur_Abbreviation": "EUR", "Cur_OfficialRate": 3.4, "Cur_Scale": 1},
                {"Cur_Abbreviation": "RUB", "Cur_OfficialRate": 3.6, "Cur_Scale": 100},
            ]

    class FakeSession:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def get(self, _url):
            nonlocal calls
            calls += 1
            return FakeResponse()

    monkeypatch.setattr("sharebudget.webapp.ClientSession", FakeSession)
    request = SimpleNamespace(
        app={FX_CACHE_KEY: {}, FX_LOCK_KEY: asyncio.Lock()},
    )

    first = await exchange_rates(request)
    second = await exchange_rates(request)

    assert first.status == 200
    assert second.status == 200
    assert FX_CACHE_SECONDS == 30 * 60
    assert calls == 1


@pytest.mark.asyncio
async def test_archived_collection_can_be_deleted_by_admin_from_mini_app(tmp_path):
    database = Database(tmp_path / "delete-archive.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Организатор")
    collection_id = await service.create_collection(-100500, "Старый сбор", "EUR", 1)
    await service.archive(collection_id, 1)
    settings = Settings(bot_token=TOKEN, database_path=database.path)
    bot = SimpleNamespace(send_message=AsyncMock())
    application = web.Application()
    setup_webapp_routes(application, bot, service, settings)
    owner_auth = signed_init_data(user={"id": 1, "first_name": "Организатор"})

    async with TestClient(TestServer(application)) as client:
        response = await client.delete(
            f"/api/collections/{collection_id}",
            headers={"X-Telegram-Init-Data": owner_auth},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["report_sent"] is True
    assert await service.get_collection(collection_id) is None
    assert "безвозвратно удалил" in bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_join_can_enable_private_collection_notifications(tmp_path):
    database = Database(tmp_path / "notifications.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Организатор")
    collection_id = await service.create_collection(-100500, "Поездка", "EUR", 1)
    settings = Settings(bot_token=TOKEN, database_path=database.path)
    bot = SimpleNamespace(send_message=AsyncMock())
    application = web.Application()
    setup_webapp_routes(application, bot, service, settings)
    member_auth = signed_init_data(user={"id": 2, "first_name": "Участник"})

    async with TestClient(TestServer(application)) as client:
        response = await client.post(
            f"/api/collections/{collection_id}/join",
            json={"subscribe": True},
            headers={"X-Telegram-Init-Data": member_auth},
        )
        payload = await response.json()

    assert response.status == 200
    assert payload["notifications_enabled"] is True
    assert await service.notification_subscription(collection_id, 2) is True
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_personal_collection_uses_bot_notifications_without_group(tmp_path):
    database = Database(tmp_path / "personal-collection.db")
    await database.initialize()
    service = BudgetService(database)
    settings = Settings(bot_token=TOKEN, database_path=database.path)
    bot = SimpleNamespace(send_message=AsyncMock())
    application = web.Application()
    setup_webapp_routes(application, bot, service, settings)
    owner_auth = signed_init_data(user={"id": 7, "first_name": "Владелец"})

    async with TestClient(TestServer(application)) as client:
        response = await client.post(
            "/api/collections",
            json={"chat_id": 0, "title": "Без группы", "currency": "BYN", "subscribe": True},
            headers={"X-Telegram-Init-Data": owner_auth},
        )
        payload = await response.json()
        details = await client.get(
            f"/api/collections/{payload['collection_id']}",
            headers={"X-Telegram-Init-Data": owner_auth},
        )
        details_payload = await details.json()

    assert response.status == 200
    assert payload["report_sent"] is False
    assert payload["notifications_enabled"] is True
    assert details_payload["collection"]["is_personal"] is True
    assert details_payload["events"][0]["kind"] == "created"
    bot.send_message.assert_awaited_once()
