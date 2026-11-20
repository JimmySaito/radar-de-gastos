"""El empaquetado de Lambda debe ser reproducible: mismo contenido, mismos bytes.

Antes, pip extraía las dependencias con la fecha del momento y el hash de layer.zip cambiaba
en cada build, así que Terraform reemplazaba la capa en cada despliegue sin necesidad.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import zipfile
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _build_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_lambda", ROOT / "scripts" / "build_lambda.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(base: Path) -> Path:
    (base / "python" / "pkg" / "__pycache__").mkdir(parents=True)
    (base / "python" / "pkg" / "__init__.py").write_text("x = 1\n")
    (base / "python" / "pkg" / "mod.py").write_text("y = 2\n")
    (base / "python" / "pkg" / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"\x00")
    return base


def test_same_content_gives_identical_zip(tmp_path: Path) -> None:
    build = _build_module()
    src = _tree(tmp_path / "src")
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"

    build.zip_dir(src, first, src)
    for path in src.rglob("*.py"):  # otra extracción de pip: mismas bytes, otra fecha
        os.utime(path, (1_900_000_000, 1_900_000_000))
    build.zip_dir(src, second, src)

    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )


def test_zip_entries_are_portable_and_without_bytecode(tmp_path: Path) -> None:
    build = _build_module()
    src = _tree(tmp_path / "src")
    target = tmp_path / "out.zip"
    build.zip_dir(src, target, src)

    with zipfile.ZipFile(target) as zf:
        infos = zf.infolist()
    names = [i.filename for i in infos]
    assert names == ["python/pkg/__init__.py", "python/pkg/mod.py"]  # orden fijo, separador "/"
    assert {i.date_time for i in infos} == {build.FIXED_DATE}


def test_content_change_changes_the_zip(tmp_path: Path) -> None:
    build = _build_module()
    src = _tree(tmp_path / "src")
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    build.zip_dir(src, first, src)
    (src / "python" / "pkg" / "mod.py").write_text("y = 3\n")
    build.zip_dir(src, second, src)
    assert first.read_bytes() != second.read_bytes()
