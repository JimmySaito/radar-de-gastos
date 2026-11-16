"""Conector de Claude Code a partir de sus registros locales (``~/.claude/projects``).

Claude Code guarda cada sesión como JSONL; cada respuesta del asistente incluye el
modelo y su ``usage`` (tokens de entrada, salida y caché). Así se puede centralizar el
consumo de Claude Code aunque se use con una suscripción Pro/Max, que no tiene API de uso.

- Los tokens son exactos. El costo es el **equivalente en API** (estimado, ver
  ``pricing.py``): con suscripción no se paga por token, pero sirve para comparar.
- Un mismo mensaje puede aparecer en varias líneas (streaming): se deduplica por
  ``message.id`` + ``requestId``, igual que la facturación cuenta una sola respuesta.
- Solo tiene sentido donde están los archivos (el servidor local o el equipo de cada
  persona). Para centralizar un equipo, cada persona ejecuta
  ``scripts/push_claude_code.py`` contra la API de ingesta.
"""

from __future__ import annotations

import getpass
import json
from collections.abc import Iterable, Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Any

from usage_hub.connectors._util import RecordBuilder, to_date
from usage_hub.connectors.pricing import price_for
from usage_hub.connectors.registry import register
from usage_hub.domain.models import ConnectorConfig, Provider, UsageRecord
from usage_hub.domain.ports import CollectionWindow

DEFAULT_HOME = "~/.claude"
IGNORED_MODELS = {"<synthetic>"}


class ClaudeCodeLogConnector:
    provider = Provider.CLAUDE_CODE

    def __init__(self, home: Path, user: str) -> None:
        self._projects = home / "projects"
        self._user = user

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        if not self._projects.is_dir():
            raise FileNotFoundError(
                f"no se encontró {self._projects}: ¿Claude Code está instalado en este equipo?"
            )
        builder = RecordBuilder(tenant_id, self.provider)
        principal = f"claude_code:{self._user}"
        for entry in self._unique_entries(window):
            usage, model = entry["usage"], entry["model"]
            input_tokens = int(usage.get("input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cost = price_for(model).cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_write_tokens=cache_write,
                cache_read_tokens=cache_read,
                fast=usage.get("speed") == "fast",
            )
            builder.add(
                entry["day"],
                model,
                principal,
                input_tokens=input_tokens + cache_write,
                output_tokens=output_tokens,
                cached_tokens=cache_read,
                requests=1,
                cost=cost,
            )
        return list(builder.build())

    def _unique_entries(self, window: CollectionWindow) -> Iterator[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        for path in sorted(self._projects.rglob("*.jsonl")):
            # Optimización: un archivo sin cambios desde antes de la ventana no aporta nada.
            if to_date(path.stat().st_mtime) < window.start:
                continue
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    entry = _parse(line, window.start, window.end)
                    if entry is None:
                        continue
                    if entry["key"] in seen:
                        continue
                    seen.add(entry["key"])
                    yield entry


def _parse(line: str, start: date, end: date) -> dict[str, Any] | None:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return None
    usage, model = message.get("usage"), message.get("model")
    if not isinstance(usage, dict) or not model or model in IGNORED_MODELS:
        return None
    timestamp = data.get("timestamp")
    if not timestamp:
        return None
    day = to_date(timestamp)
    if not start <= day < end:
        return None
    key = (str(message.get("id") or data.get("uuid")), str(data.get("requestId") or ""))
    return {"key": key, "day": day, "model": str(model), "usage": usage}


def resolve_home(raw: str | None) -> Path:
    return Path(raw or DEFAULT_HOME).expanduser()


@register(Provider.CLAUDE_CODE)
def _factory(config: ConnectorConfig, secret: Mapping[str, str]) -> ClaudeCodeLogConnector:
    user = config.options.get("user") or getpass.getuser()
    return ClaudeCodeLogConnector(resolve_home(config.options.get("path")), user)
