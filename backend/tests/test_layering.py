"""Import direction: the lower layers (services, runtime, cluster, gateway) must not import the HTTP layer (api).

`api` is the outermost layer: routers call services, never the other way round. Code that a service needs from
`api` (e.g. the booking `*_core` functions) belongs in `services/`.

The check is strict since the booking `*_core` functions moved to `services/reservations.py` (W1-03): a new
import of `api` from a lower layer fails the test. Function-level ("lazy") imports inside the package are counted
and reported too, since most of them exist to hide such cycles.
"""

from __future__ import annotations

import ast
import os
import warnings
from dataclasses import dataclass
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "slotserve"
LOWER_LAYERS = ("services", "runtime", "cluster", "gateway")
FORBIDDEN = "slotserve.api"
STRICT = True


@dataclass(frozen=True)
class Import:
    module: str  # importing module, e.g. slotserve.services.chat.booking
    target: str  # imported module, e.g. slotserve.api.reservations
    line: int
    lazy: bool  # inside a function

    def __str__(self) -> str:
        where = "function-level " if self.lazy else ""
        return f"{self.module}:{self.line} {where}imports {self.target}"


def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(source: str, module: str, *, is_package: bool = False) -> list[Import]:
    """Absolute names of everything a module imports (`from x import y` yields x.y as well, y may be a module)."""
    found: list[Import] = []
    package = module if is_package else module.rpartition(".")[0]

    def visit(node: ast.AST, lazy: bool) -> None:
        for child in ast.iter_child_nodes(node):
            inner = lazy or isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
            if isinstance(child, ast.Import):
                found.extend(Import(module, alias.name, child.lineno, lazy) for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    base_parts = package.split(".")
                    base_parts = base_parts[: len(base_parts) - (child.level - 1)]
                    base = ".".join(base_parts + ([child.module] if child.module else []))
                else:
                    base = child.module or ""
                found.append(Import(module, base, child.lineno, lazy))
                found.extend(Import(module, f"{base}.{alias.name}", child.lineno, lazy) for alias in child.names)
            visit(child, inner)

    visit(ast.parse(source), False)
    return found


def package_imports(root: Path = PACKAGE) -> list[Import]:
    result: list[Import] = []
    for path in sorted(root.rglob("*.py")):
        result.extend(
            imports_of(
                path.read_text(encoding="utf-8"),
                module_name(path, root),
                is_package=path.name == "__init__.py",
            )
        )
    return result


def is_api(target: str) -> bool:
    return target == FORBIDDEN or target.startswith(FORBIDDEN + ".")


def layer_violations(found: list[Import]) -> list[Import]:
    lower = tuple(f"slotserve.{layer}" for layer in LOWER_LAYERS)
    seen: set[tuple[str, int]] = set()
    out = []
    for imp in found:
        in_lower = any(imp.module == p or imp.module.startswith(p + ".") for p in lower)
        if in_lower and is_api(imp.target) and (imp.module, imp.line) not in seen:
            seen.add((imp.module, imp.line))
            out.append(imp)
    return out


def test_scanner_resolves_relative_imports():
    """The scanner itself is checked strictly, so report-only mode cannot hide a broken scanner."""
    source = (
        "from ..api.models import x\n"
        "from .. import api\n"
        "import slotserve.api.keys\n"
        "from ..services import booking\n"
        "def f():\n"
        "    from ...api.reservations import create_core\n"
    )
    found = imports_of(source, "slotserve.services.catalog_deploy")
    nested = imports_of(source, "slotserve.services.chat.booking")
    assert [str(v) for v in layer_violations(found)] == [
        "slotserve.services.catalog_deploy:1 imports slotserve.api.models",
        "slotserve.services.catalog_deploy:2 imports slotserve.api",
        "slotserve.services.catalog_deploy:3 imports slotserve.api.keys",
    ]
    assert [str(v) for v in layer_violations(nested)][-1] == (
        "slotserve.services.chat.booking:6 function-level imports slotserve.api.reservations"
    )
    assert not layer_violations(imports_of(source, "slotserve.api.admin"))
    pkg = imports_of("from .devenv import x\n", "slotserve.services", is_package=True)
    assert pkg[0].target == "slotserve.services.devenv"


def test_lower_layers_do_not_import_api():
    found = package_imports()
    violations = layer_violations(found)
    lazy = [imp for imp in found if imp.lazy and imp.target.startswith("slotserve")]
    lazy_lines = {(imp.module, imp.line) for imp in lazy}
    report = [
        f"{len(violations)} import(s) from {', '.join(LOWER_LAYERS)} into api (target: 0):",
        *(f"  {v}" for v in violations),
        f"{len(lazy_lines)} function-level import statement(s) inside slotserve (target: <= 20)",
    ]
    print("\n".join(report))
    strict = STRICT or os.environ.get("SLOTSERVE_LAYERING_STRICT") == "1"
    if violations and not strict:
        warnings.warn("\n".join(report), stacklevel=1)
    if strict:
        assert not violations, "\n".join(report)
