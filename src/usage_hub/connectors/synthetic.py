"""Conector sintético y determinista: demo sin credenciales y generación de carga.

Con la misma semilla produce siempre los mismos datos (reproducibilidad de los
experimentos). Opciones: ``members`` (int), ``seed`` (int).
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping
from decimal import Decimal

from usage_hub.connectors.registry import register
from usage_hub.domain.models import ConnectorConfig, Provider, UsageRecord
from usage_hub.domain.ports import CollectionWindow

# Precios ilustrativos por millón de tokens (entrada, salida); no son tarifas oficiales.
CATALOG: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal(4), Decimal(20)),
    "claude-sonnet-5-5": (Decimal(2), Decimal(10)),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "gpt-model-large": (Decimal("2.50"), Decimal(10)),
    "gemini-model-pro": (Decimal("1.25"), Decimal(10)),
}
MILLION = Decimal(1_000_000)
SKIP_PROBABILITY = 0.35  # probabilidad de que un miembro no use un modelo en un día


class SyntheticConnector:
    provider = Provider.SYNTHETIC

    def __init__(self, members: int = 10, seed: int = 42) -> None:
        self._members = members
        self._seed = seed

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        for day in window.days():
            rng = random.Random(f"{self._seed}:{tenant_id}:{day.isoformat()}")  # noqa: S311 - no criptográfico
            for member_idx in range(self._members):
                for model, (price_in, price_out) in CATALOG.items():
                    if rng.random() < SKIP_PROBABILITY:
                        continue
                    input_tokens = rng.randint(10_000, 2_000_000)
                    output_tokens = rng.randint(1_000, 400_000)
                    cost = (
                        Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out
                    ) / MILLION
                    yield UsageRecord(
                        tenant_id=tenant_id,
                        provider=self.provider,
                        service=model,
                        principal=f"user:dev-{member_idx:03d}",
                        usage_date=day,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=rng.randint(0, input_tokens // 2),
                        requests=rng.randint(10, 3_000),
                        cost=cost.quantize(Decimal("0.000001")),
                    )


@register(Provider.SYNTHETIC)
def _factory(config: ConnectorConfig, secret: Mapping[str, str]) -> SyntheticConnector:
    return SyntheticConnector(
        members=int(config.options.get("members", "10")), seed=int(config.options.get("seed", "42"))
    )
