"""Conector de OpenAI (ChatGPT API) basado en la Organization Usage & Costs API.

Endpoints (requieren Admin key, ``Authorization: Bearer sk-admin-...``):
- ``GET /v1/organization/usage/completions``: tokens y requests, agrupables por
  ``user_id``, ``api_key_id``, ``project_id`` y ``model``.
- ``GET /v1/organization/costs``: USD, agrupable sólo por ``project_id`` y ``line_item``.

Limitación documentada (ver ADR-0006): OpenAI no expone costo por usuario, así que el
costo se atribuye al principal ``project:<id>`` y los tokens al principal
``user:<id>``/``api_key:<id>``. El mapeo de gobierno resuelve ambos a equipo.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import httpx

from radar_gastos.connectors._util import RecordBuilder, midnight_utc, to_date, to_decimal
from radar_gastos.connectors.registry import register
from radar_gastos.domain.models import ConnectorConfig, Provider, UsageRecord
from radar_gastos.domain.ports import CollectionWindow

BASE_URL = "https://api.openai.com/v1"
QueryParams = list[tuple[str, str | int | float | bool | None]]
_RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


_HINTS = {
    "expired_secret_key": "la llave venció: crea una nueva Admin key.",
    "invalid_api_key": "la llave no es válida.",
}


class OpenAIAPIError(httpx.HTTPStatusError):
    """Error con el motivo que devuelve OpenAI (no el texto genérico de httpx) y una pista."""

    def __init__(self, response: httpx.Response) -> None:
        code, message = "", response.reason_phrase
        try:
            error = response.json().get("error") or {}
            code, message = str(error.get("code") or ""), str(error.get("message") or message)
        except ValueError:
            pass
        hint = _HINTS.get(code, "")
        if response.status_code in {401, 403} and not hint:
            hint = "se requiere una Admin key de organización (sk-admin-...), no una de proyecto."
        text = f"OpenAI {response.status_code} {code}: {message}".strip()
        super().__init__(
            f"{text} → {hint}" if hint else text, request=response.request, response=response
        )


class OpenAIUsageConnector:
    provider = Provider.OPENAI

    def __init__(self, client: httpx.Client, max_attempts: int = 4) -> None:
        self._client = client
        self._max_attempts = max_attempts

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        builder = RecordBuilder(tenant_id, self.provider)
        start_time = int(midnight_utc(window.start).timestamp())
        end_time = int(midnight_utc(window.end).timestamp())
        days = (window.end - window.start).days

        usage_params: QueryParams = [
            ("start_time", start_time),
            ("end_time", end_time),
            ("bucket_width", "1d"),
            ("limit", days),
            ("group_by", "user_id"),
            ("group_by", "api_key_id"),
            ("group_by", "model"),
        ]
        for bucket in self._paginate("/organization/usage/completions", usage_params):
            day = to_date(bucket["start_time"])
            for result in bucket.get("results", []):
                principal = (
                    f"user:{result['user_id']}"
                    if result.get("user_id")
                    else f"api_key:{result.get('api_key_id') or 'unknown'}"
                )
                cached = int(result.get("input_cached_tokens") or 0)
                builder.add(
                    day,
                    str(result.get("model") or "unknown"),
                    principal,
                    input_tokens=max(int(result.get("input_tokens") or 0) - cached, 0),
                    output_tokens=int(result.get("output_tokens") or 0),
                    cached_tokens=cached,
                    requests=int(result.get("num_model_requests") or 0),
                )

        cost_params: QueryParams = [
            ("start_time", start_time),
            ("end_time", end_time),
            ("bucket_width", "1d"),
            ("limit", days),
            ("group_by", "project_id"),
            ("group_by", "line_item"),
        ]
        for bucket in self._paginate("/organization/costs", cost_params):
            day = to_date(bucket["start_time"])
            for result in bucket.get("results", []):
                amount = result.get("amount") or {}
                builder.add(
                    day,
                    str(result.get("line_item") or "unknown"),
                    f"project:{result.get('project_id') or 'default'}",
                    cost=to_decimal(amount.get("value")),
                )
        return list(builder.build(keep=lambda d: window.start <= d < window.end))

    def _paginate(self, path: str, params: QueryParams) -> Iterator[dict[str, Any]]:
        page: str | None = None
        while True:
            query = [*params, ("page", page)] if page else params
            body = self._get(path, query)
            yield from body.get("data", [])
            if not body.get("has_more"):
                return
            page = body.get("next_page")

    def _get(self, path: str, params: QueryParams) -> dict[str, Any]:
        import time

        for attempt in range(1, self._max_attempts + 1):
            response = self._client.get(path, params=params)
            if response.status_code not in _RETRYABLE or attempt == self._max_attempts:
                if response.is_error:
                    raise OpenAIAPIError(response)
                result: dict[str, Any] = response.json()
                return result
            retry_after = float(response.headers.get("retry-after", 2**attempt))
            time.sleep(min(retry_after, 30.0))
        raise AssertionError("unreachable")  # pragma: no cover


@register(Provider.OPENAI)
def _factory(config: ConnectorConfig, secret: Mapping[str, str]) -> OpenAIUsageConnector:
    client = httpx.Client(
        base_url=config.options.get("base_url", BASE_URL),
        headers={"Authorization": f"Bearer {secret['admin_api_key']}"},
        timeout=httpx.Timeout(30.0, connect=5.0),
    )
    return OpenAIUsageConnector(client)
