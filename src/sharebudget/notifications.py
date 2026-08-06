from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from .service import BudgetService

LOGGER = logging.getLogger(__name__)


async def notify_subscribers(
    bot: Bot,
    service: BudgetService,
    collection,
    text: str,
    *,
    exclude_user_ids: Collection[int] = (),
    reply_markup=None,
) -> int:
    """Deliver a collection event to opted-in participants via private chat."""
    excluded = set(exclude_user_ids)
    subscribers = [
        row
        for row in await service.notification_subscribers(collection["id"])
        if row["user_id"] not in excluded
    ]

    async def deliver(row) -> bool:
        user_id = row["user_id"]
        try:
            await bot.send_message(
                user_id,
                f"🔔 <b>{escape(collection['title'])}</b>\n\n{text}",
                parse_mode="HTML",
                disable_notification=False,
                reply_markup=reply_markup,
            )
            return True
        except TelegramForbiddenError:
            await service.set_notification_subscription(collection["id"], user_id, False)
            LOGGER.info("Disabled notifications after bot was blocked by user %s", user_id)
        except TelegramAPIError:
            LOGGER.warning(
                "Could not notify subscriber %s for collection %s",
                user_id,
                collection["id"],
                exc_info=True,
            )
        return False

    if not subscribers:
        return 0
    delivered = 0
    batch_size = 20
    for offset in range(0, len(subscribers), batch_size):
        batch = subscribers[offset : offset + batch_size]
        delivered += sum(await asyncio.gather(*(deliver(row) for row in batch)))
        if offset + batch_size < len(subscribers):
            await asyncio.sleep(1)
    return delivered


async def report_collection_event(
    bot: Bot,
    service: BudgetService,
    collection,
    text: str,
    reply_markup=None,
    *,
    exclude_user_ids: Collection[int] = (),
    subscriber_reply_markup=None,
) -> tuple[bool, int]:
    """Publish an event to the linked group and opted-in private subscribers."""

    async def send_to_group() -> bool:
        if not collection["chat_id"]:
            return False
        try:
            await bot.send_message(
                collection["chat_id"],
                f"🔔 <b>{escape(collection['title'])}</b>\n\n{text}",
                parse_mode="HTML",
                disable_notification=True,
                reply_markup=reply_markup,
            )
            return True
        except TelegramAPIError:
            LOGGER.warning(
                "Could not publish collection event to chat %s",
                collection["chat_id"],
                exc_info=True,
            )
            return False

    group_sent, delivered = await asyncio.gather(
        send_to_group(),
        notify_subscribers(
            bot,
            service,
            collection,
            text,
            exclude_user_ids=exclude_user_ids,
            reply_markup=subscriber_reply_markup,
        ),
    )
    return group_sent, delivered


async def replace_repayment_prompt(
    bot: Bot,
    chat_id: int,
    message_id: int | None,
    text: str,
) -> None:
    """Remove a repayment prompt and leave one final status message in its place."""
    if message_id is not None:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="HTML",
                    reply_markup=None,
                )
                return
            except TelegramAPIError:
                LOGGER.info("Could not remove repayment prompt %s in chat %s", message_id, chat_id)
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramAPIError:
        LOGGER.info("Could not deliver final repayment status to chat %s", chat_id)
