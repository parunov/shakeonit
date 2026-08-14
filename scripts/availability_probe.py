from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from aiohttp import ClientSession, ClientTimeout, TCPConnector


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * percent))]


async def probe(url: str, requests: int, concurrency: int, timeout: float) -> dict:
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    statuses: dict[int, int] = {}
    errors = 0

    async with ClientSession(
        timeout=ClientTimeout(total=timeout),
        connector=TCPConnector(limit=concurrency, ttl_dns_cache=60),
    ) as session:

        async def one() -> None:
            nonlocal errors
            async with semaphore:
                started = time.perf_counter()
                try:
                    async with session.get(url, headers={"Cache-Control": "no-cache"}) as response:
                        await response.read()
                    statuses[response.status] = statuses.get(response.status, 0) + 1
                    if response.status != 200:
                        errors += 1
                except Exception:
                    errors += 1
                finally:
                    latencies.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(requests)))
        elapsed = time.perf_counter() - started

    successful = requests - errors
    return {
        "url": url,
        "requests": requests,
        "concurrency": concurrency,
        "successful": successful,
        "errors": errors,
        "availability_percent": round(successful / requests * 100, 3),
        "statuses": statuses,
        "rps": round(requests / elapsed, 1),
        "mean_ms": round(statistics.fmean(latencies), 2),
        "p95_ms": round(percentile(latencies, 0.95), 2),
        "p99_ms": round(percentile(latencies, 0.99), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe ShakeOnIt public availability")
    parser.add_argument("url")
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    if args.requests <= 0 or args.concurrency <= 0 or args.timeout <= 0:
        parser.error("requests, concurrency and timeout must be positive")
    print(
        json.dumps(
            asyncio.run(probe(args.url, args.requests, args.concurrency, args.timeout)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
