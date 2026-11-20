"""El dashboard debe funcionar con la CSP estricta que aplican CloudFront y la API.

La política (script-src 'self'; style-src 'self') bloquea en el navegador los atributos
style="", los manejadores on*="" y los <script> en línea. En local no hay CSP, así que un
estilo en línea se ve bien en desarrollo y falla en AWS (las barras de "Gasto por persona"
quedaron vacías por esto). Los estilos dinámicos usan data-bg / data-w (ver app.js).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
FILES = ["index.html", "app.js"]


@pytest.mark.parametrize("name", FILES)
def test_no_inline_styles_or_handlers(name: str) -> None:
    text = (FRONTEND / name).read_text(encoding="utf-8")
    offending = [
        f"{n}: {line.strip()[:80]}"
        for n, line in enumerate(text.splitlines(), start=1)
        if re.search(r'\sstyle="|\son[a-z]+="|<script>(?!</script>)', line)
    ]
    assert not offending, "La CSP bloquearía:\n" + "\n".join(offending)


def test_dynamic_styles_are_applied_by_script() -> None:
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "function applyDynamicStyles" in app
    assert "data-bg=" in app and "data-w=" in app
