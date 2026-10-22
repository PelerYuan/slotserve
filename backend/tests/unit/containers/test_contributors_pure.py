"""Contributors and the spec package do no I/O (A01 acceptance 3): no ``await``/``async``, no database, Docker or
HTTP client, nothing from ``slotserve.services``/``api``/``orm``
(the package must work without them, also on a node)."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "slotserve" / "containers"
FORBIDDEN = ("httpx", "sqlalchemy", "docker", "slotserve.services", "slotserve.api", "slotserve.orm", "slotserve.db")


def _files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _imports(tree: ast.AST, path: Path) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: resolve against the package
                parts = ["slotserve", "containers", *path.relative_to(PACKAGE).parent.parts]
                parts = parts[: len(parts) - (node.level - 1)]
                names.append(".".join([*parts, node.module or ""]).rstrip("."))
            else:
                names.append(node.module or "")
    return names


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.relative_to(PACKAGE).as_posix())
def test_no_io_in_the_spec_package(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Await | ast.AsyncFunctionDef | ast.AsyncFor | ast.AsyncWith), (
            f"{path.name}:{node.lineno}: no async code in the spec package"
        )
    for name in _imports(tree, path):
        assert not any(name == f or name.startswith(f + ".") for f in FORBIDDEN), f"{path.name} imports {name}"


def test_no_hook_comments_left():
    root = PACKAGE.parent
    for path in [*_files(), *(root / "services" / "devenv").rglob("*.py")]:
        text = path.read_text(encoding="utf-8")
        assert "storage hook" not in text and "ports hook" not in text, path
