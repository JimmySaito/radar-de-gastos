"""Tabla de precios de modelos de Claude para estimar el *costo equivalente en API*.

Se usa cuando la fuente reporta tokens pero no dinero (p. ej. Claude Code con una
suscripción Pro/Max): el resultado es una **estimación** para comparar uso y valor, no lo
facturado. Precios en USD por millón de tokens, tarifa estándar de la API de Anthropic
(noviembre de 2026). Fuente: https://platform.claude.com/docs/en/about-claude/pricing
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MILLION = Decimal(1_000_000)
CACHE_WRITE_MULTIPLIER = Decimal("1.25")  # escritura de caché de 5 min = 1,25× la entrada
FAST_MODE_MULTIPLIER = Decimal(2)  # fast mode en Opus 5.5 = 2× la tarifa estándar


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input: Decimal
    output: Decimal
    cache_read: Decimal

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
        fast: bool = False,
    ) -> Decimal:
        total = (
            Decimal(input_tokens) * self.input
            + Decimal(cache_write_tokens) * self.input * CACHE_WRITE_MULTIPLIER
            + Decimal(cache_read_tokens) * self.cache_read
            + Decimal(output_tokens) * self.output
        ) / MILLION
        return total * FAST_MODE_MULTIPLIER if fast else total


def _p(inp: str, out: str, read: str | None = None) -> ModelPrice:
    i = Decimal(inp)
    return ModelPrice(i, Decimal(out), Decimal(read) if read else i / 10)


# Del prefijo más específico al más general: la primera coincidencia gana.
PRICES: tuple[tuple[str, ModelPrice], ...] = (
    ("claude-fable-5-2", _p("8", "40", "0.20")),
    ("claude-mythos-5-2", _p("8", "40", "0.20")),
    ("claude-fable-5", _p("10", "50")),
    ("claude-mythos-5", _p("10", "50")),
    ("claude-opus-5-5", _p("4", "20", "0.20")),
    ("claude-opus-5", _p("5", "25")),
    ("claude-opus-4", _p("5", "25")),
    ("claude-sonnet-5-5", _p("2", "10", "0.10")),
    ("claude-sonnet-5", _p("2", "10")),
    ("claude-sonnet-4", _p("3", "15")),
    ("claude-haiku-5-5", _p("0.10", "0.50")),
    ("claude-haiku-4", _p("1", "5")),
)
DEFAULT_PRICE = _p("4", "20", "0.20")  # modelo desconocido: se asume el Opus vigente


def price_for(model: str) -> ModelPrice:
    normalized = model.lower().removeprefix("anthropic.").split("@")[0]
    return next((price for prefix, price in PRICES if normalized.startswith(prefix)), DEFAULT_PRICE)
