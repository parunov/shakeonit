from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sharebudget.db import Database
from sharebudget.main import dispatch_repayment_reminders_once
from sharebudget.service import BudgetService


@pytest.mark.asyncio
async def test_repayment_reminder_is_sent_with_actions_and_not_duplicated(tmp_path):
    database = Database(tmp_path / "reminders.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "recipient", "Получатель")
    await service.upsert_user(2, "sender", "Отправитель")
    collection_id = await service.create_collection(0, "Поездка", "EUR", 1)
    await service.join(collection_id, 2)
    await service.add_expense(collection_id, 1, 1000, [1, 2], "Билеты")
    repayment_id = await service.add_repayment(collection_id, 2, 1, 500, "За билеты")
    created_at = datetime.now(UTC) - timedelta(hours=2)
    async with service.db.connect() as connection:
        await connection.execute(
            "UPDATE transactions SET created_at=? WHERE id=?",
            (created_at.strftime("%Y-%m-%d %H:%M:%S"), repayment_id),
        )
        await connection.commit()
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))

    assert await dispatch_repayment_reminders_once(bot, service) == 1
    assert await dispatch_repayment_reminders_once(bot, service) == 0

    bot.send_message.assert_awaited_once()
    call = bot.send_message.await_args
    assert call.args[0] == 1
    assert "Ожидается подтверждение возврата" in call.args[1]
    assert "Отправитель" in call.args[1]
    assert "Поездка" in call.args[1]
    callbacks = {
        button.callback_data
        for row in call.kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert callbacks == {f"repayconfirm:{repayment_id}", f"repayreject:{repayment_id}"}

