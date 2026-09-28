"""Steady synthetic traffic against the gateway so there is always something to observe.

70% product lookups, 30% checkouts. Prints a status-code summary every 10 seconds.
Settings: GATEWAY_URL (default http://localhost:8080), RPS (default 5).
"""

import asyncio
import os
import random
import time
from collections import Counter

import httpx

from demo.common import SKUS

REPORT_EVERY_SECONDS = 10


async def one_request(client: httpx.AsyncClient, url: str, stats: Counter[str]) -> None:
    try:
        if random.random() < 0.7:
            resp = await client.get(f"{url}/products/{random.choice(SKUS)}")
        else:
            order = {"sku": random.choice(SKUS), "qty": random.randint(1, 3)}
            resp = await client.post(f"{url}/checkout", json=order)
        stats[str(resp.status_code)] += 1
    except httpx.HTTPError:
        stats["conn_error"] += 1


async def wait_for_gateway(client: httpx.AsyncClient, url: str) -> None:
    while True:
        try:
            if (await client.get(f"{url}/health")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        print(f"loadgen: waiting for {url} ...", flush=True)
        await asyncio.sleep(2)


async def main() -> None:
    url = os.getenv("GATEWAY_URL", "http://localhost:8080").rstrip("/")
    rps = float(os.getenv("RPS", "5"))
    stats: Counter[str] = Counter()
    inflight: set[asyncio.Task[None]] = set()

    async with httpx.AsyncClient(timeout=5.0) as client:
        await wait_for_gateway(client, url)
        print(f"loadgen: sending {rps} req/s to {url}", flush=True)
        last_report = time.monotonic()
        while True:
            task = asyncio.create_task(one_request(client, url, stats))
            inflight.add(task)
            task.add_done_callback(inflight.discard)
            await asyncio.sleep(1 / rps)

            if time.monotonic() - last_report >= REPORT_EVERY_SECONDS:
                summary = " ".join(f"{k}={v}" for k, v in sorted(stats.items()))
                print(f"loadgen: last {REPORT_EVERY_SECONDS}s: {summary}", flush=True)
                stats.clear()
                last_report = time.monotonic()


if __name__ == "__main__":
    asyncio.run(main())
