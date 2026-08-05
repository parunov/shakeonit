from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
)
from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .config import Settings
from .links import parse_group_start_param
from .money import CURRENCIES, format_money, parse_amount
from .notifications import notify_subscribers
from .service import BudgetService, DomainError

LOGGER = logging.getLogger(__name__)
WEBAPP_DIR = Path(__file__).with_name("webapp_assets")
BOT_KEY = web.AppKey("bot", Bot)
SERVICE_KEY = web.AppKey("service", BudgetService)
SETTINGS_KEY = web.AppKey("settings", Settings)
AUTH_KEY = web.RequestKey("telegram_auth", dict)
NEW_USER_KEY = web.RequestKey("new_user", bool)
FX_CACHE_KEY = web.AppKey("fx_cache", dict)
NBRB_RATES_URL = "https://api.nbrb.by/exrates/rates?periodicity=0"


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


def _participant(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "payment_details": row["payment_details"],
        "is_admin": bool(row["is_admin"]),
        "active": bool(row["active"]),
    }


def _collection(row) -> dict:
    values = dict(row)
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
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
    collection = await service.get_collection(collection_id)
    if not collection:
        raise ApiError("Сбор не найден", 404)
    if not await service.is_participant(collection_id, user_id):
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
) -> tuple[bool, int]:
    try:
        await bot.send_message(
            collection["chat_id"],
            text,
            parse_mode="HTML",
            disable_notification=True,
            reply_markup=reply_markup,
        )
        group_sent = True
    except TelegramAPIError:
        LOGGER.warning(
            "Could not publish Mini App report to chat %s", collection["chat_id"], exc_info=True
        )
        group_sent = False
    notifications_sent = await notify_subscribers(
        bot,
        service,
        collection,
        text,
        exclude_user_ids=exclude_user_ids,
        reply_markup=subscriber_reply_markup,
    )
    return group_sent, notifications_sent


async def _confirm_private_subscription(
    bot: Bot, service: BudgetService, collection, user_id: int
) -> bool:
    try:
        await bot.send_message(
            user_id,
            f"🔔 <b>Уведомления включены</b>\n\nСбор: «{escape(collection['title'])}». "
            "Теперь важные операции будут приходить в этот чат.",
            parse_mode="HTML",
        )
        return True
    except TelegramAPIError:
        await service.set_notification_subscription(collection["id"], user_id, False)
        LOGGER.info("Telegram write access is unavailable for user %s", user_id)
        return False


def _collection_invite_markup(settings: Settings, collection_id: int) -> InlineKeyboardMarkup:
    username = settings.bot_username.lstrip("@")
    app_url = (
        f"https://t.me/{username}?startapp=collection_{collection_id}&mode=compact"
        if settings.main_app_enabled
        else f"https://t.me/{username}?start=app"
    )
    rows = [
        [
            InlineKeyboardButton(
                text="📱 Открыть сбор",
                url=app_url,
            )
        ],
        [
            InlineKeyboardButton(
                text="🙋 Участвовать в сборе",
                callback_data=f"join:{collection_id}",
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _name(user: dict) -> str:
    full_name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    return escape(full_name)


@web.middleware
async def api_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    try:
        settings = request.app[SETTINGS_KEY]
        auth = validate_init_data(
            request.headers.get("X-Telegram-Init-Data", ""),
            settings.bot_token,
            settings.webapp_auth_max_age,
        )
        request[AUTH_KEY] = auth
        user = auth["user"]
        full_name = " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        )
        request[NEW_USER_KEY] = await request.app[SERVICE_KEY].upsert_user(
            user["id"], user.get("username"), full_name, private_started=True
        )
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
                "preferred_currency": user["preferred_currency"],
            },
            "collections": [_collection(row) for row in rows],
            "chats": [
                {
                    "chat_id": row["chat_id"],
                    "label": f"Группа сбора «{row['reference_title']}»",
                }
                for row in chats
            ],
            "currencies": list(CURRENCIES),
            "invitation": invitation,
            "is_new_user": request.get(NEW_USER_KEY, False),
            "context_chat_id": context_chat_id,
            "bot_username": request.app[SETTINGS_KEY].bot_username.lstrip("@"),
            "main_app_enabled": request.app[SETTINGS_KEY].main_app_enabled,
        }
    )


async def collection_details(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    snapshot = await service.collection_snapshot(collection_id)
    history = await service.history(collection_id, 100)
    shares = await service.expense_shares_for_transactions(
        row["id"] for row in history if row["kind"] == "expense"
    )
    names = {row["id"]: row["full_name"] for row in snapshot.participants}
    return web.json_response(
        {
            "ok": True,
            "collection": _collection(collection),
            "participants": [_participant(row) for row in snapshot.participants],
            "balances": [
                {"user_id": member_id, "amount": amount}
                for member_id, amount in snapshot.balances.items()
            ],
            "debts": [
                {
                    "debtor_id": debt.debtor_id,
                    "creditor_id": debt.creditor_id,
                    "amount": debt.amount,
                    "debtor_name": names[debt.debtor_id],
                    "creditor_name": names[debt.creditor_id],
                }
                for debt in snapshot.debts
            ],
            "total": snapshot.total,
            "notifications_enabled": await service.notification_subscription(
                collection_id, user["id"]
            ),
            "history": [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "creator_id": row["creator_id"],
                    "creator_name": row["creator_name"],
                    "counterparty_id": row["counterparty_id"],
                    "counterparty_name": row["counterparty_name"],
                    "amount": row["amount"],
                    "comment": row["comment"],
                    "status": row["status"],
                    "confirmation_status": row["confirmation_status"],
                    "confirmed_at": row["confirmed_at"],
                    "created_at": row["created_at"],
                    "shared_with": row["shared_with"],
                    "shares": shares.get(row["id"], []),
                }
                for row in history
            ],
        }
    )


async def create_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    payload = await _json_body(request)
    chat_id = _integer(payload, "chat_id", "Выберите группу")
    context_chat_id = parse_group_start_param(
        request[AUTH_KEY].get("start_param", ""), request.app[SETTINGS_KEY].bot_token
    )
    if chat_id != context_chat_id and not await service.can_create_in_chat(user["id"], chat_id):
        raise ApiError("Сначала откройте меню бота в нужной группе", 403)
    collection_id = await service.create_collection(
        chat_id, str(payload.get("title", "")), str(payload.get("currency", "")), user["id"]
    )
    collection = await service.get_collection(collection_id)
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"🧾 {_name(user)} создал сбор <b>«{escape(collection['title'])}»</b> · "
        f"{collection['currency']}\n\nНажмите «Участвовать» — регистрация займет один шаг.",
        _collection_invite_markup(request.app[SETTINGS_KEY], collection_id),
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {
            "ok": True,
            "collection_id": collection_id,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
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


async def add_expense(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    payload = await _json_body(request)
    amount = parse_amount(str(payload.get("amount", "")))
    participants = payload.get("participant_ids")
    if not isinstance(participants, list):
        raise ApiError("Выберите участников")
    participant_ids = [int(item) for item in participants]
    comment = str(payload.get("comment", ""))
    transaction_id = await service.add_expense(
        collection_id, user["id"], amount, participant_ids, comment
    )
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"💸 {_name(user)} добавил затрату <b>{format_money(amount, collection['currency'])}</b>"
        f" · {escape(comment) if comment else 'без комментария'} · на {len(participant_ids)} чел.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {
            "ok": True,
            "transaction_id": transaction_id,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
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
    confirm_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить получение",
                    callback_data=f"repayconfirm:{transaction_id}",
                )
            ]
        ]
    )
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"⏳ {_name(user)} сообщил о возврате долга {escape(creditor['full_name'])}: "
        f"<b>{format_money(amount, collection['currency'])}</b>. Баланс изменится после "
        "подтверждения получателем.",
        confirm_markup,
        exclude_user_ids={user["id"], creditor_id},
    )
    try:
        await bot.send_message(
            creditor_id,
            f"🤝 <b>Подтвердите получение</b>\n\nВозврат #{transaction_id}: "
            f"<b>{format_money(amount, collection['currency'])}</b>\n"
            f"Сбор: {escape(collection['title'])}",
            parse_mode="HTML",
            reply_markup=confirm_markup,
        )
    except TelegramAPIError:
        LOGGER.info("Creditor %s has no private chat with bot", creditor_id)
    return web.json_response(
        {
            "ok": True,
            "transaction_id": transaction_id,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
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
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"✅ {_name(user)} подтвердил получение возврата #{transaction_id}: "
        f"<b>{format_money(transaction['amount'], collection['currency'])}</b>. "
        "Балансы пересчитаны.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {"ok": True, "report_sent": sent, "notifications_sent": notifications_sent}
    )


async def join_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await service.get_collection(collection_id)
    if not collection or collection["status"] != "active":
        raise ApiError("Активный сбор не найден", 404)
    payload = await _json_body(request)
    subscribe = payload.get("subscribe") is True
    await service.join(collection_id, user["id"], subscribe=subscribe)
    if subscribe:
        subscribe = await _confirm_private_subscription(bot, service, collection, user["id"])
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"🙋 {_name(user)} участвует в сборе <b>«{escape(collection['title'])}»</b>.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {
            "ok": True,
            "report_sent": sent,
            "notifications_sent": notifications_sent,
            "notifications_enabled": subscribe,
        }
    )


async def global_history(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    transactions, events = await service.global_history(user["id"])
    shares = await service.expense_shares_for_transactions(
        row["id"] for row in transactions if row["kind"] == "expense"
    )
    return web.json_response(
        {
            "ok": True,
            "transactions": [
                {
                    **dict(row),
                    "shares": shares.get(row["id"], []),
                }
                for row in transactions
            ],
            "events": [dict(row) for row in events],
        }
    )


async def edit_transaction(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    transaction_id = int(request.match_info["transaction_id"])
    transaction = await service.transaction(transaction_id)
    if not transaction:
        raise ApiError("Транзакция не найдена", 404)
    collection = await _require_member(service, transaction["collection_id"], user["id"])
    payload = await _json_body(request)
    amount = parse_amount(str(payload.get("amount", "")))
    comment = str(payload.get("comment", ""))
    await service.edit_transaction(transaction_id, user["id"], amount, comment)
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"✏️ {_name(user)} обновил транзакцию #{transaction_id}: "
        f"<b>{format_money(amount, collection['currency'])}</b>",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {"ok": True, "report_sent": sent, "notifications_sent": notifications_sent}
    )


async def cancel_transaction(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    transaction_id = int(request.match_info["transaction_id"])
    transaction = await service.transaction(transaction_id)
    if not transaction:
        raise ApiError("Транзакция не найдена", 404)
    collection = await _require_member(service, transaction["collection_id"], user["id"])
    await service.cancel_transaction(transaction_id, user["id"])
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"↩️ {_name(user)} отменил транзакцию #{transaction_id}. Балансы пересчитаны.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {"ok": True, "report_sent": sent, "notifications_sent": notifications_sent}
    )


async def leave_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    await service.remove_participant(collection_id, user["id"], user["id"])
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"👋 {_name(user)} вышел из сбора <b>«{escape(collection['title'])}»</b>.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {"ok": True, "report_sent": sent, "notifications_sent": notifications_sent}
    )


async def save_payment(request: web.Request) -> web.Response:
    service, _, user = _context(request)
    payload = await _json_body(request)
    await service.set_payment_details(user["id"], str(payload.get("payment_details", "")))
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


async def exchange_rates(request: web.Request) -> web.Response:
    cache = request.app[FX_CACHE_KEY]
    now = time.monotonic()
    if not cache.get("rates") or now - cache.get("loaded_at", 0) > 6 * 60 * 60:
        try:
            async with (
                ClientSession(timeout=ClientTimeout(total=6)) as session,
                session.get(NBRB_RATES_URL) as response,
            ):
                response.raise_for_status()
                rows = await response.json()
            rates = {"BYN": 1.0}
            rate_date = None
            for row in rows:
                currency = row.get("Cur_Abbreviation")
                if currency in CURRENCIES:
                    rates[currency] = float(row["Cur_OfficialRate"]) / int(row["Cur_Scale"])
                    rate_date = rate_date or row.get("Date")
            if set(rates) != set(CURRENCIES):
                raise ValueError("Неполный набор курсов")
            cache.update(rates=rates, rate_date=rate_date, loaded_at=now)
        except (ClientError, OSError, ValueError, TypeError, KeyError, TimeoutError):
            LOGGER.warning("Could not load NBRB exchange rates", exc_info=True)
            raise ApiError("Не удалось загрузить курсы валют. Попробуйте позже", 503) from None
    return web.json_response({"ok": True, "rates": cache["rates"], "date": cache.get("rate_date")})


async def archive_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    await service.archive(collection_id, user["id"])
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"📦 {_name(user)} завершил сбор <b>«{escape(collection['title'])}»</b>. Архив — 30 дней.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {"ok": True, "report_sent": sent, "notifications_sent": notifications_sent}
    )


async def restore_collection(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    await service.restore(collection_id, user["id"])
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"♻️ {_name(user)} восстановил сбор <b>«{escape(collection['title'])}»</b>.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {"ok": True, "report_sent": sent, "notifications_sent": notifications_sent}
    )


async def transfer_admin(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    payload = await _json_body(request)
    new_admin_id = _integer(payload, "user_id", "Выберите участника")
    await service.transfer_admin(collection_id, user["id"], new_admin_id)
    member = await _member(service, collection_id, new_admin_id)
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"👑 Администратор сбора <b>«{escape(collection['title'])}»</b> — "
        f"{escape(member['full_name'])}.",
        exclude_user_ids={user["id"]},
    )
    return web.json_response(
        {"ok": True, "report_sent": sent, "notifications_sent": notifications_sent}
    )


async def remove_member(request: web.Request) -> web.Response:
    service, bot, user = _context(request)
    collection_id = int(request.match_info["collection_id"])
    collection = await _require_member(service, collection_id, user["id"])
    payload = await _json_body(request)
    member_id = _integer(payload, "user_id", "Выберите участника")
    member = await _member(service, collection_id, member_id)
    await service.remove_participant(collection_id, user["id"], member_id)
    sent, notifications_sent = await _report(
        bot,
        service,
        collection,
        f"👥 {escape(member['full_name'])} больше не участвует в сборе "
        f"<b>«{escape(collection['title'])}»</b>.",
        exclude_user_ids={user["id"], member_id},
    )
    return web.json_response(
        {"ok": True, "report_sent": sent, "notifications_sent": notifications_sent}
    )


async def app_index(_: web.Request) -> web.FileResponse:
    response = web.FileResponse(WEBAPP_DIR / "index.html")
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
    application.middlewares.extend([security_headers, api_middleware])
    application.router.add_get("/app", app_index)
    application.router.add_get("/app/", app_index)
    application.router.add_get("/app/static/{filename}", app_asset)
    application.router.add_get("/api/bootstrap", bootstrap)
    application.router.add_get("/api/history", global_history)
    application.router.add_get("/api/rates", exchange_rates)
    application.router.add_get("/api/collections/{collection_id}", collection_details)
    application.router.add_post("/api/collections", create_collection)
    application.router.add_post("/api/chats/prepare", prepare_chat_request)
    application.router.add_post("/api/collections/{collection_id}/expenses", add_expense)
    application.router.add_post("/api/collections/{collection_id}/repayments", add_repayment)
    application.router.add_post("/api/collections/{collection_id}/join", join_collection)
    application.router.add_patch(
        "/api/collections/{collection_id}/notifications", save_notification_subscription
    )
    application.router.add_post("/api/collections/{collection_id}/leave", leave_collection)
    application.router.add_post("/api/collections/{collection_id}/archive", archive_collection)
    application.router.add_post("/api/collections/{collection_id}/restore", restore_collection)
    application.router.add_post("/api/collections/{collection_id}/transfer", transfer_admin)
    application.router.add_post("/api/collections/{collection_id}/remove", remove_member)
    application.router.add_patch("/api/transactions/{transaction_id}", edit_transaction)
    application.router.add_post("/api/transactions/{transaction_id}/cancel", cancel_transaction)
    application.router.add_post("/api/transactions/{transaction_id}/confirm", confirm_repayment)
    application.router.add_patch("/api/me/payment", save_payment)
    application.router.add_patch("/api/me/currency", save_preferred_currency)
