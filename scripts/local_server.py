"""Servidor local de demostración (sin AWS).

Ejecuta el mismo handler de Lambda de la API traduciendo peticiones HTTP a eventos de
API Gateway HTTP API v2, con almacenamiento en memoria y datos sintéticos.

    python scripts/local_server.py --port 8080 --days 60 --members 12
    # abrir http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("STORAGE", "memory")
os.environ.setdefault("AUTH_MODE", "local")
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "UsageHub")
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "true")
os.environ.setdefault("POWERTOOLS_METRICS_DISABLED", "true")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from usage_hub.adapters.memory import InMemoryQueue  # noqa: E402
from usage_hub.connectors.synthetic import SyntheticConnector  # noqa: E402
from usage_hub.domain.models import Budget, ConnectorConfig, PrincipalMapping, Provider, UsageRecord  # noqa: E402
from usage_hub.domain.ports import CollectionWindow  # noqa: E402
from usage_hub.handlers import api  # noqa: E402
from usage_hub.handlers.bootstrap import container, today_utc  # noqa: E402
from usage_hub.services.ingestion import UsageCollector, UsageIngestor, parse_task  # noqa: E402

FRONTEND = ROOT / "frontend"
TENANT = "demo"
TEAMS = ["plataforma", "datos", "producto", "seguridad"]
PROVIDER_FOR_MODEL = {
    "claude": Provider.ANTHROPIC,
    "gpt": Provider.OPENAI,
    "gemini": Provider.GOOGLE,
}


class _Ctx:
    function_name = "local-api"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:local:000000000000:function:local-api"
    aws_request_id = "local"


def seed(days: int, members: int) -> None:
    c = container()
    end = today_utc() + timedelta(days=1)
    synthetic = SyntheticConnector(members=members, seed=7)
    records: list[UsageRecord] = []
    for r in synthetic.fetch(TENANT, CollectionWindow(end - timedelta(days=days), end)):
        provider = next(
            (p for k, p in PROVIDER_FOR_MODEL.items() if r.service.startswith(k)), Provider.CUSTOM
        )
        records.append(r.model_copy(update={"provider": provider}))
    # Consumo de nube (AWS) atribuido por tag.
    for r in synthetic.fetch(TENANT, CollectionWindow(end - timedelta(days=days), end)):
        if r.service == "claude-haiku-5-5":
            records.append(
                r.model_copy(
                    update={
                        "provider": Provider.AWS,
                        "service": "Amazon Bedrock" if int(r.principal[-1]) % 2 else "AWS Lambda",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cached_tokens": 0,
                        "cost": r.cost * 40,
                    }
                )
            )
    for i in range(members):
        for provider in (Provider.ANTHROPIC, Provider.OPENAI, Provider.GOOGLE, Provider.AWS):
            c.mappings.put_mapping(
                TENANT,
                PrincipalMapping(
                    provider=provider,
                    principal=f"user:dev-{i:03d}",
                    member=f"persona{i:02d}@empresa.com",
                    team=TEAMS[i % len(TEAMS)],
                ),
            )
    stats = UsageIngestor(c.usage, c.mappings).ingest(records)
    for team, limit in zip(TEAMS, ("1100", "3000", "1500", "900"), strict=True):
        c.configs.put_budget(Budget(tenant_id=TENANT, team=team, monthly_limit=limit))
    c.configs.put_connector(
        ConnectorConfig(
            tenant_id=TENANT,
            connector_id="demo-synthetic",
            provider=Provider.SYNTHETIC,
            options={"members": str(members)},
        )
    )
    print(f"datos sembrados: {stats.as_dict()}")


def drain_queue() -> None:
    """Simula a SQS + Lambda colectora: procesa los mensajes encolados por la API."""
    c = container()
    queue = c.queue
    assert isinstance(queue, InMemoryQueue)
    collector = UsageCollector(c.configs, c.secrets, UsageIngestor(c.usage, c.mappings))
    while True:
        try:
            message = queue.messages.pop(0)  # list.pop es atómico en CPython (servidor multihilo)
        except IndexError:
            return
        collector.handle(parse_task(message))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: evita abrir una conexión TCP por petición

    def _api(self, method: str) -> None:
        url = urlsplit(self.path)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode() if length else None
        event: dict[str, Any] = {
            "version": "2.0",
            "routeKey": "$default",
            "rawPath": url.path,
            "rawQueryString": url.query,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "queryStringParameters": dict(parse_qsl(url.query)) or None,
            "requestContext": {
                "http": {"method": method, "path": url.path, "sourceIp": "127.0.0.1"},
                "requestId": "local",
                "stage": "$default",
            },
            "body": body,
            "isBase64Encoded": False,
        }
        result = api.handler(event, _Ctx())  # type: ignore[arg-type]
        drain_queue()
        payload = (result.get("body") or "").encode()
        self.send_response(result["statusCode"])
        for k, v in (result.get("headers") or {}).items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _static(self) -> None:
        path = urlsplit(self.path).path
        target = (FRONTEND / (path.lstrip("/") or "index.html")).resolve()
        if not target.is_relative_to(FRONTEND) or not target.is_file():
            target = FRONTEND / "index.html"
        self.send_response(200)
        self.send_header(
            "content-type", mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        self.end_headers()
        self.wfile.write(target.read_bytes())

    def do_GET(self) -> None:
        if self.path.startswith("/v1/"):
            self._api("GET")
        elif self.path.startswith("/config.json"):
            payload = json.dumps({"apiBaseUrl": "", "authMode": "local"}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._static()

    def do_POST(self) -> None:
        self._api("POST")

    def do_PUT(self) -> None:
        self._api("PUT")

    def do_OPTIONS(self) -> None:
        self._api("OPTIONS")

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--members", type=int, default=12)
    args = parser.parse_args()
    seed(args.days, args.members)
    print(f"UsageHub local en http://localhost:{args.port}")
    ThreadingHTTPServer.request_queue_size = (
        512  # backlog por defecto (5) rechaza conexiones bajo carga
    )
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
