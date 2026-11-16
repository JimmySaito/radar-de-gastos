"""Servidor local de UsageHub (sin AWS).

Ejecuta el mismo handler de Lambda de la API traduciendo peticiones HTTP a eventos de
API Gateway HTTP API v2. Ofrece dos conjuntos de datos:

- **Ejemplo** (tenant ``demo``): organización ficticia generada al arrancar.
- **Datos reales** (tenant ``local``): las conexiones que configures en la pantalla de
  Configuración. La configuración y las credenciales se guardan en ``~/.usagehub``
  (o ``$USAGEHUB_HOME``) y, al arrancar, se resincronizan los últimos días.

    python scripts/local_server.py --port 8080
    # abrir http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("STORAGE", "local")
os.environ.setdefault("AUTH_MODE", "local")
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "UsageHub")
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "true")
os.environ.setdefault("POWERTOOLS_METRICS_DISABLED", "true")
os.environ.setdefault("POWERTOOLS_LOG_LEVEL", "WARNING")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from usage_hub.adapters.localfs import default_home  # noqa: E402
from usage_hub.adapters.memory import InMemoryQueue  # noqa: E402
from usage_hub.handlers import api  # noqa: E402
from usage_hub.handlers.bootstrap import container, today_utc  # noqa: E402
from usage_hub.services.connections import ConnectionService  # noqa: E402
from usage_hub.services.demo import seed_demo  # noqa: E402
from usage_hub.services.ingestion import UsageCollector, UsageIngestor, parse_task  # noqa: E402

FRONTEND = ROOT / "frontend"
REAL_TENANT = "local"
STARTUP_SYNC_DAYS = 30
_drain_lock = threading.Lock()


class _Ctx:
    function_name = "local-api"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:local:000000000000:function:local-api"
    aws_request_id = "local"


def drain_queue() -> None:
    """Simula a SQS + Lambda colectora: procesa los mensajes encolados por la API.

    Los fallos de un conector quedan registrados en su estado (visible en Configuración)
    y no interrumpen la respuesta HTTP."""
    c = container()
    queue = c.queue
    assert isinstance(queue, InMemoryQueue)
    collector = UsageCollector(c.configs, c.secrets, UsageIngestor(c.usage, c.mappings))
    with _drain_lock:
        while queue.messages:
            message = queue.messages.pop(0)
            try:
                collector.handle(parse_task(message))
            except Exception as exc:
                print(f"sincronización fallida: {type(exc).__name__}", file=sys.stderr)


def startup_sync() -> None:
    """Reconstruye los datos reales en memoria a partir de las conexiones guardadas."""
    c = container()
    service = ConnectionService(c.configs, c.secrets, c.queue, today_utc)
    connectors = [x for x in c.configs.list_connectors() if x.tenant_id != c.settings.demo_tenant]
    for config in connectors:
        if config.enabled:
            service.sync(config.tenant_id, config.connector_id, STARTUP_SYNC_DAYS)
    if connectors:
        print(
            f"sincronizando {len(connectors)} conexión(es) guardada(s) en segundo plano...",
            flush=True,
        )
        drain_queue()
        print("sincronización inicial terminada", flush=True)


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
        payload = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "content-type", mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        # Obligatorio con HTTP/1.1 keep-alive: sin él, el navegador espera más bytes indefinidamente.
        self.send_header("content-length", str(len(payload)))
        self.send_header("cache-control", "no-cache")  # tras un cambio, el navegador revalida
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path.startswith("/v1/"):
            self._api("GET")
        elif self.path.startswith("/config.json"):
            payload = json.dumps(
                {"apiBaseUrl": "", "authMode": "local", "tenant": REAL_TENANT}
            ).encode()
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

    def do_DELETE(self) -> None:
        self._api("DELETE")

    def do_OPTIONS(self) -> None:
        self._api("OPTIONS")

    verbose = False

    def log_message(self, fmt: str, *args: Any) -> None:
        # Por defecto no se escribe en consola: en Windows, si la ventana entra en modo
        # selección (QuickEdit), cualquier escritura bloquea el hilo y la petición se cuelga.
        if self.verbose:
            sys.stderr.write(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--members", type=int, default=12)
    parser.add_argument("--verbose", action="store_true", help="registra cada petición en consola")
    args = parser.parse_args()
    Handler.verbose = args.verbose
    c = container()
    stats = seed_demo(
        c.usage, c.mappings, c.configs, today_utc(), days=args.days, members=args.members
    )
    print(f"datos de ejemplo listos ({stats.written} registros)", flush=True)
    print(f"configuración y credenciales en: {default_home()}", flush=True)
    threading.Thread(target=startup_sync, daemon=True).start()
    print(f"UsageHub local en http://localhost:{args.port}  (Ctrl+C para detener)", flush=True)
    ThreadingHTTPServer.request_queue_size = (
        512  # backlog por defecto (5) rechaza conexiones bajo carga
    )
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
