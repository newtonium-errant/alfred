"""Ruling 1 (separable module): ``alfred.evstore`` imports stdlib + ``structlog`` ONLY — zero
``alfred.*`` imports. Extraction later is a ``git mv`` + a pyproject entry. This AST-scans every
module in the package so the property can't silently regress (a stray ``from alfred.x import y``
would couple the product back to STAY-C and defeat the whole separability ruling).
"""
from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1] / "src" / "alfred" / "evstore"
_ALLOWED_TOP_LEVEL = {
    "json", "hashlib", "fcntl", "os", "pathlib", "datetime", "dataclasses",
    "typing", "structlog", "__future__",
}


def _modules():
    return sorted(_PKG.glob("*.py"))


def test_package_exists_and_has_modules():
    mods = {p.name for p in _modules()}
    assert {"__init__.py", "store.py", "chain.py"} <= mods


def test_no_alfred_imports_anywhere_in_package():
    offenders = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "alfred":
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                # a relative import (level>0) stays inside the package — that's fine; an
                # absolute `from alfred...` couples back to the platform.
                if node.level == 0 and mod.split(".")[0] == "alfred":
                    offenders.append(f"{path.name}: from {mod} import ...")
    assert offenders == [], f"evstore must not import alfred.*: {offenders}"


def test_top_level_imports_are_stdlib_or_structlog_only():
    offenders = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in _ALLOWED_TOP_LEVEL:
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                top = (node.module or "").split(".")[0]
                if top and top not in _ALLOWED_TOP_LEVEL:
                    offenders.append(f"{path.name}: from {node.module} import ...")
    assert offenders == [], f"evstore may import stdlib + structlog only: {offenders}"
