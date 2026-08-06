from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType

from sharebudget.config import Settings
from sharebudget.db import Database
from sharebudget.handlers import open_webapp
from sharebudget.service import BudgetService


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
    )

    await open_webapp(message, settings, service)

    message.answer.assert_awaited_once()
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].web_app.url == "https://example.com/app"
    message.delete.assert_awaited_once()


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
    assert button.text == "📱 Запустить приложение"
    assert button.url.startswith("https://t.me/ShakeOnIt_bot?startapp=chat_n100500_")
    assert button.url.endswith("&mode=compact")
    bot.delete_message.assert_awaited_once_with(-100500, 20)
    first.delete.assert_awaited_once()
    second.delete.assert_awaited_once()
