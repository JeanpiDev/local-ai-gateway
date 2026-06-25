#!/usr/bin/env python3
"""Prueba de estrés del gateway-api (asyncio + httpx).

Hace un barrido de concurrencia contra POST /v1/chat/completions y mide, por nivel:
throughput (req/s), latencia p50/p95/p99, y conteo de 200 / 429 / errores. Sirve para
hallar el límite de peticiones concurrentes y validar el comportamiento de cola/429.

Uso (dentro del contenedor gateway-api, que ya trae httpx):
  python /tmp/loadtest.py --admin-key <ADMIN_BOOTSTRAP_KEY> --model qwen2.5:7b-instruct \
      --levels 1,2,4 --total 6 --max-tokens 16

Si pasas --api-key usa esa key; si pasas --admin-key, provisiona un usuario temporal
y lo borra al final. Variables de entorno equivalentes: GATEWAY_URL, API_KEY, ADMIN_KEY, MODEL.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time

import httpx


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return s[k]


async def one_request(client, url, key, model, prompt, max_tokens, timeout):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "stream": False, "max_tokens": max_tokens}
    t0 = time.perf_counter()
    try:
        r = await client.post(url, headers={"Authorization": f"Bearer {key}"},
                              json=body, timeout=timeout)
        return r.status_code, (time.perf_counter() - t0) * 1000
    except Exception:
        return 0, (time.perf_counter() - t0) * 1000


async def run_level(client, url, key, model, prompt, max_tokens, timeout, concurrency, total):
    sem = asyncio.Semaphore(concurrency)
    results: list[tuple[int, float]] = []

    async def task():
        async with sem:
            results.append(await one_request(client, url, key, model, prompt, max_tokens, timeout))

    t0 = time.perf_counter()
    await asyncio.gather(*[task() for _ in range(total)])
    return results, time.perf_counter() - t0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway-url", default=os.environ.get("GATEWAY_URL", "http://localhost:8000"))
    ap.add_argument("--api-key", default=os.environ.get("API_KEY", ""))
    ap.add_argument("--admin-key", default=os.environ.get("ADMIN_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("MODEL", "qwen2.5:7b-instruct"))
    ap.add_argument("--levels", default="1,2,4")
    ap.add_argument("--total", type=int, default=6, help="peticiones por nivel")
    ap.add_argument("--max-tokens", type=int, default=16)
    ap.add_argument("--prompt", default="Cuenta del 1 al 3.")
    ap.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()

    base = args.gateway_url.rstrip("/")
    chat_url = f"{base}/v1/chat/completions"
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    async with httpx.AsyncClient() as client:
        key = args.api_key
        user_id = None
        if not key:
            if not args.admin_key:
                print("Necesitas --api-key o --admin-key"); return
            r = await client.post(f"{base}/admin/users", headers={"X-Admin-Key": args.admin_key},
                                  json={"name": "Load", "email": f"load-{int(time.time())}@local.test",
                                        "password": "Load12345!", "role": "user"}, timeout=60)
            r.raise_for_status()
            key = r.json()["api_key"]; user_id = r.json()["id"]
            print(f"Usuario temporal provisionado ({user_id[:8]}...)")

        print(f"\nBarrido contra {chat_url}  modelo={args.model}  total/nivel={args.total}  max_tokens={args.max_tokens}\n")
        print(f"{'conc':>4} {'200':>4} {'429':>4} {'err':>4} {'wall_s':>7} {'p50':>7} {'p95':>7} {'p99':>7} {'req/s':>7}")
        try:
            for c in levels:
                results, wall = await run_level(client, chat_url, key, args.model, args.prompt,
                                                args.max_tokens, args.timeout, c, args.total)
                oks = [dt for code, dt in results if code == 200]
                n429 = sum(1 for code, _ in results if code == 429)
                errs = sum(1 for code, _ in results if code not in (200, 429))
                rps = (len(oks) / wall) if wall else 0
                print(f"{c:>4} {len(oks):>4} {n429:>4} {errs:>4} {wall:>7.1f} "
                      f"{pct(oks,50)/1000:>6.1f}s {pct(oks,95)/1000:>6.1f}s {pct(oks,99)/1000:>6.1f}s {rps:>7.3f}")
        finally:
            if user_id:
                await client.delete(f"{base}/admin/users/{user_id}", headers={"X-Admin-Key": args.admin_key}, timeout=60)
                print("\n(usuario temporal borrado)")


if __name__ == "__main__":
    asyncio.run(main())
