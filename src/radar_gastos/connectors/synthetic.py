"""Conector sintético y determinista: demo sin credenciales y generación de carga.

Con la misma semilla produce siempre los mismos datos (reproducibilidad de los
experimentos). Opciones: ``members`` (int), ``seed`` (int).
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping
from decimal import Decimal

from radar_gastos.connectors.registry import register
from radar_gastos.domain.models import ConnectorConfig, Provider, UsageRecord
from radar_gastos.domain.ports import CollectionWindow

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


PROVIDER_BY_PREFIX = {
    "claude": Provider.ANTHROPIC,
    "gpt": Provider.OPENAI,
    "gemini": Provider.GOOGLE,
}
AWS_SERVICES = ("Amazon Bedrock", "AWS Lambda")
AWS_COST_FACTOR = Decimal(40)


class SyntheticConnector:
    """Con ``realistic=True`` emula una organización multiproveedor: asigna cada modelo a su
    proveedor (Anthropic, OpenAI, Google) y agrega costo de nube en AWS. Es el modo que
    alimenta la vista *Ejemplo*."""

    provider = Provider.SYNTHETIC

    def __init__(self, members: int = 10, seed: int = 42, *, realistic: bool = False) -> None:
        self._members = members
        self._seed = seed
        self._realistic = realistic

    def _provider_for(self, model: str) -> Provider:
        if not self._realistic:
            return self.provider
        return next(
            (p for prefix, p in PROVIDER_BY_PREFIX.items() if model.startswith(prefix)),
            Provider.CUSTOM,
        )

    def fetch(self, tenant_id: str, window: CollectionWindow) -> Iterable[UsageRecord]:
        for day in window.days():
            rng = random.Random(f"{self._seed}:{tenant_id}:{day.isoformat()}")  # noqa: S311 - no criptográfico
            for member_idx in range(self._members):
                principal = f"user:dev-{member_idx:03d}"
                for model, (price_in, price_out) in CATALOG.items():
                    if rng.random() < SKIP_PROBABILITY:
                        continue
                    input_tokens = rng.randint(10_000, 2_000_000)
                    output_tokens = rng.randint(1_000, 400_000)
                    cost = (
                        Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out
                    ) / MILLION
                    requests = rng.randint(10, 3_000)
                    yield UsageRecord(
                        tenant_id=tenant_id,
                        provider=self._provider_for(model),
                        service=model,
                        principal=principal,
                        usage_date=day,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=rng.randint(0, input_tokens // 2),
                        requests=requests,
                        cost=cost.quantize(Decimal("0.000001")),
                    )
                    if self._realistic and model == "claude-haiku-5-5":
                        yield UsageRecord(
                            tenant_id=tenant_id,
                            provider=Provider.AWS,
                            service=AWS_SERVICES[member_idx % len(AWS_SERVICES)],
                            principal=principal,
                            usage_date=day,
                            requests=requests,
                            cost=(cost * AWS_COST_FACTOR).quantize(Decimal("0.000001")),
                        )


@register(Provider.SYNTHETIC)
def _factory(config: ConnectorConfig, secret: Mapping[str, str]) -> SyntheticConnector:
    return SyntheticConnector(
        members=int(config.options.get("members", "10")),
        seed=int(config.options.get("seed", "42")),
        realistic=config.options.get("realistic", "false").lower() == "true",
    )
