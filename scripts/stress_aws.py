"""Pruebas de estrés contra el despliegue en AWS, con métricas del servidor.

Para cada escenario (load, stress, spike):
  1. obtiene un token OAuth 2.0 *client credentials* del cliente M2M de Cognito
     (el secreto se lee de Terraform y nunca se imprime);
  2. ejecuta scripts/loadgen.py (carga mixta con duplicados y verificación de consistencia);
  3. lee de CloudWatch lo que pasó dentro de AWS en esa ventana: peticiones y errores de API
     Gateway, concurrencia y duración de las Lambdas, cola SQS y DLQ, DynamoDB y los
     reintentos del ledger.

Los resultados se guardan en docs/results/aws-<escenario>.json.

    python scripts/stress_aws.py --scenarios load stress spike --rps 20 --duration 120

La ingesta va al tenant del cliente M2M (variable m2m_tenant de Terraform): usa uno dedicado
(p. ej. pruebas-carga) para no mezclar la carga con la vista de Ejemplo ni con datos reales.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]
TF_DIR = ROOT / "infra" / "terraform" / "envs" / "dev"
RESULTS = ROOT / "docs" / "results"
sys.path.insert(0, str(ROOT / "scripts"))

import loadgen  # noqa: E402


def tf_output(name: str) -> str:
    return subprocess.run(
        ["terraform", "output", "-raw", name],
        cwd=TF_DIR,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def m2m_token(domain: str, client_id: str, client_secret: str) -> str:
    response = httpx.post(
        f"{domain}/oauth2/token",
        data={"grant_type": "client_credentials", "scope": "radar-gastos/ingest"},
        auth=(client_id, client_secret),
        timeout=15,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def server_metrics(
    cw: Any, start: datetime, end: datetime, api_id: str, prefix: str
) -> dict[str, Any]:
    """Métricas de CloudWatch de la ventana, en resoluciones de 1 minuto."""

    def q(qid: str, ns: str, metric: str, dims: dict[str, str], stat: str) -> dict[str, Any]:
        return {
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": ns,
                    "MetricName": metric,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
                },
                "Period": 60,
                "Stat": stat,
            },
        }

    api = {"ApiId": api_id, "Stage": "$default"}
    fn = lambda name: {"FunctionName": f"{prefix}-{name}"}  # noqa: E731
    queue = {"QueueName": f"{prefix}-work"}
    dlq = {"QueueName": f"{prefix}-work-dlq"}
    table = {"TableName": prefix}
    queries = [
        q("api_requests", "AWS/ApiGateway", "Count", api, "Sum"),
        q("api_5xx", "AWS/ApiGateway", "5xx", api, "Sum"),
        q("api_4xx", "AWS/ApiGateway", "4xx", api, "Sum"),
        q("api_latency_p95", "AWS/ApiGateway", "Latency", api, "p95"),
        q("api_fn_concurrency", "AWS/Lambda", "ConcurrentExecutions", fn("api"), "Maximum"),
        q("api_fn_duration_p95", "AWS/Lambda", "Duration", fn("api"), "p95"),
        q("api_fn_throttles", "AWS/Lambda", "Throttles", fn("api"), "Sum"),
        q(
            "collector_concurrency",
            "AWS/Lambda",
            "ConcurrentExecutions",
            fn("collector"),
            "Maximum",
        ),
        q("collector_invocations", "AWS/Lambda", "Invocations", fn("collector"), "Sum"),
        q("collector_errors", "AWS/Lambda", "Errors", fn("collector"), "Sum"),
        q("collector_duration_p95", "AWS/Lambda", "Duration", fn("collector"), "p95"),
        q("queue_oldest_s", "AWS/SQS", "ApproximateAgeOfOldestMessage", queue, "Maximum"),
        q("queue_visible", "AWS/SQS", "ApproximateNumberOfMessagesVisible", queue, "Maximum"),
        q("dlq_visible", "AWS/SQS", "ApproximateNumberOfMessagesVisible", dlq, "Maximum"),
        q("ddb_write_units", "AWS/DynamoDB", "ConsumedWriteCapacityUnits", table, "Sum"),
        q("ddb_read_units", "AWS/DynamoDB", "ConsumedReadCapacityUnits", table, "Sum"),
        q(
            "ddb_throttled",
            "AWS/DynamoDB",
            "ThrottledRequests",
            {**table, "Operation": "TransactWriteItems"},
            "Sum",
        ),
        # Métricas de negocio (EMF) del colector: dimensión service="collector".
        q("ledger_written", "RadarDeGastos", "RecordsWritten", {"service": "collector"}, "Sum"),
        q(
            "queue_to_ledger_p95_ms",
            "RadarDeGastos",
            "QueueToLedgerLatency",
            {"service": "collector"},
            "p95",
        ),
        q(
            "ledger_retry_conflict",
            "RadarDeGastos",
            "LedgerRetryTransactionConflict",
            {"service": "collector"},
            "Sum",
        ),
        q(
            "ledger_retry_condition",
            "RadarDeGastos",
            "LedgerRetryConditionalCheckFailed",
            {"service": "collector"},
            "Sum",
        ),
    ]
    data = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
    out: dict[str, Any] = {}
    for r in data["MetricDataResults"]:
        values = [v for _, v in sorted(zip(r["Timestamps"], r["Values"], strict=True))]
        out[r["Id"]] = {
            "per_minute": [round(v, 1) for v in values],
            "max": round(max(values), 1) if values else 0,
            "sum": round(sum(values), 1) if values else 0,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--scenarios", nargs="+", default=["load", "stress", "spike"])
    parser.add_argument("--rps", type=float, default=20)
    parser.add_argument("--duration", type=float, default=120)
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument("--dup-rate", type=float, default=0.15)
    parser.add_argument("--read-ratio", type=float, default=0.5)
    parser.add_argument("--verify-timeout", type=float, default=300)
    parser.add_argument("--pause", type=float, default=90, help="segundos entre escenarios")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--tag", default="", help="sufijo de los archivos (p. ej. sharding)")
    parser.add_argument("--mode", choices=["mixed", "read", "ingest"], default="mixed")
    args = parser.parse_args()

    api_url = tf_output("api_url")
    domain = tf_output("cognito_domain")
    client_id = tf_output("ingest_client_id")
    client_secret = tf_output("ingest_client_secret")
    api_id = api_url.split("//")[1].split(".")[0]
    prefix = tf_output("table_name")  # radar-gastos-dev: prefijo de todos los recursos
    cw = boto3.client("cloudwatch", region_name=args.region)
    RESULTS.mkdir(parents=True, exist_ok=True)

    for i, scenario in enumerate(args.scenarios):
        if i:
            print(f"pausa de {args.pause:.0f} s para separar las ventanas de métricas", flush=True)
            time.sleep(args.pause)
        token = m2m_token(domain, client_id, client_secret)  # vale 60 min: uno por escenario
        print(f"== {scenario}: {args.rps} rps base, {args.duration:.0f} s", flush=True)
        start = datetime.now(tz=UTC) - timedelta(minutes=1)
        lg_args = argparse.Namespace(
            base_url=api_url,
            token=token,
            tenant="",
            scenario=scenario,
            mode=args.mode,
            read_ratio=args.read_ratio,
            rps=args.rps,
            duration=args.duration,
            batch=args.batch,
            dup_rate=args.dup_rate,
            concurrency=64,
            verify_timeout=args.verify_timeout,
            out="",
        )
        summary = asyncio.run(loadgen.run(lg_args))
        time.sleep(90)  # CloudWatch publica las métricas con ~1-2 min de retraso
        end = datetime.now(tz=UTC) + timedelta(minutes=1)
        summary["server"] = server_metrics(cw, start, end, api_id, prefix)
        summary["environment"] = "AWS Academy Learner Lab, us-east-1"
        summary["executed_at"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
        out = RESULTS / f"aws-{scenario}{'-' + args.tag if args.tag else ''}.json"
        out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        c = summary.get("consistency") or {}
        print(
            f"   {summary['requests']} peticiones, {summary['achieved_rps']} req/s, errores {summary['error_rate']:.2%}, "
            f"ingesta p95 {summary.get('ingest_latency_ms', {}).get('p95')} ms, "
            f"costo esperado {c.get('expected_cost')} vs observado {c.get('observed_cost')} -> {out.name}",
            flush=True,
        )


if __name__ == "__main__":
    main()
