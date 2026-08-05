from __future__ import annotations

import asyncio
import logging
import signal
import ssl
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, FSInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

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


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def start_webhook(
    bot: Bot,
    dispatcher: Dispatcher,
    settings: Settings,
) -> None:
    if not settings.webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET is required when WEBHOOK_URL is configured")
    application = web.Application()
    application.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        handle_in_background=True,
        secret_token=settings.webhook_secret,
    ).register(application, path=settings.webhook_path)
    setup_application(application, dispatcher, bot=bot)

    ssl_context = None
    webhook_certificate = None
    if settings.webhook_cert_path or settings.webhook_key_path:
        if not settings.webhook_cert_path or not settings.webhook_key_path:
            raise RuntimeError("Both WEBHOOK_CERT_PATH and WEBHOOK_KEY_PATH are required")
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(settings.webhook_cert_path, settings.webhook_key_path)
        webhook_certificate = FSInputFile(settings.webhook_cert_path)

    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=settings.webhook_host,
        port=settings.webhook_port,
        ssl_context=ssl_context,
    )
    await site.start()
    await bot.set_webhook(
        url=settings.webhook_url,
        certificate=webhook_certificate,
        secret_token=settings.webhook_secret,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )
    logging.getLogger(__name__).info(
        "Webhook started on %s:%s%s",
        settings.webhook_host,
        settings.webhook_port,
        settings.webhook_path,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(stop_signal, stop_event.set)
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


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
            BotCommand(command="collections", description="Сборы"),
            BotCommand(command="expense", description="Добавить затрату"),
            BotCommand(command="repay", description="Вернуть долг"),
            BotCommand(command="balance", description="Мой баланс"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
        ]
    )
    cleanup_task = asyncio.create_task(archive_cleanup(service))
    try:
        if settings.webhook_url:
            await start_webhook(bot, dispatcher, settings)
        else:
            logging.getLogger(__name__).warning(
                "WEBHOOK_URL is not configured; using long polling for local development"
            )
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
