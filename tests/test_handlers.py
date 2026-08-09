from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType

from sharebudget.config import Settings
from sharebudget.db import Database
from sharebudget.handlers import (
    app_launch_markup,
    join_collection,
    open_webapp,
    remember_group_when_bot_is_added,
    start,
    start_text_fallback,
    unknown_action,
)
from sharebudget.service import BudgetService


@pytest.mark.asyncio
async def test_unknown_message_is_silently_ignored():
    message = SimpleNamespace(answer=AsyncMock())

    await unknown_action(message)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_contact_deep_link_renders_bot_api_profile_link_without_username(tmp_path, monkeypatch):
    database = Database(tmp_path / "contact-link.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Владелец")
    await service.upsert_user(2, None, "Участник без ника")
    collection_id = await service.create_collection(0, "Поездка", "EUR", 1)
    await service.join(collection_id, 2)
    monkeypatch.setattr("sharebudget.handlers.sync_user", AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.PRIVATE, id=1),
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )

    await start(
        message,
        SimpleNamespace(args="contact_2"),
        service,
        Settings(
            bot_token="123456:test-token",
            webapp_url="https://example.com/app",
            main_app_enabled=True,
        ),
    )

    text = message.answer.await_args.args[0]
    message.answer.assert_awaited_once()
    assert 'href="tg://user?id=2"' in text
    assert "Участник без ника" in text


@pytest.mark.asyncio
async def test_welcome_explains_benefits_without_privacy_or_password_copy(
    tmp_path, monkeypatch
):
    database = Database(tmp_path / "welcome.db")
    await database.initialize()
    service = BudgetService(database)
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.PRIVATE, id=1),
        from_user=SimpleNamespace(id=1, username="anna", full_name="Анна"),
        answer=AsyncMock(),
    )
    monkeypatch.setattr("sharebudget.handlers.sync_user", AsyncMock())

    await start(
        message,
        SimpleNamespace(args=None),
        service,
        Settings(
            bot_token="123456:test-token",
            webapp_url="https://example.com/app",
            main_app_enabled=True,
        ),
    )

    assert message.answer.await_count == 2
    welcome = message.answer.await_args_list[0]
    launch = message.answer.await_args_list[1]
    assert "создайте сбор" in welcome.args[0].lower()
    assert "кто кому сколько" in welcome.args[0].lower()
    assert "privacy" not in welcome.args[0].lower()
    assert "парол" not in welcome.args[0].lower()
    assert not hasattr(welcome.kwargs["reply_markup"], "inline_keyboard")
    assert launch.args[0] == "📱 <b>Все сборы в одном удобном приложении</b>"
    assert launch.kwargs["reply_markup"].inline_keyboard[0][0].text == "Открыть приложение"


@pytest.mark.asyncio
async def test_plain_start_fallback_sends_welcome(tmp_path, monkeypatch):
    database = Database(tmp_path / "start-fallback.db")
    await database.initialize()
    service = BudgetService(database)
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.PRIVATE, id=1),
        from_user=SimpleNamespace(id=1, username=None, full_name="Анна"),
        answer=AsyncMock(),
    )
    monkeypatch.setattr("sharebudget.handlers.sync_user", AsyncMock())

    await start_text_fallback(
        message,
        service,
        Settings(bot_token="123456:test-token", webapp_url="https://example.com/app"),
    )

    assert message.answer.await_count == 2
    assert "Добро пожаловать" in message.answer.await_args_list[0].args[0]
    assert "Все сборы в одном удобном приложении" in message.answer.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_shared_inline_invitation_join_works_without_message_or_collection_url(tmp_path):
    database = Database(tmp_path / "inline-join.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Владелец")
    collection_id = await service.create_collection(0, "Поездка", "EUR", 1)
    callback = SimpleNamespace(
        data=f"join:{collection_id}",
        message=None,
        from_user=SimpleNamespace(id=2, username=None, full_name="Новый участник"),
        bot=SimpleNamespace(send_message=AsyncMock()),
        answer=AsyncMock(),
    )

    settings = Settings(bot_token="123456:test-token", webapp_url="https://example.com/app")
    await join_collection(callback, service, settings)
    await join_collection(callback, service, settings)

    assert await service.is_participant(collection_id, 2)
    assert callback.answer.await_count == 2
    events = await service.collection_events(collection_id, limit=20)
    assert sum(row["kind"] == "joined" and row["actor_id"] == 2 for row in events) == 1


@pytest.mark.asyncio
async def test_open_app_button_always_returns_private_mini_app_link(tmp_path):
    database = Database(tmp_path / "private-link.db")
    await database.initialize()
    service = BudgetService(database)
    bot = SimpleNamespace(delete_message=AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.PRIVATE, id=1),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        delete=AsyncMock(),
        bot=bot,
    )
    settings = Settings(
        bot_token="123456:test-token",
        webapp_url="https://example.com/app",
        bot_username="ShakeOnIt_bot",
        main_app_enabled=True,
    )

    await open_webapp(message, settings, service)

    message.answer.assert_awaited_once()
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == (
        "https://t.me/ShakeOnIt_bot?startapp=home&mode=compact"
    )
    message.delete.assert_awaited_once()


def test_create_collection_link_opens_main_mini_app_form():
    settings = Settings(
        bot_token="123456:test-token",
        webapp_url="https://example.com/app",
        bot_username="ShakeOnIt_bot",
        main_app_enabled=True,
    )

    button = app_launch_markup(settings, "create").inline_keyboard[0][0]

    assert button.url == "https://t.me/ShakeOnIt_bot?startapp=create&mode=compact"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_open_app_button_returns_group_scoped_main_app_link_and_removes_previous(tmp_path):
    database = Database(tmp_path / "group-link.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(7, "owner", "Владелец")
    bot = SimpleNamespace(delete_message=AsyncMock())
    first = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.SUPERGROUP, id=-100500, title="Друзья"),
        from_user=SimpleNamespace(id=7),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=20)),
        delete=AsyncMock(),
        bot=bot,
    )
    settings = Settings(
        bot_token="123456:test-token",
        webapp_url="https://example.com/app",
        bot_username="ShakeOnIt_bot",
        main_app_enabled=True,
    )

    await open_webapp(first, settings, service)

    second = SimpleNamespace(
        chat=first.chat,
        from_user=first.from_user,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=25)),
        delete=AsyncMock(),
        bot=bot,
    )
    await open_webapp(second, settings, service)

    second.answer.assert_awaited_once()
    button = second.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть приложение"
    assert button.url.startswith("https://t.me/ShakeOnIt_bot?startapp=chat_n100500_")
    assert button.url.endswith("&mode=compact")
    bot.delete_message.assert_awaited_once_with(-100500, 20, request_timeout=5)
    first.delete.assert_awaited_once()
    second.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_added_to_group_installs_privacy_safe_inline_launcher(tmp_path):
    database = Database(tmp_path / "group-onboarding.db")
    await database.initialize()
    service = BudgetService(database)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)))
    event = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.SUPERGROUP, id=-100700, title="Друзья"),
        from_user=SimpleNamespace(id=7, username="owner", full_name="Владелец"),
        new_chat_member=SimpleNamespace(status="member"),
        bot=bot,
    )
    settings = Settings(
        bot_token="123456:test-token",
        webapp_url="https://example.com/app",
        bot_username="ShakeOnIt_bot",
        main_app_enabled=True,
    )

    await remember_group_when_bot_is_added(event, service, settings)

    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url.startswith(
        "https://t.me/ShakeOnIt_bot?startapp=chat_n100700_"
    )
    assert await service.take_bot_message(-100700, "app_link") == 77
