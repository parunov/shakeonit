import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sharebudget.config import Settings
from sharebudget.db import Database
from sharebudget.links import group_start_param
from sharebudget.service import BudgetService
from sharebudget.webapp import (
    ApiError,
    _collection_invite_markup,
    setup_webapp_routes,
    validate_init_data,
)

TOKEN = "123456:test-token"


def signed_init_data(
    *, auth_date: int | None = None, user: dict | None = None, start_param: str | None = None
) -> str:
    data = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAEAAAE",
        "signature": "telegram-ed25519-signature",
        "user": json.dumps(
            user or {"id": 42, "first_name": "Анна", "username": "anna"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if start_param:
        data["start_param"] = start_param
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_validate_init_data_accepts_authentic_telegram_payload():
    result = validate_init_data(signed_init_data(), TOKEN)

    assert result["user"]["id"] == 42
    assert result["user"]["first_name"] == "Анна"


def test_validate_init_data_rejects_tampering():
    raw = signed_init_data().replace("anna", "ivan")

    with pytest.raises(ApiError, match="подтвердить вход"):
        validate_init_data(raw, TOKEN)


def test_validate_init_data_rejects_expired_session():
    raw = signed_init_data(auth_date=int(time.time()) - 90000)

    with pytest.raises(ApiError, match="Сессия устарела"):
        validate_init_data(raw, TOKEN, max_age=86400)


def test_validate_init_data_rejects_duplicate_fields():
    raw = signed_init_data() + "&user=%7B%7D"

    with pytest.raises(ApiError, match="Некорректные данные"):
        validate_init_data(raw, TOKEN)


def test_invalid_startapp_link_is_not_rendered_without_main_app():
    settings = Settings(bot_token=TOKEN, main_app_enabled=False)
    buttons = [
        button for row in _collection_invite_markup(settings, 42).inline_keyboard for button in row
    ]

    assert not any(button.url and "startapp" in button.url for button in buttons)
    assert any(button.callback_data == "join:42" for button in buttons)

    settings.main_app_enabled = True
    enabled_buttons = [
        button for row in _collection_invite_markup(settings, 42).inline_keyboard for button in row
    ]
    assert any(button.url and "startapp=collection_42" in button.url for button in enabled_buttons)


@pytest.mark.asyncio
async def test_webapp_serves_ui_and_authenticates_api(tmp_path):
    database = Database(tmp_path / "miniapp.db")
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "owner", "Организатор")
    collection_id = await service.create_collection(-100500, "Поездка", "EUR", 1)
    settings = Settings(bot_token=TOKEN, database_path=database.path)
    application = web.Application()
    setup_webapp_routes(application, object(), service, settings)

    async with TestClient(TestServer(application)) as client:
        page = await client.get("/app")
        assert page.status == 200
        assert "ShakeOnIt" in await page.text()

        script = await client.get("/app/static/app.js")
        assert script.status == 200
        assert script.headers["Cache-Control"] == "no-cache"

        unauthorized = await client.get("/api/bootstrap")
        assert unauthorized.status == 401

        authorized = await client.get(
            "/api/bootstrap",
            headers={"X-Telegram-Init-Data": signed_init_data()},
        )
        payload = await authorized.json()
        assert authorized.status == 200
        assert payload["user"]["id"] == 42
        assert payload["user"]["preferred_currency"] == "BYN"
        assert payload["main_app_enabled"] is False
        assert payload["is_new_user"] is True

        currency = await client.patch(
            "/api/me/currency",
            json={"currency": "USD"},
            headers={"X-Telegram-Init-Data": signed_init_data()},
        )
        assert currency.status == 200
        assert (await service.get_user(42))["preferred_currency"] == "USD"

        group_context = await client.get(
            "/api/bootstrap",
            headers={
                "X-Telegram-Init-Data": signed_init_data(
                    start_param=group_start_param(-100500, TOKEN)
                )
            },
        )
        group_payload = await group_context.json()
        assert group_payload["context_chat_id"] == -100500
        assert group_payload["collections"][0]["id"] == collection_id
        assert group_payload["collections"][0]["is_participant"] is False

        forged_context = await client.get(
            "/api/bootstrap",
            headers={
                "X-Telegram-Init-Data": signed_init_data(start_param="chat_n100500_deadbeef0000")
            },
        )
        forged_payload = await forged_context.json()
        assert forged_payload["context_chat_id"] is None
        assert forged_payload["collections"] == []

        invitation = await client.get(
            "/api/bootstrap",
            headers={
                "X-Telegram-Init-Data": signed_init_data(start_param=f"collection_{collection_id}")
            },
        )
        invitation_payload = await invitation.json()
        assert invitation_payload["invitation"]["collection"]["id"] == collection_id
        assert invitation_payload["invitation"]["is_participant"] is False
