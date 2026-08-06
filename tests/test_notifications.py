from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sharebudget.notifications import report_collection_event


@pytest.mark.asyncio
async def test_collection_event_reaches_group_and_private_subscribers():
    service = SimpleNamespace(
        notification_subscribers=AsyncMock(
            return_value=[{"user_id": 1}, {"user_id": 2}, {"user_id": 3}]
        ),
        set_notification_subscription=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    collection = {"id": 7, "chat_id": -100500, "title": "Поездка"}

    group_sent, delivered = await report_collection_event(
        bot,
        service,
        collection,
        "Статус обновлён",
        exclude_user_ids={1},
    )

    assert group_sent is True
    assert delivered == 2
    assert sorted(call.args[0] for call in bot.send_message.await_args_list) == [-100500, 2, 3]
    assert all("Поездка" in call.args[1] for call in bot.send_message.await_args_list)
