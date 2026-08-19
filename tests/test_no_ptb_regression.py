"""No-PTB regression pins — python-telegram-bot must never come back (T4 C5).

Telegram retired 2026-08-18 (tokens revoked); the bot surface was deleted
2026-08-19 (T4 C3/C4) and the ``python-telegram-bot`` dependency dropped from
pyproject's ``voice`` extra (C5). PTB may STILL BE INSTALLED in a dev venv
(removing a dep from pyproject uninstalls nothing), which is exactly why
"the import happens to work" proves nothing — these pins make reappearance
fail loudly on three independent surfaces:

  1. STATIC — no module under ``src/alfred`` imports ``telegram``/PTB, at
     any nesting depth (AST walk, so function-local lazy imports are caught
     — the shape that hid ``tts.py``'s ``from telegram import InputFile``
     from top-level grep).
  2. DYNAMIC — the suite-wide meta_path blocker (tests/conftest.py) turns
     any runtime ``import telegram`` into an ImportError, with its own
     positive AND negative controls here (a guard that cannot fire is not a
     guard; a guard that over-fires on ``alfred.telegram`` would break the
     whole suite).
  3. DECLARED — pyproject.toml carries no python-telegram-bot requirement
     on any dependency list.

Per ``feedback_regression_pin_unconditional.md``: no importorskip, no
conditional skips — these run on every suite invocation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src" / "alfred"


def _ptb_imports_in(path: Path) -> list[str]:
    """Every PTB import in ``path``, as ``file:line:module`` strings.

    Walks the FULL AST (not just module top-level) so lazy function-local
    imports are caught too.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "telegram" or alias.name.startswith("telegram."):
                    hits.append(f"{path}:{node.lineno}:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # level > 0 is a relative import — always inside alfred, never PTB.
            if node.level == 0 and (mod == "telegram" or mod.startswith("telegram.")):
                hits.append(f"{path}:{node.lineno}:{mod}")
    return hits


def test_no_src_module_imports_ptb() -> None:
    """STATIC pin: ``src/alfred`` is PTB-free at every nesting depth."""
    files = sorted(_SRC.rglob("*.py"))
    # Positive control on the instrument: the walk must actually be looking
    # at a real tree — an empty selection would make this pin vacuously
    # green (the exclusion-pin-needs-a-positive-control rule).
    assert len(files) > 100, (
        f"AST sweep found only {len(files)} files under {_SRC} — the "
        "selection is broken, so a green result would mean nothing."
    )
    hits = [h for f in files for h in _ptb_imports_in(f)]
    assert hits == [], (
        "python-telegram-bot imports found in src/alfred — the Telegram "
        f"surface is retired (T4, 2026-08-19): {hits}"
    )


def test_instrument_control_the_walker_sees_a_planted_import(tmp_path: Path) -> None:
    """The AST walker itself gets a positive control: a planted lazy import
    MUST be found, or the static pin above is a scanner that cannot see."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "def f():\n    from telegram import Update\n    return Update\n",
        encoding="utf-8",
    )
    hits = _ptb_imports_in(planted)
    assert len(hits) == 1 and hits[0].endswith(":telegram")


def test_runtime_import_of_ptb_is_blocked() -> None:
    """DYNAMIC pin + the blocker's positive control: importing PTB inside the
    suite raises, and the message names the retirement (proving OUR blocker
    fired rather than PTB merely being absent from the venv)."""
    assert "telegram" not in sys.modules, (
        "PTB is already loaded — something imported it before the blocker "
        "could refuse, which means the suite ran PTB code."
    )
    with pytest.raises(ImportError, match="[Tt]elegram.*retired"):
        import telegram  # noqa: F401


def test_blocker_does_not_over_block_our_own_package() -> None:
    """NEGATIVE control: ``alfred.telegram`` (our package, PTB's near-name
    neighbour) must import cleanly — a blocker keyed on a prefix instead of
    the exact top-level name would take the whole talker down with it."""
    import alfred.telegram.config as cfg

    assert cfg.__name__ == "alfred.telegram.config"


def test_pyproject_declares_no_ptb_dependency() -> None:
    """DECLARED pin: no dependency list re-grows python-telegram-bot.

    Parsed, not grepped: pyproject legitimately MENTIONS the package in the
    removal comment (name-match is not referent-match — the referent here is
    a requirement string in a dependency array).
    """
    import tomllib

    data = tomllib.loads(
        (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    deps = list(data["project"].get("dependencies", []))
    optional = data["project"].get("optional-dependencies", {})
    for group, reqs in optional.items():
        deps.extend(f"[{group}] {r}" for r in reqs)
    # Positive control: we parsed the real manifest, and the voice extra —
    # the group PTB was removed FROM — still exists and is non-empty.
    assert deps, "parsed no dependencies at all — wrong file or broken parse"
    assert any(d.startswith("[voice]") for d in deps), (
        "the voice extra vanished — this pin expected to sweep the group "
        "python-telegram-bot was removed from"
    )
    offenders = [d for d in deps if "python-telegram-bot" in d]
    assert offenders == [], (
        "python-telegram-bot reappeared in pyproject dependency lists — the "
        f"Telegram surface is retired: {offenders}"
    )
