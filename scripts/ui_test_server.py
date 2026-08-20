from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp import web

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sharebudget.config import Settings  # noqa: E402
from sharebudget.db import Database  # noqa: E402
from sharebudget.links import group_start_param  # noqa: E402
from sharebudget.service import BudgetService  # noqa: E402
from sharebudget.webapp import setup_webapp_routes  # noqa: E402

TOKEN = "123456:ui-test-token"


def signed_init_data() -> str:
    data = {
        "auth_date": str(int(time.time())),
        "query_id": "ui-test",
        "signature": "ui-test-signature",
        "user": json.dumps(
            {"id": 1, "first_name": "Анна", "username": "anna"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "start_param": group_start_param(-100500, TOKEN),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def rendered_index() -> str:
    index = (PROJECT_ROOT / "src/sharebudget/webapp_assets/index.html").read_text("utf-8")
    asset_version = hashlib.sha256(
        b"".join(
            (PROJECT_ROOT / "src/sharebudget/webapp_assets" / name).read_bytes()
            for name in ("styles.css", "telegram-ready.js", "app.js")
        )
    ).hexdigest()[:12]
    return index.replace("__BOT_USERNAME__", "ShakeOnIt_bot").replace(
        "__ASSET_VERSION__", asset_version
    ).replace(
        "__ANALYTICS_SCRIPT__", ""
    )


def test_index() -> str:
    index = rendered_index()
    mock = f"""
<script>
window.Telegram = {{ WebApp: {{
  initData: {json.dumps(signed_init_data())},
  initDataUnsafe: {{
    user: {{ id: 1, first_name: "Анна", username: "anna", allows_write_to_pm: true }},
    chat: {{ id: -100500, type: "supergroup", title: "Друзья в Берлине" }}
  }},
  ready() {{}}, expand() {{}}, setHeaderColor() {{}}, setBackgroundColor() {{}},
  enableClosingConfirmation() {{}}, disableClosingConfirmation() {{}}, close() {{}},
  requestWriteAccess(callback) {{ callback(true); }},
  showConfirm(message, callback) {{ window.__lastConfirm = message; callback(true); }},
  openTelegramLink(url) {{
    window.__lastTelegramLink = url;
    document.documentElement.dataset.lastTelegramLink = url;
  }},
  shareMessage(id, callback) {{ window.__lastSharedMessage = id; callback(true); }},
  HapticFeedback: {{ impactOccurred() {{}} }},
  BackButton: {{ show() {{}}, hide() {{}}, onClick(callback) {{ window.__back = callback; }} }}
}} }};
</script>
"""
    return index.replace(
        '<script id="telegram-sdk" src="https://telegram.org/js/telegram-web-app.js?63" async></script>',
        mock,
    )


async def create_application(database_path: Path) -> web.Application:
    database = Database(database_path)
    await database.initialize()
    service = BudgetService(database)
    await service.upsert_user(1, "anna", "Анна", private_started=True)
    await service.upsert_user(2, "boris", "Борис", private_started=True)
    await service.upsert_user(3, None, "Максим", private_started=True)
    await service.set_payment_details(2, "Телефон +375 29 000-00-00", "Беларусбанк")
    collection_id = await service.create_collection(-100500, "Берлин", "EUR", 1)
    await service.join(collection_id, 2, subscribe=True)
    await service.join(collection_id, 3, subscribe=True)
    await service.add_expense(collection_id, 1, 12_000, [1, 2, 3], "Отель")
    await service.add_repayment(collection_id, 2, 1, 1_000, "Первая часть долга")
    await service.add_repayment(collection_id, 2, 1, 1_000, "Вторая часть долга")
    for number in range(23):
        await service.add_expense(
            collection_id, 1, 100 + number, [1, 2, 3], f"Тестовый расход {number + 1}"
        )
    dinner_id = await service.create_collection(-100500, "Ужин с друзьями", "EUR", 2)
    await service.join(dinner_id, 1, subscribe=True)
    await service.add_expense(dinner_id, 2, 5_000, [1], "Общий счёт")
    delayed_request = {"path": "", "seconds": 0.0}

    @web.middleware
    async def delay_next_api(request: web.Request, handler):
        if delayed_request["path"] and request.path == delayed_request["path"]:
            seconds = delayed_request["seconds"]
            delayed_request.update(path="", seconds=0.0)
            await asyncio.sleep(seconds)
        return await handler(request)

    @web.middleware
    async def inject_telegram(request: web.Request, handler):
        if request.path in {"/app", "/app/"}:
            if request.query.get("sdk") == "slow":
                ready_probe = """
<script>
window.TelegramWebviewProxy = {
  postEvent(type) {
    if (type === "web_app_ready") {
      window.__telegramReadyAt = performance.now();
      document.documentElement.dataset.telegramReady = "true";
      document.documentElement.dataset.telegramReadyAt = String(window.__telegramReadyAt);
    }
  }
};
</script>
"""
                page = rendered_index().replace("<head>", f"<head>{ready_probe}").replace(
                    "https://telegram.org/js/telegram-web-app.js",
                    "/test/slow-telegram-sdk.js",
                )
                return web.Response(text=page, content_type="text/html")
            return web.Response(text=test_index(), content_type="text/html")
        return await handler(request)

    application = web.Application(middlewares=[delay_next_api, inject_telegram])

    async def mutate(_: web.Request) -> web.Response:
        await service.add_expense(collection_id, 2, 900, [1, 2, 3], "Фоновое обновление")
        return web.json_response({"ok": True})

    application.router.add_post("/test/mutate", mutate)

    async def delay(request: web.Request) -> web.Response:
        payload = await request.json()
        delayed_request.update(
            path=str(payload.get("path") or ""),
            seconds=max(0.0, min(float(payload.get("seconds") or 0), 5.0)),
        )
        return web.json_response({"ok": True})

    application.router.add_post("/test/delay", delay)

    async def slow_telegram_sdk(_: web.Request) -> web.Response:
        await asyncio.sleep(3)
        return web.Response(text="", content_type="application/javascript")

    application.router.add_get("/test/slow-telegram-sdk.js", slow_telegram_sdk)
    settings = Settings(
        bot_token=TOKEN,
        database_path=database.path,
        webapp_url="http://127.0.0.1:8765/app",
        main_app_enabled=True,
    )
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        delete_message=AsyncMock(),
        save_prepared_inline_message=AsyncMock(return_value=SimpleNamespace(id="prepared-ui")),
    )
    setup_webapp_routes(application, bot, service, settings)
    return application


async def run(port: int) -> None:
    temporary = tempfile.TemporaryDirectory(prefix="shakeonit-ui-")
    application = await create_application(Path(temporary.name) / "ui.db")
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    print(f"UI test server: http://127.0.0.1:{port}/app", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        temporary.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local authenticated Mini App test server")
    parser.add_argument("--port", type=int, default=8765)
    asyncio.run(run(parser.parse_args().port))


if __name__ == "__main__":
    main()
