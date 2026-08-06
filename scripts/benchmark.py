from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import statistics
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sharebudget.config import Settings  # noqa: E402
from sharebudget.db import Database  # noqa: E402
from sharebudget.service import BudgetService  # noqa: E402
from sharebudget.webapp import setup_webapp_routes  # noqa: E402

TOKEN = "123456:benchmark-token"


def signed_init_data(user_id: int) -> str:
    data = {
        "auth_date": str(int(time.time())),
        "query_id": f"benchmark-{user_id}",
        "signature": "benchmark-signature",
        "user": json.dumps(
            {"id": user_id, "first_name": f"User {user_id}", "username": f"user{user_id}"},
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * percent))]


async def measure(
    name: str,
    requests: int,
    concurrency: int,
    operation: Callable[[int], Awaitable[bool]],
) -> dict:
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors = 0

    async def run_one(index: int) -> None:
        nonlocal errors
        async with semaphore:
            started = time.perf_counter()
            if not await operation(index):
                errors += 1
            latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(run_one(index) for index in range(requests)))
    elapsed = time.perf_counter() - started
    return {
        "name": name,
        "requests": requests,
        "concurrency": concurrency,
        "errors": errors,
        "rps": round(requests / elapsed, 1),
        "mean_ms": round(statistics.fmean(latencies), 2),
        "p50_ms": round(percentile(latencies, 0.50), 2),
        "p95_ms": round(percentile(latencies, 0.95), 2),
        "p99_ms": round(percentile(latencies, 0.99), 2),
    }


async def run(args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="shakeonit-benchmark-") as directory:
        database = Database(Path(directory) / "benchmark.db")
        await database.initialize()
        service = BudgetService(database)
        for user_id in range(1, args.users + 1):
            await service.upsert_user(user_id, f"user{user_id}", f"User {user_id}")
        collection_id = await service.create_collection(-100500, "Benchmark", "EUR", 1)
        for user_id in range(2, args.users + 1):
            await service.join(collection_id, user_id)
        for index in range(args.transactions):
            participants = [((index + offset) % args.users) + 1 for offset in range(5)]
            await service.add_expense(
                collection_id,
                (index % args.users) + 1,
                10_000 + index,
                participants,
                f"Expense {index}",
            )

        settings = Settings(bot_token=TOKEN, database_path=database.path)
        application = web.Application()
        setup_webapp_routes(application, SimpleNamespace(), service, settings)
        auth = [signed_init_data(user_id) for user_id in range(1, args.users + 1)]

        async with TestClient(TestServer(application)) as client:

            async def sync(index: int) -> bool:
                response = await client.get(
                    "/api/sync",
                    headers={"X-Telegram-Init-Data": auth[index % len(auth)]},
                )
                await response.read()
                return response.status == 200

            async def details(index: int) -> bool:
                response = await client.get(
                    f"/api/collections/{collection_id}",
                    headers={"X-Telegram-Init-Data": auth[index % len(auth)]},
                )
                await response.read()
                return response.status == 200

            async def balance(index: int) -> bool:
                response = await client.get(
                    "/api/balance",
                    headers={"X-Telegram-Init-Data": auth[index % len(auth)]},
                )
                await response.read()
                return response.status == 200

            await sync(0)
            await details(0)
            results = [
                await measure("sync", args.requests, args.concurrency, sync),
                await measure(
                    "collection_details",
                    max(50, args.requests // 5),
                    args.concurrency,
                    details,
                ),
                await measure(
                    "balance",
                    max(50, args.requests // 5),
                    args.concurrency,
                    balance,
                ),
            ]
            print(
                json.dumps(
                    {
                        "users": args.users,
                        "transactions": args.transactions,
                        "results": results,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Local ShakeOnIt API load probe")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--transactions", type=int, default=300)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=20)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
