from __future__ import annotations

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
    delivered = 0
    for row in await service.notification_subscribers(collection["id"]):
        user_id = row["user_id"]
        if user_id in excluded:
            continue
        try:
            await bot.send_message(
                user_id,
                f"🔔 <b>{escape(collection['title'])}</b>\n\n{text}",
                parse_mode="HTML",
                disable_notification=False,
                reply_markup=reply_markup,
            )
            delivered += 1
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
    return delivered
