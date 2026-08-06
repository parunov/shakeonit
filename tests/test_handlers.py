from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType

from sharebudget.config import Settings
from sharebudget.handlers import open_webapp


@pytest.mark.asyncio
async def test_open_app_button_always_returns_private_mini_app_link():
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.PRIVATE, id=1),
        answer=AsyncMock(),
    )
    settings = Settings(
        bot_token="123456:test-token",
        webapp_url="https://example.com/app",
    )

    await open_webapp(message, settings)

    message.answer.assert_awaited_once()
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].web_app.url == "https://example.com/app"


@pytest.mark.asyncio
async def test_open_app_button_returns_group_scoped_main_app_link():
    message = SimpleNamespace(
        chat=SimpleNamespace(type=ChatType.SUPERGROUP, id=-100500),
        answer=AsyncMock(),
    )
    settings = Settings(
        bot_token="123456:test-token",
        webapp_url="https://example.com/app",
        bot_username="ShakeOnIt_bot",
        main_app_enabled=True,
    )

    await open_webapp(message, settings)

    message.answer.assert_awaited_once()
    button = message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.url.startswith("https://t.me/ShakeOnIt_bot?startapp=chat_n100500_")
    assert button.url.endswith("&mode=compact")
