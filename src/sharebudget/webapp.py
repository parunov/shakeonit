from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import socket
import time
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    KeyboardButtonRequestChat,
)
from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from .config import Settings
from .keyboards import repayment_confirmation
from .links import parse_group_start_param
from .money import CURRENCIES, format_money, parse_amount
from .notifications import replace_repayment_prompt, report_collection_event, send_with_retry
from .render import telegram_user_link, transaction_update_report
from .service import BudgetService, DomainError

LOGGER = logging.getLogger(__name__)
WEBAPP_DIR = Path(__file__).with_name("webapp_assets")
BOT_KEY = web.AppKey("bot", Bot)
SERVICE_KEY = web.AppKey("service", BudgetService)
SETTINGS_KEY = web.AppKey("settings", Settings)
AUTH_KEY = web.RequestKey("telegram_auth", dict)
NEW_USER_KEY = web.RequestKey("new_user", bool)
FX_CACHE_KEY = web.AppKey("fx_cache", dict)
FX_LOCK_KEY = web.AppKey("fx_lock", asyncio.Lock)
DELIVERY_TASKS_KEY = web.AppKey("delivery_tasks", set)
DELIVERY_LIMIT_KEY = web.AppKey("delivery_limit", asyncio.Semaphore)
USER_SYNC_CACHE_KEY = web.AppKey("user_sync_cache", dict)
AUTH_CACHE_KEY = web.AppKey("auth_cache", dict)
NBRB_RATES_URL = "https://api.nbrb.by/exrates/rates?periodicity=0"
FX_CACHE_SECONDS = 30 * 60
FX_RETRY_SECONDS = 60
USER_SYNC_CACHE_SECONDS = 10 * 60
USER_SYNC_CACHE_LIMIT = 10_000
AUTH_CACHE_SECONDS = 5 * 60
AUTH_CACHE_LIMIT = 10_000


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def validate_init_data(raw: str, bot_token: str, max_age: int = 86400) -> dict:
    if not raw or len(raw) > 8192:
        raise ApiError("Откройте приложение из Telegram", 401)
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    if len({key for key, _ in pairs}) != len(pairs):
        raise ApiError("Некорректные данные авторизации", 401)
    data = dict(pairs)
    received_hash = data.pop("hash", "")
    if not received_hash:
        raise ApiError("Некорректные данные авторизации", 401)
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise ApiError("Не удалось подтвердить вход через Telegram", 401)
    try:
        auth_date = int(data["auth_date"])
        user = json.loads(data["user"])
        user["id"] = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("Некорректные данные пользователя", 401) from exc
    now = int(time.time())
    if auth_date > now + 60 or now - auth_date > max_age:
        raise ApiError("Сессия устарела. Закройте и снова откройте приложение", 401)
    if user.get("is_bot") or not user.get("first_name"):
        raise ApiError("Некорректные данные пользователя", 401)
    data["user"] = user
    return data


def _row(row) -> dict:
    return dict(row) if row is not None else {}


def _participant(row, payment_methods: list[dict] | None = None) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "payment_details": row["payment_details"],
        "bank_name": row["bank_name"],
        "payment_methods": payment_methods or [],
        "is_admin": bool(row["is_admin"]),
        "active": bool(row["active"]),
    }


def _collection(row) -> dict:
    values = dict(row)
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "is_personal": row["chat_id"] == 0,
        "title": row["title"],
        "currency": row["currency"],
        "admin_id": row["admin_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "participants_count": values.get("participants_count"),
        "is_participant": bool(values.get("is_participant", True)),
    }


async def _json_body(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest) as exc:
        raise ApiError("Некорректный запрос") from exc
    if not isinstance(payload, dict):
        raise ApiError("Некорректный запрос")
    return payload


def _integer(payload: dict, field: str, message: str) -> int:
    try:
        return int(payload.get(field))
    except (TypeError, ValueError) as exc:
        raise ApiError(message) from exc


async def _member(service: BudgetService, collection_id: int, user_id: int):
    member = next(
        (row for row in await service.list_participants(collection_id) if row["id"] == user_id),
        None,
    )
    if member is None:
        raise ApiError("Участник не найден", 404)
    return member


async def _require_member(service: BudgetService, collection_id: int, user_id: int):
    collection = await service.get_collection_for_member(collection_id, user_id)
    if not collection:
        if not await service.get_collection(collection_id):
            raise ApiError("Сбор не найден", 404)
        raise ApiError("Этот сбор доступен только его участникам", 403)
    return collection


async def _report(
    bot: Bot,
    service: BudgetService,
    collection,
    text: str,
    reply_markup=None,
    *,
    exclude_user_ids=(),
    subscriber_reply_markup=None,
    category="collection_events",
) -> tuple[bool, int]:
    return await report_collection_event(
        bot,
        service,
        collection,
        text,
        reply_markup,
        exclude_user_ids=exclude_user_ids,
        subscriber_reply_markup=subscriber_reply_markup,
        category=category,
    )


def _queue_delivery(request: web.Request, awaitable, label: str) -> None:
    """Run Telegram delivery outside the API response with bounded concurrency and time."""

    async def runner() -> None:
        try:
            async with request.app[DELIVERY_LIMIT_KEY]:
                await asyncio.wait_for(awaitable, timeout=12)
        except TimeoutError:
            LOGGER.warning("Telegram delivery timed out: %s", label)
        except Exception:
            LOGGER.exception("Telegram delivery failed: %s", label)

    task = asyncio.create_task(runner(), name=f"telegram-delivery:{label}")
    request.app[DELIVERY_TASKS_KEY].add(task)
    task.add_done_callback(request.app[DELIVERY_TASKS_KEY].discard)


def _queue_report(
    request: web.Request,
    collection,
    text: str,
    reply_markup=None,
    *,
    exclude_user_ids=(),
    subscriber_reply_markup=None,
    category="collection_events",
) -> tuple[bool, int]:
    service, bot, _ = _context(request)
    _queue_delivery(
        request,
        _report(
            bot,
            service,
            collection,
            text,
            reply_markup,
            exclude_user_ids=exclude_user_ids,
            subscriber_reply_markup=subscriber_reply_markup,
            category=category,
        ),
        f"collection-{collection['id']}",
    )
    return False, 0


async def delivery_context(application: web.Application):
    application[DELIVERY_TASKS_KEY] = set()
    application[DELIVERY_LIMIT_KEY] = asyncio.Semaphore(4)
    yield
    tasks = list(application[DELIVERY_TASKS_KEY])
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=3)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _confirm_private_subscription(
    bot: Bot,
    service: BudgetService,
    collection,
    user_id: int,
) -> bool:
    try:
        await bot.send_message(
            user_id,
            "🔔 <b>Уведомления включены</b>\n\n"
            f"Сбор: <b>«{escape(collection['title'])}»</b>. "
            "Теперь важные операции будут приходить в этот чат.",
            parse_mode="HTML",
            request_timeout=5,
        )
        return True
    except TelegramAPIError:
        await service.set_notification_subscription(collection["id"], user_id, False)
        LOGGER.info("Telegram write access is unavailable for user %s", user_id)
        return False


def _collection_invite_markup(collection_id: int, settings: Settings) -> InlineKeyboardMarkup:
    username = settings.bot_username.lstrip("@")
    collection_url = (
        f"https://t.me/{username}?startapp=collection_{collection_id}&mode=compact"
        if settings.main_app_enabled
        else f"https://t.me/{username}?start=collection_{collection_id}"
    )
    rows = [[
        InlineKeyboardButton(text="🙋 Участвовать", callback_data=f"join:{collection_id}"),
        InlineKeyboardButton(text="👀 Просмотреть", url=collection_url),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _full_name(user: dict) -> str:
    return " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)


def _name(user: dict) -> str:
    return telegram_user_link(user["id"], _full_name(user), user.get("username"))


@web.middleware
async def api_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    try:
        settings = request.app[SETTINGS_KEY]
        raw_init_data = request.headers.get("X-Telegram-Init-Data", "")
        now_epoch = int(time.time())
        auth_cache = request.app[AUTH_CACHE_KEY]
        cached_auth = auth_cache.get(raw_init_data)
        if cached_auth and cached_auth[1] >= now_epoch:
            auth = cached_auth[0]
        else:
            auth = validate_init_data(
                raw_init_data,
                settings.bot_token,
                settings.webapp_auth_max_age,
            )
            expires_at = min(
                int(auth["auth_date"]) + settings.webapp_auth_max_age,
                now_epoch + AUTH_CACHE_SECONDS,
            )
            auth_cache[raw_init_data] = (auth, expires_at)
            if len(auth_cache) > AUTH_CACHE_LIMIT:
                auth_cache.pop(next(iter(auth_cache)), None)
        request[AUTH_KEY] = auth
        user = auth["user"]
        full_name = " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        )
        if request.path == "/api/sync":
            request[NEW_USER_KEY] = False
        else:
            now = time.monotonic()
            identity = (user.get("username"), full_name)
            cache = request.app[USER_SYNC_CACHE_KEY]
            cached = cache.get(user["id"])
            if cached and cached[0] == identity and now - cached[1] < USER_SYNC_CACHE_SECONDS:
                request[NEW_USER_KEY] = False
            else:
                request[NEW_USER_KEY] = await request.app[SERVICE_KEY].upsert_user(
                    user["id"], user.get("username"), full_name, private_started=True
                )
                cache[user["id"]] = (identity, now)
                if len(cache) > USER_SYNC_CACHE_LIMIT:
                    cache.pop(next(iter(cache)), None)
        return await handler(request)
    except (ApiError, DomainError, ValueError) as exc:
        status = exc.status if isinstance(exc, ApiError) else 400
        return web.json_response({"ok": False, "error": str(exc)}, status=status)
    except web.HTTPException:
        raise
    except Exception:
        LOGGER.exception("Unhandled Mini App API error on %s", request.path)
        return web.json_response(
            {"ok": False, "error": "Не удалось выполнить действие. Попробуйте ещё раз"},
            status=500,
        )


def _context(request: web.Request) -> tuple[BudgetService, Bot, dict]:
    return request.app[SERVICE_KEY], request.app[BOT_KEY], request[AUTH_KEY]["user"]


async def bootstrap(request: web.Request) -> web.Response:
    service, _, telegram_user = _context(request)
    user_id = telegram_user["id"]
    start_param = request[AUTH_KEY].get("start_param", "")
    context_chat_id = parse_group_start_param(start_param, request.app[SETTINGS_KEY].bot_token)
    rows = await service.list_visible_collections(user_id, context_chat_id)
    chats = await service.list_user_collection_chats(user_id)
    user = await service.get_user(user_id)
    payment_methods = [dict(row) for row in await service.list_payment_methods(user_id)]
    payment_details_missing = bool(
        not payment_methods
        and any(row["status"] == "active" and row["is_participant"] for row in rows)
    )
    pending_confirmation = await service.pending_repayment_confirmation(user_id)
    invitation = None
    if start_param.startswith("collection_"):
        try:
            target_id = int(start_param.removeprefix("collection_"))
        except ValueError:
            target_id = 0
        target = await service.get_collection(target_id) if target_id else None
        if target and target["status"] == "active":
            invitation = {
                "collection": _collection(target),
                "is_participant": await service.is_participant(target_id, user_id),
            }
    return web.json_response(
        {
            "ok": True,
            "user": {
                "id": user_id,
                "full_name": user["full_name"],
                "username": user["username"],
                "payment_details": user["payment_details"],
                "bank_name": user["bank_name"],
                "preferred_currency": user["preferred_currency"],
                "payment_methods": payment_methods,
                "notification_preferences": {
                    "notify_expenses": bool(user["notify_expenses"]),
                    "notify_repayments": bool(user["notify_repayments"]),
                    "notify_collection_events": bool(user["notify_collection_events"]),
                    "notify_reminders": bool(user["notify_reminders"]),
                },
            },
            "collections": [_collection(row) for row in rows],
            "chats": [
                {
                    "chat_id": row["chat_id"],
                    "label": f"Группа сбора «{row['reference_title']}»",
                    "title": row["reference_title"],
                }
                for row in chats
            ],
            "currencies": list(CURRENCIES),
            "invitation": invitation,
            "is_new_user": request.get(NEW_USER_KEY, False),
            "context_chat_id": context_chat_id,
            "bot_username": request.app[SETTINGS_KEY].bot_username.lstrip("@"),
            "main_app_enabled": request.app[SETTINGS_KEY].main_app_enabled,
            "pending_repayment_confirmation": (
                dict(pending_confirmation) if pending_confirmation else None
            ),
            "pending_repayment_count": (
                int(pending_confirmation["pending_count"]) if pending_confirmation else 0
            ),
            "payment_details_missing": payment_details_missing,
            "sync_version": await service.sync_token(user_id, context_chat_id),
        }
    )


async def sync_status(request: web.Request) -> web.Response:
    service, _, telegram_user = _context(request)
    context_chat_id = parse_group_start_param(
        request[AUTH_KEY].get("start_param", ""), request.app[SETTINGS_KEY].bot_token
    )
    return web.json_response(
        {
            "ok": True,
            "sync_version": await service.sync_token(telegram_user["id"], context_chat_id),
        }
    )


async def collection_details(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    try:
        history_limit = min(500, max(10, int(request.query.get("history_limit", "10"))))
        events_limit = min(500, max(10, int(request.query.get("events_limit", "10"))))
    except ValueError as exc:
        raise ApiError("Некорректный размер страницы") from exc
    view = await service.collection_view(
        collection_id,
        user["id"],
        history_limit=history_limit + 1,
        events_limit=events_limit + 1,
    )
    if view is None:
        if not await service.get_collection(collection_id):
            raise ApiError("Сбор не найден", 404)
        raise ApiError("Этот сбор доступен только его участникам", 403)
    collection = view.collection
    snapshot = view.snapshot
    history_has_more = len(view.history) > history_limit
    events_has_more = len(view.events) > events_limit
    history = view.history[:history_limit]
    events = view.events[:events_limit]
    shares = view.shares
    people = {row["id"]: row for row in snapshot.participants}
    payment_methods = view.payment_methods
    pending = view.pending_repayments
    return web.json_response(
        {
            "ok": True,
            "collection": _collection(collection),
            "participants": [
                _participant(row, payment_methods.get(row["id"], []))
                for row in snapshot.participants
            ],
            "balances": [
                {"user_id": member_id, "amount": amount}
                for member_id, amount in snapshot.balances.items()
            ],
            "debts": [
                {
                    "debtor_id": debt.debtor_id,
                    "creditor_id": debt.creditor_id,
                    "amount": debt.amount,
                    "repayable_amount": max(
                        0,
                        debt.amount
                        - (pending.get(debt.creditor_id, 0) if debt.debtor_id == user["id"] else 0),
                    ),
                    "debtor_name": people[debt.debtor_id]["full_name"],
                    "debtor_username": people[debt.debtor_id]["username"],
                    "creditor_name": people[debt.creditor_id]["full_name"],
                    "creditor_username": people[debt.creditor_id]["username"],
                }
                for debt in snapshot.debts
            ],
            "total": snapshot.total,
            "notifications_enabled": view.notifications_enabled,
            "history_has_more": history_has_more,
            "events_has_more": events_has_more,
            "events": [dict(row) for row in events],
            "history": [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "creator_id": row["creator_id"],
                    "creator_name": row["creator_name"],
                    "creator_username": row["creator_username"],
                    "counterparty_id": row["counterparty_id"],
                    "counterparty_name": row["counterparty_name"],
                    "counterparty_username": row["counterparty_username"],
                    "amount": row["amount"],
                    "comment": row["comment"],
                    "status": row["status"],
                    "confirmation_status": row["confirmation_status"],
                    "confirmed_at": row["confirmed_at"],
                    "cancelled_by": row["cancelled_by"],
                    "created_at": row["created_at"],
                    "shared_with": row["shared_with"],
                    "has_inactive_participants": row["id"] in view.inactive_transaction_ids,
                    "shares": shares.get(row["id"], []),
                }
                for row in history
            ],
        }
    )


async def create_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    payload = await _json_body(request)
    raw_chat_id = payload.get("chat_id")
    chat_id = (
        0 if raw_chat_id in (None, "", 0, "0") else _integer(payload, "chat_id", "Выберите группу")
    )
    personal = chat_id == 0
    context_chat_id = parse_group_start_param(
        request[AUTH_KEY].get("start_param", ""), request.app[SETTINGS_KEY].bot_token
    )
    if (
        not personal
        and chat_id != context_chat_id
        and not await service.can_create_in_chat(user["id"], chat_id)
    ):
        raise ApiError("Сначала откройте меню бота в нужной группе", 403)
    collection_id = await service.create_collection(
        chat_id, str(payload.get("title", "")), str(payload.get("currency", "")), user["id"]
    )
    collection = await service.get_collection(collection_id)
    notifications_enabled = False
    if personal and payload.get("subscribe") is True:
        await service.set_notification_subscription(collection_id, user["id"], True)
        notifications_enabled = await _confirm_private_subscription(
            bot, service, collection, user["id"]
        )

    async def deliver_creation() -> None:
        sent, _ = await _report(
            bot,
            service,
            collection,
            "🧾 <b>Новый общий сбор</b>\n\n"
            f"{_name(user)} приглашает вести расходы вместе в {collection['currency']}. "
            "Добавляйте траты и сразу видьте, кто кому сколько должен(а).",
            _collection_invite_markup(collection_id, request.app[SETTINGS_KEY]),
            exclude_user_ids={user["id"]},
        )
        if not personal and sent:
            prompt_id = await service.take_bot_message(chat_id, "create_collection_prompt")
            if prompt_id is not None:
                try:
                    await bot.delete_message(chat_id, prompt_id, request_timeout=5)
                except TelegramAPIError:
                    LOGGER.info(
                        "Could not delete collection prompt %s in chat %s", prompt_id, chat_id
                    )

    _queue_delivery(request, deliver_creation(), f"create-collection-{collection_id}")
    return web.json_response(
        {
            "ok": True,
            "collection_id": collection_id,
            "report_sent": False,
            "notifications_sent": 0,
            "notifications_queued": True,
            "notifications_enabled": notifications_enabled,
        }
    )


async def prepare_chat_request(request: web.Request) -> web.Response:
    _, bot, user = _context(request)
    prepared = await bot.save_prepared_keyboard_button(
        user_id=user["id"],
        button=KeyboardButton(
            text="Выбрать группу",
            request_chat=KeyboardButtonRequestChat(
                request_id=secrets.randbelow(2**31),
                chat_is_channel=False,
                bot_is_member=True,
                request_title=True,
                request_username=True,
            ),
        ),
    )
    return web.json_response({"ok": True, "request_id": prepared.id})


async def prepare_collection_share(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    prepared = await bot.save_prepared_inline_message(
        user_id=user["id"],
        result=InlineQueryResultArticle(
            id=f"collection-{collection_id}",
            title=f"Сбор «{collection['title']}»",
            description="Приглашение вести общие расходы вместе",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"🤝 Присоединяйтесь к сбору <b>«{escape(collection['title'])}»</b>\n\n"
                    f"Инициатор: {_name(user)}\n\n"
                    "Вступайте легко в совместный сбор средств, контролируйте расходы "
                    "и возвраты долгов."
                ),
                parse_mode="HTML",
            ),
            reply_markup=_collection_invite_markup(
                collection_id, request.app[SETTINGS_KEY]
            ),
        ),
        allow_user_chats=True,
        allow_group_chats=True,
        allow_bot_chats=False,
        allow_channel_chats=False,
    )
    return web.json_response({"ok": True, "prepared_message_id": prepared.id})


async def add_expense(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    payload = await _json_body(request)
    participant_shares = None
    raw_shares = payload.get("participant_shares")
    if raw_shares is not None:
        if not isinstance(raw_shares, list) or not raw_shares:
            raise ApiError("Укажите сумму хотя бы для одного участника")
        participant_shares = {}
        for item in raw_shares:
            if not isinstance(item, dict):
                raise ApiError("Проверьте индивидуальные суммы")
            participant_id = _integer(item, "user_id", "Проверьте участника")
            if participant_id in participant_shares:
                raise ApiError("Участник указан несколько раз")
            participant_shares[participant_id] = parse_amount(str(item.get("amount", "")))
        participant_ids = list(participant_shares)
        amount = sum(participant_shares.values())
    else:
        amount = parse_amount(str(payload.get("amount", "")))
        participants = payload.get("participant_ids")
        if not isinstance(participants, list):
            raise ApiError("Выберите участников")
        participant_ids = [int(item) for item in participants]
    comment = str(payload.get("comment", ""))
    transaction_id = await service.add_expense(
        collection_id,
        user["id"],
        amount,
        participant_ids,
        comment,
        exact_shares=participant_shares,
    )
    sent, notifications_sent = _queue_report(
        request,
        collection,
        f"💸 {_name(user)} добавил(а) затрату <b>{format_money(amount, collection['currency'])}</b>"
        f" · {escape(comment) if comment else 'без комментария'} · на {len(participant_ids)} чел.",
        exclude_user_ids={user["id"]},
        category="expenses",
    )
    return web.json_response(
        {
            "ok": True,
            "transaction_id": transaction_id,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_queued": True,
        }
    )


async def add_repayment(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    payload = await _json_body(request)
    creditor_id = _integer(payload, "creditor_id", "Выберите получателя")
    amount = parse_amount(str(payload.get("amount", "")))
    comment = str(payload.get("comment", ""))
    transaction_id = await service.add_repayment(
        collection_id, user["id"], creditor_id, amount, comment
    )
    creditor = await _member(service, collection_id, creditor_id)
    comment_line = f"\nКомментарий: {escape(comment.strip())}" if comment.strip() else ""
    confirm_markup = repayment_confirmation(transaction_id)

    async def deliver_repayment() -> None:
        async def send_confirmation() -> None:
            if not await service.notification_enabled_for_user(creditor_id, "repayments"):
                return
            try:
                confirmation_message = await send_with_retry(
                    lambda: bot.send_message(
                        creditor_id,
                        "🤝 <b>Подтвердите получение</b>\n\n"
                        f"От: {_name(user)}\n"
                        f"Сумма: <b>{format_money(amount, collection['currency'])}</b>\n"
                        f"Сбор: <b>«{escape(collection['title'])}»</b>{comment_line}",
                        parse_mode="HTML",
                        reply_markup=confirm_markup,
                        request_timeout=5,
                    )
                )
                message_id = getattr(confirmation_message, "message_id", None)
                if isinstance(message_id, int):
                    await service.set_repayment_confirmation_message(transaction_id, message_id)
            except TelegramAPIError:
                LOGGER.info("Creditor %s has no private chat with bot", creditor_id)

        await asyncio.gather(
            _report(
                bot,
                service,
                collection,
                f"⏳ {_name(user)} сообщил(а) о возврате долга "
                f"{telegram_user_link(creditor['id'], creditor['full_name'], creditor['username'])}: "
                f"<b>{format_money(amount, collection['currency'])}</b>. Баланс изменится после "
                f"подтверждения получателем.{comment_line}",
                confirm_markup,
                exclude_user_ids={user["id"], creditor_id},
                category="repayments",
            ),
            send_confirmation(),
        )

    _queue_delivery(request, deliver_repayment(), f"repayment-{transaction_id}")
    return web.json_response(
        {
            "ok": True,
            "transaction_id": transaction_id,
            "report_sent": False,
            "notifications_sent": 0,
            "notifications_queued": True,
        }
    )


async def request_funds(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    debts = await service.request_funds(collection_id, user["id"])
    collection_title = f"<b>«{escape(collection['title'])}»</b>"

    async def deliver_funds_requests() -> None:
        async def deliver(debt) -> bool:
            try:
                await send_with_retry(
                    lambda: bot.send_message(
                        debt.debtor_id,
                        "🔔 <b>Просьба рассчитаться</b>\n\n"
                        f"{_name(user)} просит вас рассчитаться по действующим долгам "
                        f"в сборе {collection_title}.",
                        parse_mode="HTML",
                        request_timeout=5,
                    )
                )
                return True
            except TelegramAPIError:
                LOGGER.info(
                    "Could not deliver funds request to user %s for collection %s",
                    debt.debtor_id,
                    collection_id,
                )
                return False

        eligible_debts = []
        for debt in debts:
            if await service.notification_enabled_for_user(debt.debtor_id, "reminders"):
                eligible_debts.append(debt)
        await asyncio.gather(*(deliver(debt) for debt in eligible_debts))
        if collection["chat_id"]:
            try:
                await bot.send_message(
                    collection["chat_id"],
                    f"🔔 {_name(user)} просит рассчитаться по действующим долгам "
                    f"в сборе {collection_title}.",
                    parse_mode="HTML",
                    disable_notification=True,
                    request_timeout=5,
                )
            except TelegramAPIError:
                LOGGER.warning(
                    "Could not publish funds request report to chat %s",
                    collection["chat_id"],
                    exc_info=True,
                )

    _queue_delivery(request, deliver_funds_requests(), f"funds-request-{collection_id}")

    return web.json_response(
        {
            "ok": True,
            "debtors_count": len(debts),
            "notifications_sent": 0,
            "failed_count": 0,
            "report_sent": False,
            "notifications_queued": True,
        }
    )


async def confirm_repayment(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    transaction_id = int(request.match_info["transaction_id"])
    transaction = await service.transaction(transaction_id)
    if not transaction:
        raise ApiError("Возврат долга не найден", 404)
    collection = await _require_member(service, transaction["collection_id"], user["id"])
    await service.confirm_repayment(transaction_id, user["id"])
    sender = await service.get_user(transaction["creator_id"])
    comment_line = (
        f" Комментарий: {escape(transaction['comment'])}." if transaction["comment"] else ""
    )
    comment_detail = (
        f"\nКомментарий: {escape(transaction['comment'])}" if transaction["comment"] else ""
    )

    async def deliver_confirmation() -> None:
        await asyncio.gather(
            _report(
                bot,
                service,
                collection,
                f"✅ {_name(user)} подтвердил(а) получение возврата от "
                f"{telegram_user_link(sender['id'], sender['full_name'], sender['username'])}: "
                f"<b>{format_money(transaction['amount'], collection['currency'])}</b>. "
                f"Балансы пересчитаны.{comment_line}",
                exclude_user_ids={user["id"]},
                category="repayments",
            ),
            replace_repayment_prompt(
                bot,
                user["id"],
                transaction["confirmation_message_id"],
                "✅ <b>Получение подтверждено</b>\n\n"
                f"От: {telegram_user_link(sender['id'], sender['full_name'], sender['username'])}\n"
                f"Кому: {_name(user)}\n"
                f"Сумма: <b>{format_money(transaction['amount'], collection['currency'])}</b>\n"
                f"Сбор: <b>«{escape(collection['title'])}»</b>{comment_detail}",
            ),
        )

    _queue_delivery(request, deliver_confirmation(), f"confirm-repayment-{transaction_id}")
    return web.json_response(
        {
            "ok": True,
            "report_sent": False,
            "notifications_sent": 0,
            "notifications_queued": True,
        }
    )


async def reject_repayment(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    transaction_id = int(request.match_info["transaction_id"])
    transaction = await service.transaction(transaction_id)
    if not transaction:
        raise ApiError("Возврат долга не найден", 404)
    collection = await _require_member(service, transaction["collection_id"], user["id"])
    await service.reject_repayment(transaction_id, user["id"])
    sender = await service.get_user(transaction["creator_id"])
    comment_line = (
        f" Комментарий: {escape(transaction['comment'])}." if transaction["comment"] else ""
    )
    comment_detail = (
        f"\nКомментарий: {escape(transaction['comment'])}" if transaction["comment"] else ""
    )

    async def deliver_rejection() -> None:
        await asyncio.gather(
            _report(
                bot,
                service,
                collection,
                f"❌ {_name(user)} отклонил(а) получение возврата от "
                f"{telegram_user_link(sender['id'], sender['full_name'], sender['username'])}: "
                f"<b>{format_money(transaction['amount'], collection['currency'])}</b>. "
                f"Баланс не изменился.{comment_line}",
                exclude_user_ids={user["id"]},
                category="repayments",
            ),
            replace_repayment_prompt(
                bot,
                user["id"],
                transaction["confirmation_message_id"],
                "❌ <b>Получение отклонено</b>\n\n"
                f"От: {telegram_user_link(sender['id'], sender['full_name'], sender['username'])}\n"
                f"Сумма: <b>{format_money(transaction['amount'], collection['currency'])}</b>\n"
                f"Сбор: <b>«{escape(collection['title'])}»</b>{comment_detail}",
            ),
        )

    _queue_delivery(request, deliver_rejection(), f"reject-repayment-{transaction_id}")
    return web.json_response(
        {
            "ok": True,
            "report_sent": False,
            "notifications_sent": 0,
            "notifications_queued": True,
        }
    )


async def join_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await service.get_collection(collection_id)
    if not collection or collection["status"] != "active":
        raise ApiError("Активный сбор не найден", 404)
    payload = await _json_body(request)
    subscribe = payload.get("subscribe") is True
    was_member = await service.is_participant(collection_id, user["id"])
    was_subscribed = (
        await service.notification_subscription(collection_id, user["id"])
        if was_member
        else False
    )
    joined_now = await service.join(collection_id, user["id"], subscribe=subscribe)
    if subscribe and not was_subscribed:
        await _confirm_private_subscription(bot, service, collection, user["id"])
    notifications_enabled = await service.notification_subscription(collection_id, user["id"])
    if joined_now:
        sent, notifications_sent = _queue_report(
            request,
            collection,
            f"🙋 {_name(user)} участвует в сборе <b>«{escape(collection['title'])}»</b>.",
            exclude_user_ids={user["id"]},
        )
    else:
        sent, notifications_sent = False, 0
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_enabled": notifications_enabled,
            "notifications_queued": joined_now,
            "already_participant": not joined_now,
        }
    )


async def global_history(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    section = request.query.get("section", "all")
    if section not in {"all", "transactions", "events"}:
        raise ApiError("Некорректный раздел истории")
    try:
        transaction_offset = max(0, int(request.query.get("transaction_offset", "0")))
        event_offset = max(0, int(request.query.get("event_offset", "0")))
    except ValueError as exc:
        raise ApiError("Некорректная страница истории") from exc
    page_size = 10
    include_transactions = section in {"all", "transactions"}
    include_events = section in {"all", "events"}
    history_task = service.global_history(
        user["id"],
        page_size + 1,
        transaction_offset,
        event_offset,
        include_transactions=include_transactions,
        include_events=include_events,
    )
    stats_task = (
        service.expense_statistics(user["id"], include_collections=False)
        if section == "all"
        else None
    )
    if stats_task is not None:
        (transactions, events), stats = await asyncio.gather(history_task, stats_task)
    else:
        transactions, events = await history_task
        stats = None
    transaction_has_more = len(transactions) > page_size
    event_has_more = len(events) > page_size
    transactions = transactions[:page_size]
    events = events[:page_size]
    shares, inactive_transaction_ids = await asyncio.gather(
        service.expense_shares_for_transactions(
            row["id"] for row in transactions if row["kind"] == "expense"
        ),
        service.transactions_with_inactive_participants(row["id"] for row in transactions),
    )
    response = {
        "ok": True,
        "transactions": [
            {
                **dict(row),
                "has_inactive_participants": row["id"] in inactive_transaction_ids,
                "shares": shares.get(row["id"], []),
            }
            for row in transactions
        ],
        "events": [dict(row) for row in events],
        "transaction_has_more": transaction_has_more,
        "event_has_more": event_has_more,
    }
    if stats is not None:
        response["expense_stats"] = stats
    return web.json_response(response)


async def expense_statistics(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    return web.json_response(
        {"ok": True, **await service.expense_statistics(user["id"])}
    )


async def transaction_edit_context(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    transaction_id = int(request.match_info["transaction_id"])
    transaction = await service.transaction(transaction_id)
    if not transaction:
        raise ApiError("Транзакция не найдена", 404)
    collection = await _require_member(service, transaction["collection_id"], user["id"])
    participants, shares_by_transaction = await asyncio.gather(
        service.list_participants(collection["id"]),
        service.expense_shares_for_transactions([transaction_id]),
    )
    return web.json_response(
        {
            "ok": True,
            "collection": _collection(collection),
            "participants": [dict(row) for row in participants],
            "history": [
                {
                    **dict(transaction),
                    "shares": shares_by_transaction.get(transaction_id, []),
                }
            ],
        }
    )


async def balance_overview(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    overview = await service.balance_overview(user["id"])
    return web.json_response({"ok": True, **overview})


async def edit_transaction(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    transaction_id = int(request.match_info["transaction_id"])
    transaction = await service.transaction(transaction_id)
    if not transaction:
        raise ApiError("Транзакция не найдена", 404)
    collection = await _require_member(service, transaction["collection_id"], user["id"])
    payload = await _json_body(request)
    comment = str(payload.get("comment", ""))
    participant_ids = None
    participant_shares = None
    raw_shares = payload.get("participant_shares")
    if raw_shares is not None:
        if not isinstance(raw_shares, list) or not raw_shares:
            raise ApiError("Укажите сумму хотя бы для одного участника")
        participant_shares = {}
        for item in raw_shares:
            if not isinstance(item, dict):
                raise ApiError("Проверьте индивидуальные суммы")
            participant_id = _integer(item, "user_id", "Проверьте участника")
            if participant_id in participant_shares:
                raise ApiError("Участник указан несколько раз")
            participant_shares[participant_id] = parse_amount(str(item.get("amount", "")))
        amount = sum(participant_shares.values())
    else:
        amount = parse_amount(str(payload.get("amount", "")))
    if "participant_ids" in payload:
        participants = payload["participant_ids"]
        if not isinstance(participants, list):
            raise ApiError("Выберите участников")
        participant_ids = [int(item) for item in participants]
    before_shares = (
        (await service.expense_shares_for_transactions([transaction_id])).get(transaction_id, [])
        if transaction["kind"] == "expense"
        else []
    )
    await service.edit_transaction(
        transaction_id,
        user["id"],
        amount,
        comment,
        participant_ids=participant_ids,
        exact_shares=participant_shares,
    )
    updated = await service.transaction(transaction_id)
    after_shares = (
        (await service.expense_shares_for_transactions([transaction_id])).get(transaction_id, [])
        if transaction["kind"] == "expense"
        else []
    )
    sent, notifications_sent = _queue_report(
        request,
        collection,
        transaction_update_report(
            _full_name(user),
            collection,
            transaction,
            updated,
            before_shares,
            after_shares,
            actor_id=user["id"],
            actor_username=user.get("username"),
        ),
        exclude_user_ids={user["id"]},
        category="expenses" if transaction["kind"] == "expense" else "repayments",
    )
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_queued": True,
        }
    )


async def cancel_transaction(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    transaction_id = int(request.match_info["transaction_id"])
    transaction = await service.transaction(transaction_id)
    if not transaction:
        raise ApiError("Транзакция не найдена", 404)
    collection = await _require_member(service, transaction["collection_id"], user["id"])
    await service.cancel_transaction(transaction_id, user["id"])
    sent, notifications_sent = _queue_report(
        request,
        collection,
        f"↩️ {_name(user)} отменил(а) транзакцию #{transaction_id}. Балансы пересчитаны.",
        exclude_user_ids={user["id"]},
        category="expenses" if transaction["kind"] == "expense" else "repayments",
    )
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_queued": True,
        }
    )


async def leave_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    await service.remove_participant(collection_id, user["id"], user["id"])
    sent, notifications_sent = _queue_report(
        request,
        collection,
        f"👋 {_name(user)} вышел(ла) из сбора <b>«{escape(collection['title'])}»</b>.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_queued": True,
        }
    )


async def save_payment(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    payload = await _json_body(request)
    await service.set_payment_details(
        user["id"],
        str(payload.get("payment_details", "")),
        str(payload.get("bank_name", "")),
    )
    return web.json_response({"ok": True})


async def save_payment_methods(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    payload = await _json_body(request)
    methods = payload.get("payment_methods")
    if not isinstance(methods, list):
        raise ApiError("Некорректные платежные данные")
    await service.replace_payment_methods(user["id"], methods)
    return web.json_response({"ok": True})


async def save_display_name(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    payload = await _json_body(request)
    await service.set_display_name(user["id"], str(payload.get("full_name", "")))
    return web.json_response({"ok": True})


async def save_notification_preferences(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    payload = await _json_body(request)
    await service.set_notification_preferences(user["id"], payload)
    return web.json_response({"ok": True})


async def save_preferred_currency(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    payload = await _json_body(request)
    await service.set_preferred_currency(user["id"], str(payload.get("currency", "")))
    return web.json_response({"ok": True})


async def save_notification_subscription(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    payload = await _json_body(request)
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError("Некорректная настройка уведомлений")
    await service.set_notification_subscription(collection_id, user["id"], enabled)
    if enabled:
        enabled = await _confirm_private_subscription(bot, service, collection, user["id"])
    return web.json_response({"ok": True, "notifications_enabled": enabled})


def _valid_exchange_rates(rates) -> bool:
    if not isinstance(rates, dict) or set(rates) != set(CURRENCIES):
        return False
    return all(
        isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate > 0
        for rate in rates.values()
    )


async def _restore_exchange_rate_cache(application: web.Application) -> None:
    cache = application[FX_CACHE_KEY]
    if cache.get("rates"):
        return
    stored = await application[SERVICE_KEY].db.load_exchange_rate_cache()
    if not stored or not _valid_exchange_rates(stored.get("rates")):
        return
    try:
        fetched_at = int(stored.get("fetched_at") or 0)
    except (TypeError, ValueError):
        return
    age = max(0, int(time.time()) - fetched_at)
    cache.update(
        rates=stored["rates"],
        rate_date=stored.get("rate_date"),
        loaded_at=time.monotonic() - age,
        fetched_at=fetched_at,
    )


async def _refresh_exchange_rates(application: web.Application) -> bool:
    cache = application[FX_CACHE_KEY]
    async with application[FX_LOCK_KEY]:
        now = time.monotonic()
        if cache.get("rates") and now - cache.get("loaded_at", 0) < FX_CACHE_SECONDS:
            return True
        try:
            connector = TCPConnector(family=socket.AF_INET, ttl_dns_cache=FX_CACHE_SECONDS)
            timeout = ClientTimeout(total=5, connect=2, sock_connect=2, sock_read=3)
            async with (
                ClientSession(
                    connector=connector,
                    timeout=timeout,
                    headers={"Accept": "application/json", "User-Agent": "ShakeOnIt/1.0"},
                ) as session,
                session.get(NBRB_RATES_URL) as response,
            ):
                response.raise_for_status()
                rows = await response.json()
            if not isinstance(rows, list):
                raise ValueError("Некорректный ответ НБРБ")
            rates = {"BYN": 1.0}
            rate_date = None
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Некорректная запись курса НБРБ")
                currency = row.get("Cur_Abbreviation")
                if currency in CURRENCIES:
                    rates[currency] = float(row["Cur_OfficialRate"]) / int(row["Cur_Scale"])
                    rate_date = rate_date or row.get("Date")
            if not _valid_exchange_rates(rates):
                raise ValueError("Неполный набор курсов")
            now = time.monotonic()
            fetched_at = int(time.time())
            cache.update(
                rates=rates,
                rate_date=rate_date,
                loaded_at=now,
                fetched_at=fetched_at,
                retry_at=0,
            )
            try:
                await application[SERVICE_KEY].db.save_exchange_rate_cache(
                    rates, rate_date, fetched_at
                )
            except Exception:
                LOGGER.exception("Could not persist NBRB exchange rates")
            return True
        except (ClientError, OSError, ValueError, TypeError, KeyError, TimeoutError):
            cache["retry_at"] = time.monotonic() + FX_RETRY_SECONDS
            LOGGER.warning("Could not load NBRB exchange rates", exc_info=True)
            return False


def _queue_exchange_rate_refresh(application: web.Application) -> None:
    cache = application[FX_CACHE_KEY]
    task = cache.get("refresh_task")
    if task and not task.done():
        return
    task = asyncio.create_task(_refresh_exchange_rates(application), name="exchange-rate-refresh")
    cache["refresh_task"] = task
    application[DELIVERY_TASKS_KEY].add(task)
    task.add_done_callback(application[DELIVERY_TASKS_KEY].discard)


async def exchange_rates(request: web.Request) -> web.Response:
    cache = request.app[FX_CACHE_KEY]
    await _restore_exchange_rate_cache(request.app)
    now = time.monotonic()
    stale = not cache.get("rates") or now - cache.get("loaded_at", 0) >= FX_CACHE_SECONDS
    if not cache.get("rates"):
        if not await _refresh_exchange_rates(request.app):
            raise ApiError("Не удалось загрузить курсы валют. Попробуйте позже", 503)
        stale = False
    elif stale and now >= cache.get("retry_at", 0):
        _queue_exchange_rate_refresh(request.app)
    return web.json_response(
        {
            "ok": True,
            "rates": cache["rates"],
            "date": cache.get("rate_date"),
            "stale": stale,
        }
    )


async def archive_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    await service.archive(collection_id, user["id"])
    sent, notifications_sent = _queue_report(
        request,
        collection,
        f"📦 {_name(user)} завершил(а) сбор <b>«{escape(collection['title'])}»</b>. "
        "Архив — 30 дней.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_queued": True,
        }
    )


async def restore_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    await service.restore(collection_id, user["id"])
    sent, notifications_sent = _queue_report(
        request,
        collection,
        f"♻️ {_name(user)} восстановил(а) сбор <b>«{escape(collection['title'])}»</b>.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_queued": True,
        }
    )


async def delete_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    if collection["status"] != "archived":
        raise ApiError("Удалить можно только сбор из архива")
    sent, notifications_sent = _queue_report(
        request,
        collection,
        f"🗑 {_name(user)} безвозвратно удалил(а) архивный сбор "
        f"<b>«{escape(collection['title'])}»</b>.",
        exclude_user_ids={user["id"]},
    )
    await service.delete_archived(collection_id, user["id"])
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_queued": True,
        }
    )


async def transfer_admin(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    payload = await _json_body(request)
    new_admin_id = _integer(payload, "user_id", "Выберите участника")
    await service.transfer_admin(collection_id, user["id"], new_admin_id)
    member = await _member(service, collection_id, new_admin_id)
    sent, notifications_sent = _queue_report(
        request,
        collection,
        f"👑 Администратор сбора <b>«{escape(collection['title'])}»</b> — "
        f"{telegram_user_link(member['id'], member['full_name'], member['username'])}.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_queued": True,
        }
    )


async def remove_member(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    payload = await _json_body(request)
    member_id = _integer(payload, "user_id", "Выберите участника")
    member = await _member(service, collection_id, member_id)
    await service.remove_participant(collection_id, user["id"], member_id)
    sent, notifications_sent = _queue_report(
        request,
        collection,
        f"👥 {telegram_user_link(member['id'], member['full_name'], member['username'])} "
        "больше не участвует в сборе "
        f"<b>«{escape(collection['title'])}»</b>.",
        exclude_user_ids={user["id"], member_id},
    )
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_queued": True,
        }
    )


async def app_index(request: web.Request) -> web.Response:
    template = (WEBAPP_DIR / "index.html").read_text(encoding="utf-8")
    username = escape(request.app[SETTINGS_KEY].bot_username.lstrip("@"), quote=True)
    response = web.Response(
        text=template.replace("__BOT_USERNAME__", username),
        content_type="text/html",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def app_asset(request: web.Request) -> web.FileResponse:
    filename = request.match_info["filename"]
    if filename not in {"app.js", "styles.css"}:
        raise web.HTTPNotFound()
    response = web.FileResponse(WEBAPP_DIR / filename)
    response.headers["Cache-Control"] = "no-cache"
    return response


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://telegram.org; "
        "style-src 'self'; img-src 'self' data: https:; connect-src 'self'; "
        "frame-ancestors https://web.telegram.org https://*.telegram.org"
    )
    return response


def setup_webapp_routes(
    application: web.Application,
    bot: Bot,
    service: BudgetService,
    settings: Settings,
) -> None:
    application[BOT_KEY] = bot
    application[SERVICE_KEY] = service
    application[SETTINGS_KEY] = settings
    application[FX_CACHE_KEY] = {}
    application[FX_LOCK_KEY] = asyncio.Lock()
    application[USER_SYNC_CACHE_KEY] = {}
    application[AUTH_CACHE_KEY] = {}
    application.cleanup_ctx.append(delivery_context)
    application.middlewares.extend([security_headers, api_middleware])
    application.router.add_get("/app", app_index)
    application.router.add_get("/app/", app_index)
    application.router.add_get("/app/static/{filename}", app_asset)
    application.router.add_get("/api/bootstrap", bootstrap)
    application.router.add_get("/api/sync", sync_status)
    application.router.add_get("/api/history", global_history)
    application.router.add_get("/api/expense-statistics", expense_statistics)
    application.router.add_get("/api/balance", balance_overview)
    application.router.add_get("/api/rates", exchange_rates)
    application.router.add_get("/api/collections/{collection_id}", collection_details)
    application.router.add_post("/api/collections", create_collection)
    application.router.add_post("/api/chats/prepare", prepare_chat_request)
    application.router.add_post(
        "/api/collections/{collection_id}/prepare-share", prepare_collection_share
    )
    application.router.add_post("/api/collections/{collection_id}/expenses", add_expense)
    application.router.add_post("/api/collections/{collection_id}/repayments", add_repayment)
    application.router.add_post("/api/collections/{collection_id}/request-funds", request_funds)
    application.router.add_post("/api/collections/{collection_id}/join", join_collection)
    application.router.add_patch(
        "/api/collections/{collection_id}/notifications", save_notification_subscription
    )
    application.router.add_post("/api/collections/{collection_id}/leave", leave_collection)
    application.router.add_post("/api/collections/{collection_id}/archive", archive_collection)
    application.router.add_post("/api/collections/{collection_id}/restore", restore_collection)
    application.router.add_delete("/api/collections/{collection_id}", delete_collection)
    application.router.add_post("/api/collections/{collection_id}/transfer", transfer_admin)
    application.router.add_post("/api/collections/{collection_id}/remove", remove_member)
    application.router.add_patch("/api/transactions/{transaction_id}", edit_transaction)
    application.router.add_get(
        "/api/transactions/{transaction_id}/edit-context", transaction_edit_context
    )
    application.router.add_post("/api/transactions/{transaction_id}/cancel", cancel_transaction)
    application.router.add_post("/api/transactions/{transaction_id}/confirm", confirm_repayment)
    application.router.add_post("/api/transactions/{transaction_id}/reject", reject_repayment)
    application.router.add_patch("/api/me/payment", save_payment)
    application.router.add_put("/api/me/payment-methods", save_payment_methods)
    application.router.add_patch("/api/me/name", save_display_name)
    application.router.add_patch("/api/me/notifications", save_notification_preferences)
    application.router.add_patch("/api/me/currency", save_preferred_currency)
