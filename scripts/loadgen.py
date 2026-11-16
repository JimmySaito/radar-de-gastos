"""Generador de carga y verificador de consistencia (sin dependencias externas a httpx).

Escenarios (perfiles de tasa de llegada abierta, *open model*):
  load   : tasa constante (--rps) durante --duration segundos
  stress : rampa lineal de 10% a 200% de --rps
  spike  : base 20% con un pico de 10x durante el 20% central del tiempo

Ingesta: cada petición envía --batch registros; con probabilidad --dup-rate reenvía
un lote ya enviado (simula reintentos de clientes y entrega al-menos-una-vez). Al
final verifica que el costo agregado por la API sea EXACTAMENTE la suma de los
registros únicos (consistencia eventual + idempotencia).

    python scripts/loadgen.py --base-url http://localhost:8080 --scenario stress --rps 200 --duration 60
    python scripts/loadgen.py --base-url https://xxx.execute-api.us-east-1.amazonaws.com \
        --token "$TOKEN" --scenario spike --rps 100 --duration 300 --verify-timeout 300
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx


@dataclass
class Results:
    latencies_ms: dict[str, list[float]] = field(default_factory=lambda: {"ingest": [], "read": []})
    status: dict[str, int] = field(default_factory=dict)
    sent_batches: list[list[dict[str, object]]] = field(default_factory=list)
    duplicates: int = 0
    dropped: int = 0  # peticiones no lanzadas por saturación del cliente

    def record(self, kind: str, elapsed_ms: float, status: int) -> None:
        self.latencies_ms[kind].append(elapsed_ms)
        key = f"{kind}:{status}"
        self.status[key] = self.status.get(key, 0) + 1


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def rate_at(scenario: str, t: float, duration: float, rps: float) -> float:
    progress = t / duration
    if scenario == "stress":
        return rps * (0.1 + 1.9 * progress)
    if scenario == "spike":
        return rps * (2.0 if 0.4 <= progress < 0.6 else 0.2)
    return rps


def make_batch(run_id: str, seq: int, size: int, day: str) -> list[dict[str, object]]:
    rng = random.Random(f"{run_id}:{seq}")
    return [
        {
            "provider": "custom",
            "service": f"loadtest-{run_id}",
            "principal": f"user:lt-{seq}-{i}",
            "usage_date": day,
            "input_tokens": rng.randint(100, 100_000),
            "output_tokens": rng.randint(10, 10_000),
            "requests": rng.randint(1, 50),
            "cost": str(Decimal(rng.randint(1, 50_000)) / 1000),
        }
        for i in range(size)
    ]


async def run(args: argparse.Namespace) -> dict[str, object]:
    headers = {"content-type": "application/json", "x-tenant-id": args.tenant}
    if args.token:
        headers["authorization"] = f"Bearer {args.token}"
    run_id = uuid.uuid4().hex[:8]
    day = datetime.now(tz=UTC).date().isoformat()
    results = Results()
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    semaphore = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(
        base_url=args.base_url, headers=headers, timeout=30, limits=limits
    ) as client:

        async def ingest(seq: int) -> None:
            if results.sent_batches and random.random() < args.dup_rate:
                batch = random.choice(results.sent_batches)
                results.duplicates += 1
            else:
                batch = make_batch(run_id, seq, args.batch, day)
            start = time.perf_counter()
            try:
                response = await client.post("/v1/usage/records", json={"records": batch})
                status = response.status_code
            except httpx.HTTPError:
                status = 599
            results.record("ingest", (time.perf_counter() - start) * 1000, status)
            if status == 202 and batch not in results.sent_batches:
                results.sent_batches.append(batch)

        async def read() -> None:
            start = time.perf_counter()
            try:
                response = await client.get("/v1/summary")
                status = response.status_code
            except httpx.HTTPError:
                status = 599
            results.record("read", (time.perf_counter() - start) * 1000, status)

        async def guarded(coro_factory: object) -> None:
            async with semaphore:
                await coro_factory()  # type: ignore[operator]

        tasks: set[asyncio.Task[None]] = set()
        started = time.perf_counter()
        seq, next_at = 0, started
        while (elapsed := time.perf_counter() - started) < args.duration:
            current = max(rate_at(args.scenario, elapsed, args.duration, args.rps), 0.1)
            next_at += 1 / current
            await asyncio.sleep(max(0.0, next_at - time.perf_counter()))
            if semaphore.locked() and len(tasks) > args.concurrency * 4:
                results.dropped += 1
                continue
            is_read = args.mode == "read" or (
                args.mode == "mixed" and random.random() < args.read_ratio
            )
            if is_read:
                task = asyncio.create_task(guarded(read))
            else:
                seq += 1
                task = asyncio.create_task(guarded(lambda s=seq: ingest(s)))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - started

        consistency = None
        if args.mode != "read" and results.sent_batches:
            consistency = await verify(client, run_id, day, results, args.verify_timeout)

    summary: dict[str, object] = {
        "run_id": run_id,
        "scenario": args.scenario,
        "mode": args.mode,
        "target_rps": args.rps,
        "duration_s": round(wall, 1),
        "requests": sum(len(v) for v in results.latencies_ms.values()),
        "achieved_rps": round(sum(len(v) for v in results.latencies_ms.values()) / wall, 1),
        "records_per_s": round(sum(len(b) for b in results.sent_batches) / wall, 1),
        "duplicates_sent": results.duplicates,
        "client_dropped": results.dropped,
        "status": results.status,
        "error_rate": round(
            sum(n for k, n in results.status.items() if not k.endswith((":200", ":202")))
            / max(1, sum(results.status.values())),
            5,
        ),
        "consistency": consistency,
    }
    for kind, values in results.latencies_ms.items():
        if values:
            summary[f"{kind}_latency_ms"] = {
                "p50": round(pct(values, 50), 1),
                "p95": round(pct(values, 95), 1),
                "p99": round(pct(values, 99), 1),
                "max": round(max(values), 1),
                "mean": round(statistics.fmean(values), 1),
            }
    return summary


async def verify(
    client: httpx.AsyncClient, run_id: str, day: str, results: Results, timeout: float
) -> dict[str, object]:
    """Espera a que el agregado converja al costo de los registros únicos aceptados."""
    expected = sum(Decimal(str(r["cost"])) for b in results.sent_batches for r in b)
    expected = expected.quantize(Decimal("0.01"))
    end = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    started = time.perf_counter()
    observed = Decimal(0)
    while time.perf_counter() - started < timeout:
        response = await client.get(
            "/v1/timeseries", params={"from": day, "to": end, "group_by": "service"}
        )
        if response.status_code == 200:
            for series in response.json()["series"]:
                if series["name"].startswith(f"loadtest-{run_id}"):
                    observed = sum((Decimal(c) for c in series["cost"]), Decimal(0))
        if observed == expected:
            break
        await asyncio.sleep(2)
    return {
        "expected_cost": str(expected),
        "observed_cost": str(observed),
        "converged": observed == expected,
        "convergence_s": round(time.perf_counter() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token", default="")
    parser.add_argument("--tenant", default=f"lt-{uuid.uuid4().hex[:6]}")
    parser.add_argument("--scenario", choices=["load", "stress", "spike"], default="load")
    parser.add_argument("--mode", choices=["ingest", "read", "mixed"], default="mixed")
    parser.add_argument("--read-ratio", type=float, default=0.5)
    parser.add_argument("--rps", type=float, default=50)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument("--dup-rate", type=float, default=0.1)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--verify-timeout", type=float, default=30)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    summary = asyncio.run(run(args))
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    main()
