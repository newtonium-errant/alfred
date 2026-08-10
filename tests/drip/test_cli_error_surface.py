"""#66 — the drip CLI's operator-facing error surface.

``campaign_state_path`` refuses to build an unscoped state path when the
instance name is unset (a real guard: on the box every instance shares one
WorkingDirectory, so an unscoped path silently corrupts cursors across
instances). It signalled that refusal with a bare ``ValueError``.

``cmd_drip`` — the function the top-level dispatcher actually calls — caught
only ``DripConfigError``, so the refusal escaped as a raw traceback. The
operator saw a stack dump for what is simply a missing line of config.

These pins drive the REAL entry point rather than ``campaign_state_path``
directly, because the defect lived in the gap BETWEEN the two: the state layer
raised correctly and the guard worked exactly as designed. Only a test that
crosses the CLI boundary can see the difference between a clean refusal and a
traceback.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from alfred import cli as top_cli
from alfred.drip.wiring import DripConfigError


def _raw(*, instance_name: str | None, worklist: Path, vault: Path, data: Path) -> dict:
    """A unified config shaped like a real one.

    ``instance_name=None`` omits ``telegram.instance.name`` entirely — the
    shape a fresh instance's YAML has before the operator fills it in, and the
    one that trips the state-path guard.
    """
    raw: dict = {
        "vault": {"path": str(vault)},
        "logging": {"dir": str(data)},
        "drip": {"campaigns": {"link001_repair": {
            "enabled": True,
            "worklist_path": str(worklist),
            "max_items_per_run": 5,
        }}},
    }
    if instance_name is not None:
        raw["telegram"] = {"instance": {"name": instance_name}}
    return raw


def _run_cmd_drip(monkeypatch, raw: dict, subcmd: str = "run") -> None:
    monkeypatch.setattr(top_cli, "_load_unified_config", lambda _p: raw)
    monkeypatch.setattr(top_cli, "_setup_logging_from_config", lambda *a, **k: None)
    ns = argparse.Namespace(
        config="ignored.yaml", drip_cmd=subcmd, campaign=None,
        dry_run=True, apply=False,
    )
    top_cli.cmd_drip(ns)


@pytest.fixture
def worklist(tmp_path: Path) -> Path:
    p = tmp_path / "wl.txt"
    p.write_text("note/A.md::person/Gone::remove\n", encoding="utf-8")
    return p


@pytest.mark.parametrize("subcmd", ["run", "status", "repair-verify"])
def test_unset_instance_name_is_a_clean_refusal_not_a_traceback(
    subcmd, worklist, tmp_path, monkeypatch, capsys,
) -> None:
    """THE #66 pin, across all three subcommands that build a state path.

    Parametrized because the three handlers each reach ``campaign_state_path``
    on their own line — fixing one and leaving the others is the shape of this
    bug, and a single-subcommand pin would not notice.

    Asserts the EXIT PATH, not just the message: a bare ``ValueError`` escaping
    ``cmd_drip`` never reaches ``sys.exit`` at all, so ``SystemExit`` is what
    actually separates a handled refusal from a traceback.
    """
    raw = _raw(
        instance_name=None, worklist=worklist,
        vault=tmp_path / "vault", data=tmp_path / "data",
    )

    with pytest.raises(SystemExit) as exc:
        _run_cmd_drip(monkeypatch, raw, subcmd)

    assert exc.value.code == 2, "a config problem the operator must fix exits 2"
    out = capsys.readouterr().out
    assert out.startswith("Drip:"), f"operator-facing prefix missing: {out!r}"
    assert "instance name" in out, "the message must name what to fix"


def test_the_refusal_is_a_drip_config_error_at_the_state_layer(tmp_path: Path) -> None:
    """The fix is at the SOURCE, not per-call-site.

    ``campaign_state_path`` raises ``DripConfigError`` directly, so every
    caller — the three CLI handlers today and any added later — gets the clean
    surface without having to remember a wider ``except``. A fix applied only
    at the three call sites would be one forgotten line away from regressing.

    ``DripConfigError`` subclasses ``ValueError``, so anything that already
    catches ``ValueError`` here (``brief/daemon.py`` does) is unaffected.
    """
    from alfred.drip.state import campaign_state_path

    with pytest.raises(DripConfigError) as exc:
        campaign_state_path(tmp_path, "", "link001_repair")
    assert "instance name" in str(exc.value)
    assert isinstance(exc.value, ValueError), "must stay ValueError-compatible"


def test_a_named_instance_still_builds_a_scoped_path(tmp_path: Path) -> None:
    """The guard still guards — this is not a widening.

    Without this, a 'fix' that simply stopped refusing would pass the pins
    above while reintroducing the cross-instance cursor corruption the guard
    exists to prevent.
    """
    from alfred.drip.state import campaign_state_path

    path = campaign_state_path(tmp_path, "Salem", "link001_repair")
    assert "salem" in str(path).lower()
    assert path.name == "link001_repair_state.json"
