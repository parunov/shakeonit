from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType

from sharebudget.config import Settings
from sharebudget.db import Database
from sharebudget.handlers import (
    app_launch_markup,
    join_collection,
    new_collection,
    open_webapp,
    remember_group_when_bot_is_added,
    start,
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
        Settings(bot_token="123456:test-token"),
    )

    text = message.answer.await_args.args[0]
    assert 'href="tg://user?id=2"' in text
    assert "Участник без ника" in text


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

    await join_collection(callback, service)
    await join_collection(callback, service)

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
async def test_group_create_prompt_is_tracked_until_collection_is_created(tmp_path):
    database = Database(tmp_path / "create-prompt.db")
    await database.initialize()
    service = BudgetService(database)
    bot = SimpleNamespace(delete_message=AsyncMock())
    chat = SimpleNamespace(type=ChatType.SUPERGROUP, id=-100500)
    message = SimpleNamespace(
        chat=chat,
        message=SimpleNamespace(chat=chat),
        from_user=SimpleNamespace(id=7, username="owner", full_name="Владелец"),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=55)),
        bot=bot,
    )
    settings = Settings(
        bot_token="123456:test-token",
        webapp_url="https://example.com/app",
        bot_username="ShakeOnIt_bot",
        main_app_enabled=True,
    )

    await new_collection(message, SimpleNamespace(), service, settings)

    sent = message.answer.await_args
    assert "Откройте форму создания" in sent.args[0]
    assert sent.kwargs["reply_markup"].inline_keyboard[0][0].text == (
        "➕ Создать сбор в приложении"
    )
    assert await service.take_bot_message(-100500, "create_collection_prompt") == 55


@pytest.mark.asyncio
async def test_open_app_button_returns_group_scoped_main_app_link_and_removes_previous(tmp_path):
    database = Database(tmp_path / "group-link.db")
    await database.initialize()
    service = BudgetService(database)
    bot = SimpleNamespace(delete_message=AsyncMock())
    first = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.SUPERGROUP, id=-100500),
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
