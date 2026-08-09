from __future__ import annotations

import asyncio
import logging
import signal
import ssl
from contextlib import suppress
from html import escape

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BotCommand,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .config import Settings
from .db import Database
from .handlers import router
from .keyboards import repayment_confirmation
from .links import group_start_param
from .money import format_money
from .notifications import send_with_retry
from .render import telegram_user_link
from .service import BudgetService
from .webapp import setup_webapp_routes


async def archive_cleanup(service: BudgetService) -> None:
    while True:
        await asyncio.sleep(6 * 60 * 60)
        expired = await service.expire_archives()
        if expired:
            logging.getLogger(__name__).info("Permanently closed %s expired archives", expired)


async def dispatch_repayment_reminders_once(bot: Bot, service: BudgetService) -> int:
    reminders = await service.due_repayment_reminders()
    async def deliver(item: dict) -> bool:
        stage = item["reminder_stage"]
        heading = (
            "⏳ <b>Ожидается подтверждение возврата</b>"
            if stage == 1
            else "🔔 <b>Повторное напоминание о возврате</b>"
        )
        intro = (
            "Час назад отправитель сообщил(а) о возврате долга."
            if stage == 1
            else "Пожалуйста, подтвердите получение или отклоните возврат. Больше напоминаний не будет."
        )
        comment_line = (
            f"\nКомментарий: {escape(item['comment'])}" if item["comment"] else ""
        )
        chat_id = item["counterparty_id"]
        text = (
            f"{heading}\n\n{intro}\n\n"
            f"От: {telegram_user_link(item['creator_id'], item['creator_name'], item['creator_username'])}\n"
            f"Сумма: <b>{format_money(item['amount'], item['currency'])}</b>\n"
            f"Сбор: <b>«{escape(item['collection_title'])}»</b>{comment_line}"
        )
        markup = repayment_confirmation(item["id"])
        try:
            await send_with_retry(
                lambda chat_id=chat_id, text=text, markup=markup: bot.send_message(
                    chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    request_timeout=5,
                )
            )
            return await service.mark_repayment_reminder_sent(item["id"], stage)
        except TelegramAPIError:
            logging.getLogger(__name__).info(
                "Could not deliver repayment reminder %s stage %s",
                item["id"],
                stage,
            )
            return False

    delivered = 0
    batch_size = 20
    for offset in range(0, len(reminders), batch_size):
        delivered += sum(await asyncio.gather(*(deliver(item) for item in reminders[offset : offset + batch_size])))
        if offset + batch_size < len(reminders):
            await asyncio.sleep(1)
    return delivered


async def repayment_reminder_loop(bot: Bot, service: BudgetService) -> None:
    while True:
        try:
            delivered = await dispatch_repayment_reminders_once(bot, service)
            if delivered:
                logging.getLogger(__name__).info("Delivered %s repayment reminders", delivered)
        except Exception:
            logging.getLogger(__name__).exception("Repayment reminder cycle failed")
        await asyncio.sleep(5 * 60)


async def refresh_group_launchers(bot: Bot, service: BudgetService, settings: Settings) -> None:
    """Keep a privacy-mode-safe launch button in every known Telegram group."""
    if not settings.webapp_url:
        return
    username = settings.bot_username.lstrip("@")
    for chat_id in await service.list_known_group_chat_ids():
        start_param = group_start_param(chat_id, settings.bot_token)
        url = (
            f"https://t.me/{username}?startapp={start_param}&mode=compact"
            if settings.main_app_enabled
            else f"https://t.me/{username}?start=app"
        )
        text = "📱 Все сборы этой группы — в приложении «По рукам»."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Открыть приложение", url=url)]]
        )
        previous_id = await service.take_bot_message(chat_id, "app_link")
        if previous_id is not None:
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=previous_id,
                    reply_markup=markup,
                    request_timeout=5,
                )
                await service.replace_bot_message(chat_id, "app_link", previous_id)
                continue
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    await service.replace_bot_message(chat_id, "app_link", previous_id)
                    continue
                logging.getLogger(__name__).info(
                    "Could not refresh app launcher %s in chat %s", previous_id, chat_id
                )
            except TelegramAPIError:
                logging.getLogger(__name__).info(
                    "Could not refresh app launcher %s in chat %s", previous_id, chat_id
                )
        try:
            sent = await bot.send_message(chat_id, text, reply_markup=markup, request_timeout=5)
            await service.replace_bot_message(chat_id, "app_link", sent.message_id)
        except TelegramAPIError:
            logging.getLogger(__name__).warning(
                "Could not install app launcher in chat %s", chat_id, exc_info=True
            )


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def start_webhook(
    bot: Bot,
    dispatcher: Dispatcher,
    service: BudgetService,
    settings: Settings,
) -> None:
    if not settings.webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET is required when WEBHOOK_URL is configured")
    application = web.Application(client_max_size=128 * 1024)
    application.router.add_get("/health", health)
    setup_webapp_routes(application, bot, service, settings)
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

    runner = web.AppRunner(
        application,
        access_log_format='%a %t "%r" %s %b "%{Referer}i" "%{User-Agent}i" %Tf',
    )
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

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=AiohttpSession(timeout=10),
    )
    bot_user = await bot.me()
    settings.main_app_enabled = bool(bot_user.has_main_web_app)
    dispatcher = Dispatcher(service=service, settings=settings)
    dispatcher.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="app", description="Открыть приложение"),
        ]
    )
    if settings.webapp_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Открыть приложение",
                web_app=WebAppInfo(url=settings.webapp_url),
            )
        )
    cleanup_task = asyncio.create_task(archive_cleanup(service))
    launcher_task = asyncio.create_task(refresh_group_launchers(bot, service, settings))
    reminder_task = asyncio.create_task(repayment_reminder_loop(bot, service))
    try:
        if settings.webhook_url:
            await start_webhook(bot, dispatcher, service, settings)
        else:
            logging.getLogger(__name__).warning(
                "WEBHOOK_URL is not configured; using long polling for local development"
            )
            await dispatcher.start_polling(bot)
    finally:
        cleanup_task.cancel()
        launcher_task.cancel()
        reminder_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        with suppress(asyncio.CancelledError):
            await launcher_task
        with suppress(asyncio.CancelledError):
            await reminder_task
        await bot.session.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
