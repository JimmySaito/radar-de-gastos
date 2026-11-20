"""Sirve el dashboard (SPA estática) desde la misma Lambda de la API.

Se usa cuando no hay CloudFront disponible, por ejemplo en AWS Academy Learner Lab: API
Gateway entrega HTTPS y Cognito acepta su URL como *callback*. Se activa con
``WEB_ENABLED=true``; ``/config.json`` se genera desde variables de entorno y cada respuesta
lleva las mismas cabeceras de seguridad que aplica CloudFront en el despliegue normal.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".md": "text/plain; charset=utf-8",
}


def enabled() -> bool:
    return os.environ.get("WEB_ENABLED", "").lower() == "true"


def is_web_request(event: dict[str, Any]) -> bool:
    """GET fuera de /v1/: lo que pide el navegador al abrir el dashboard."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = str(event.get("rawPath") or "/")
    return method in {"GET", "HEAD"} and not path.startswith("/v1/") and path != "/v1"


@cache
def web_root() -> Path:
    # En el paquete de Lambda: /var/task/frontend (ver scripts/build_lambda.py).
    default = Path(__file__).resolve().parents[2] / "frontend"
    return Path(os.environ.get("WEB_ROOT", default)).resolve()


def security_headers(content_type: str) -> dict[str, str]:
    """Cabeceras de seguridad del dashboard; las usan la API y el servidor local."""
    cognito = os.environ.get("COGNITO_DOMAIN_URL", "")
    csp = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"connect-src 'self' {cognito}; frame-ancestors 'none'; base-uri 'self'; "
        f"form-action 'self' {cognito}"
    )
    return {
        "content-type": content_type,
        "cache-control": "no-cache",
        "content-security-policy": csp,
        "strict-transport-security": "max-age=63072000; includeSubDomains; preload",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "strict-origin-when-cross-origin",
    }


def runtime_config() -> dict[str, str]:
    """Lo mismo que Terraform escribe en config.json con CloudFront; la API es el mismo origen."""
    return {
        "apiBaseUrl": "",
        "authMode": "cognito",
        "cognitoDomain": os.environ.get("COGNITO_DOMAIN_URL", ""),
        "clientId": os.environ.get("SPA_CLIENT_ID", ""),
    }


def serve(event: dict[str, Any]) -> dict[str, Any]:
    path = str(event.get("rawPath") or "/")
    if path == "/config.json":
        body, content_type = json.dumps(runtime_config()), CONTENT_TYPES[".json"]
    else:
        root = web_root()
        target = (root / (path.lstrip("/") or "index.html")).resolve()
        # Evita salir de la carpeta (../) y responde la SPA para rutas desconocidas.
        if not target.is_relative_to(root) or not target.is_file():
            target = root / "index.html"
        body = target.read_text(encoding="utf-8")
        content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
    return {
        "statusCode": 200,
        "headers": security_headers(content_type),
        "body": body,
        "isBase64Encoded": False,
    }
