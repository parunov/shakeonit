from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import Settings
from .db import Database
from .handlers import router
from .service import BudgetService


async def archive_cleanup(service: BudgetService) -> None:
    while True:
        await asyncio.sleep(6 * 60 * 60)
        expired = await service.expire_archives()
        if expired:
            logging.getLogger(__name__).info("Permanently closed %s expired archives", expired)


async def main() -> None:
    settings = Settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    database = Database(settings.database_path)
    await database.initialize()
    service = BudgetService(database)
    expired = await service.expire_archives()
    if expired:
        logging.getLogger(__name__).info("Permanently closed %s expired archives", expired)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(service=service)
    dispatcher.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="menu", description="Главное меню"),
            BotCommand(command="new", description="Создать сбор в группе"),
            BotCommand(command="collections", description="Мои сборы"),
            BotCommand(command="expense", description="Добавить затрату"),
            BotCommand(command="repay", description="Вернуть долг"),
            BotCommand(command="balance", description="Мой баланс"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
        ]
    )
    cleanup_task = asyncio.create_task(archive_cleanup(service))
    try:
        await dispatcher.start_polling(bot)
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await bot.session.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
