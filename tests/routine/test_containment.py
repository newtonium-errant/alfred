"""Arc #18 M2 — routine-lane path containment.

Closes the audit's S2 finding: ``_routine_path`` composed
``vault/routine/<record>.md`` from an operator- OR LLM-supplied ``record``
with only an ``.exists()`` gate, giving any of the five routine verbs the
ability to mutate an arbitrary existing ``.md`` file anywhere on the box.
The talker passes the model's ``record`` string into argv unsanitised
(``telegram/conversation.py:2501`` -> ``:2516``; ``_dispatch_routine_item``
does the same for add/remove/edit).

Two pin classes here, and the distinction matters:

  * **Unit pins** on ``_routine_path`` / the writers, which prove the gate
    itself refuses.
  * **E2E pins through a production entry point** (``cmd_item_add`` etc.),
    which prove the gate is actually REACHED in production. Per builder.md's
    optional-gate rule, per-layer unit pins cannot catch a gate parameter that
    tests thread and production doesn't — only an e2e pin can. ``vault_path``
    is a required keyword precisely so that failure is a TypeError rather than
    a silent skip, and ``test_*_requires_vault_path`` pins that too.

Every refusal pin asserts the ``*_path_escape_denied`` EVENT, not merely that
the call refused. A refusal for an unrelated cause (missing record, unknown
item) is indistinguishable from the guard firing — that is how the M1 draft
went green against an ungated build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import yaml

from alfred.routine import completion as _completion
from alfred.routine.cli import _routine_path, cmd_done
from alfred.routine.cli_items import (
    _MutationResult,
    _atomic_item_mutate,
    cmd_item_add,
    cmd_item_edit,
    cmd_item_remove,
)
from alfred.routine.config import RoutineConfig

TODAY_ISO = "2026-07-25"

CANARY_BODY = (
    "---\ntype: routine\nname: Untouchable\n"
    "items:\n- text: A\n  priority: tracked\n---\n\n# do not write here\n"
)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "routine").mkdir(parents=True)
    return v


@pytest.fixture()
def canary(tmp_path: Path) -> Path:
    """An existing routine-shaped ``.md`` OUTSIDE the vault. It parses, it has
    an ``items`` list, it would satisfy every writer gate except containment."""
    p = tmp_path / "outside.md"
    p.write_text(CANARY_BODY, encoding="utf-8")
    return p


def _config(vault: Path, tmp_path: Path) -> RoutineConfig:
    config = RoutineConfig(vault_path=str(vault), instance_name="salem")
    config.state.path = str(tmp_path / "routine_state.json")
    return config


def _write_routine(vault: Path, name: str, payload: dict) -> Path:
    routine_dir = vault / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    fm_str = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    path = routine_dir / f"{name}.md"
    path.write_text(f"---\n{fm_str}---\n\n# {name}\n", encoding="utf-8")
    return path


def _denials(cap: list[dict], event: str) -> list[dict]:
    return [c for c in cap if c.get("event") == event]


#: Escaping a vault from ``routine/`` takes TWO levels — one out of ``routine/``,
#: one out of the vault. ``../x`` lands on ``<vault>/x.md``, still inside and
#: correctly allowed.
def _hostile(canary: Path) -> str:
    return f"../../{canary.stem}"


# ---------------------------------------------------------------------------
# _routine_path — the composition site
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        "../../outside",
        "../../../etc/passwd",
        "routine/../../outside",
        "../../../../../../tmp/evil",
    ],
)
def test_routine_path_refuses_escaping_record(vault: Path, record: str) -> None:
    """Escape refused as FileNotFoundError (preserving the caller contract that
    ``cli_items`` already handles) PLUS a distinct logged event, so the logs
    never conflate 'escaped' with 'genuinely missing'."""
    with structlog.testing.capture_logs() as cap:
        with pytest.raises(FileNotFoundError):
            _routine_path(vault, record)
    assert len(_denials(cap, "routine.cli.path_escape_denied")) == 1


@pytest.mark.parametrize(
    ("record", "lands_at"),
    [
        ("..", "routine/...md"),
        ("/etc/passwd", "routine/etc/passwd.md"),
    ],
)
def test_routine_prefix_neutralises_absolute_and_bare_dotdot(
    vault: Path, record: str, lands_at: str,
) -> None:
    """``_routine_path`` always prepends ``routine/``, so an ABSOLUTE record
    name cannot override the root the way ``Path(v) / "/etc/passwd"`` would —
    ``f"routine/{record}.md"`` stays relative and lands inside. Likewise a bare
    ``..`` becomes the file ``routine/...md``.

    These are NOT escapes and must not log one; they fail the ordinary
    ``.exists()`` check. Pinned because it is the non-obvious half of the
    threat model: ``..``-SEGMENTS are the only vector through this composer,
    and a future refactor that drops the literal prefix would silently open
    the absolute-override vector this test currently proves closed.
    """
    with structlog.testing.capture_logs() as cap:
        with pytest.raises(FileNotFoundError):
            _routine_path(vault, record)
    assert _denials(cap, "routine.cli.path_escape_denied") == []
    assert (vault / lands_at).resolve().is_relative_to(vault.resolve())


def test_routine_path_missing_record_is_NOT_an_escape(vault: Path) -> None:
    """The negative half: an ordinary missing record raises the SAME exception
    type but must NOT log an escape. Without this, the escape pin above would
    pass against a build that simply logged the event unconditionally."""
    with structlog.testing.capture_logs() as cap:
        with pytest.raises(FileNotFoundError):
            _routine_path(vault, "No Such Routine")
    assert _denials(cap, "routine.cli.path_escape_denied") == []


@pytest.mark.parametrize(
    "name",
    [
        "Self Care",
        "Verify FedEx Duty/Tax Charge",       # real vault shape: "/" in name
        "S.A.L.E.M. Backronym",                # real vault shape: dots
        "KAL-LE Scope — Curation Is Additive",  # real vault shape: em-dash
        "AppleCare+ Monthly $250",             # real vault shape: punctuation
    ],
)
def test_routine_path_accepts_real_record_names(vault: Path, name: str) -> None:
    """Blocklist-regression guard. These are shapes that EXIST in the
    production vault; a character-sanitising fix would break them."""
    target = vault / "routine" / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\ntype: routine\n---\n\n# x\n", encoding="utf-8")
    assert _routine_path(vault, name) == target.resolve()


# ---------------------------------------------------------------------------
# completion writers — INHERIT+ASSERT (defence in depth)
# ---------------------------------------------------------------------------


def test_mark_done_refuses_escaping_record_path(vault: Path, canary: Path) -> None:
    with structlog.testing.capture_logs() as cap:
        res = _completion.mark_routine_item_done(
            canary, "A", TODAY_ISO, vault_path=vault,
        )
    assert res.kind == _completion.DONE_KIND_UNKNOWN_RECORD
    assert res.changed is False
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    assert len(_denials(cap, "routine.completion.done.path_escape_denied")) == 1


def test_mark_undone_refuses_escaping_record_path(vault: Path, canary: Path) -> None:
    with structlog.testing.capture_logs() as cap:
        res = _completion.mark_routine_item_undone(
            canary, "A", TODAY_ISO, vault_path=vault,
        )
    assert res.kind == _completion.DONE_KIND_UNKNOWN_RECORD
    assert res.changed is False
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    assert len(_denials(cap, "routine.completion.undone.path_escape_denied")) == 1


@pytest.mark.parametrize(
    "fn", [_completion.mark_routine_item_done, _completion.mark_routine_item_undone],
)
def test_completion_writers_require_vault_path(vault: Path, fn) -> None:
    """``vault_path`` must be REQUIRED, never defaulted. A ``vault_path=None``
    default is the standing trap builder.md names: threaded in tests, absent in
    production, every pin green, containment silently dead in the field."""
    with pytest.raises(TypeError):
        fn(vault / "routine" / "X.md", "A", TODAY_ISO)  # type: ignore[call-arg]


def test_mark_done_still_writes_an_in_vault_record(vault: Path) -> None:
    """Happy path unchanged — an over-broad fix would be green on every escape
    pin while breaking the product."""
    rec = _write_routine(vault, "Chores", {
        "type": "routine", "name": "Chores",
        "items": [{"text": "A", "priority": "tracked"}],
    })
    res = _completion.mark_routine_item_done(rec, "A", TODAY_ISO, vault_path=vault)
    assert res.kind == _completion.DONE_KIND_SUCCESS
    assert res.changed is True


# ---------------------------------------------------------------------------
# _atomic_item_mutate — gate must precede the LOCK, not just the write
# ---------------------------------------------------------------------------


def test_atomic_item_mutate_refuses_escape_before_taking_the_lock(
    vault: Path, canary: Path, tmp_path: Path,
) -> None:
    """``file_rmw_lock`` does ``lock_path.parent.mkdir(parents=True)``, so a
    gate placed after it leaves a ``.lock`` sidecar at the out-of-vault target
    even on a refusal. Assert on the DEBRIS, not only the bytes."""
    def _mut(items, completion_log):
        return _MutationResult(
            items=items, completion_log=completion_log, payload_extras={},
        )

    with structlog.testing.capture_logs() as cap:
        with pytest.raises(Exception):
            _atomic_item_mutate(canary, _mut, vault_path=vault)

    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    assert not (tmp_path / f"{canary.stem}.lock").exists()
    assert not (tmp_path / f"{canary.name}.tmp").exists()
    assert len(_denials(cap, "routine.cli_items.path_escape_denied")) == 1


def test_atomic_item_mutate_requires_vault_path(vault: Path) -> None:
    rec = _write_routine(vault, "Chores", {"type": "routine", "items": []})

    def _mut(items, completion_log):
        return _MutationResult(
            items=items, completion_log=completion_log, payload_extras={},
        )

    with pytest.raises(TypeError):
        _atomic_item_mutate(rec, _mut)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# E2E through production entry points — the class that catches un-threaded gates
# ---------------------------------------------------------------------------


def test_e2e_item_add_cannot_escape(vault: Path, canary: Path, tmp_path: Path) -> None:
    """``alfred routine item add <record> <text>`` with a hostile record — the
    exact shape the talker's ``routine_item`` tool builds from an LLM-supplied
    record name."""
    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as cap:
        code = cmd_item_add(config, record_name=_hostile(canary), item_text="Z")
    assert code != 0
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    assert len(_denials(cap, "routine.cli.path_escape_denied")) == 1


def test_e2e_item_remove_cannot_escape(vault: Path, canary: Path, tmp_path: Path) -> None:
    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as cap:
        code = cmd_item_remove(config, record_name=_hostile(canary), item_text="A")
    assert code != 0
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    assert len(_denials(cap, "routine.cli.path_escape_denied")) == 1


def test_e2e_item_edit_cannot_escape(vault: Path, canary: Path, tmp_path: Path) -> None:
    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as cap:
        code = cmd_item_edit(
            config, record_name=_hostile(canary), item_text="A", new_text="B",
        )
    assert code != 0
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    assert len(_denials(cap, "routine.cli.path_escape_denied")) == 1


def test_e2e_done_cannot_escape(vault: Path, canary: Path, tmp_path: Path) -> None:
    """``alfred routine done <record> <item>`` — the ``routine_done`` tool's
    shape. The canary carries item ``A``, so on an ungated build this would be
    a genuine completion_log write to a file outside the vault."""
    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as cap:
        code = cmd_done(config, _hostile(canary), "A", today_override=TODAY_ISO)
    assert code != 0
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    assert len(_denials(cap, "routine.cli.path_escape_denied")) == 1


# ---------------------------------------------------------------------------
# SYMLINKED VAULT — the shape production actually runs in
# ---------------------------------------------------------------------------
#
# /home/andrew/alfred is a symlink to /data/algernon/alfred (since 2026-06-25)
# and config.yaml points at the SYMLINK spelling. Every other fixture in this
# repo builds its vault directly under tmp_path, where .resolve() is a no-op —
# so the resolved and configured spellings coincide and an entire class of bug
# is invisible.
#
# It bit immediately: arc #18 made the writers return RESOLVED paths while the
# callers still computed ``path.relative_to(vault_path)`` against the CONFIGURED
# spelling. ``relative_to`` is LEXICAL, so it raised ValueError and took down
# EVERY routine verb — with 11877 tests green. These fixtures exist so the next
# resolution-shaped change fails here instead of on the box.


@pytest.fixture()
def symlinked_vault(tmp_path: Path) -> Path:
    """A vault reached through a symlink, mirroring production's topology.

    Returns the SYMLINK spelling — what config.yaml would carry. The real
    directory lives elsewhere, so ``.resolve()`` genuinely changes the string.
    """
    real = tmp_path / "data" / "algernon" / "alfred" / "vault"
    (real / "routine").mkdir(parents=True)
    link = tmp_path / "home_alfred_vault"
    link.symlink_to(real, target_is_directory=True)
    assert Path(link).resolve() != Path(link)  # precondition: really a symlink
    return link


def test_symlinked_vault_precondition_holds(symlinked_vault: Path) -> None:
    """Guard the guard: if this ever stops being a real symlink, every pin
    below silently degrades into a duplicate of the plain-vault tests."""
    assert symlinked_vault.is_symlink()
    assert symlinked_vault.resolve() != symlinked_vault


@pytest.mark.parametrize(
    ("verb", "call"),
    [
        ("done", lambda cfg: cmd_done(cfg, "Chores", "A", today_override=TODAY_ISO)),
        ("item_add", lambda cfg: cmd_item_add(cfg, record_name="Chores", item_text="New")),
        ("item_remove", lambda cfg: cmd_item_remove(cfg, record_name="Chores", item_text="A")),
        ("item_edit", lambda cfg: cmd_item_edit(
            cfg, record_name="Chores", item_text="A", new_text="B")),
    ],
)
def test_every_routine_verb_works_on_a_symlinked_vault(
    symlinked_vault: Path, tmp_path: Path, verb: str, call,
) -> None:
    """The regression pin. Each verb must succeed when the vault is configured
    via a symlink — the exact production shape."""
    _write_routine(symlinked_vault, "Chores", {
        "type": "routine", "name": "Chores",
        "items": [{"text": "A", "priority": "tracked"}],
    })
    config = _config(symlinked_vault, tmp_path)
    assert call(config) == 0, f"{verb} failed on a symlinked vault"


def test_symlinked_vault_still_refuses_escapes(
    symlinked_vault: Path, canary: Path, tmp_path: Path,
) -> None:
    """Containment must hold through the symlink too — resolving both sides
    must not accidentally widen what counts as 'inside'."""
    config = _config(symlinked_vault, tmp_path)
    with structlog.testing.capture_logs() as cap:
        code = cmd_item_add(
            config, record_name=f"../../../../{canary.stem}", item_text="Z",
        )
    assert code != 0
    assert canary.read_text(encoding="utf-8") == CANARY_BODY
    assert len(_denials(cap, "routine.cli.path_escape_denied")) == 1


def test_symlinked_vault_reports_a_relative_path_not_an_absolute_one(
    symlinked_vault: Path, tmp_path: Path,
) -> None:
    """``vault_relative`` must yield the vault-relative string even when the
    two spellings differ — the canary payload/logs carry ``routine/Chores.md``,
    not a leaked absolute realpath."""
    from alfred.vault.paths import vault_relative

    _write_routine(symlinked_vault, "Chores", {
        "type": "routine", "name": "Chores",
        "items": [{"text": "A", "priority": "tracked"}],
    })
    resolved = symlinked_vault.resolve() / "routine" / "Chores.md"
    assert vault_relative(symlinked_vault, resolved) == "routine/Chores.md"


def test_e2e_item_add_still_works_for_a_real_record(vault: Path, tmp_path: Path) -> None:
    """The e2e happy path, so the escape pins above can't pass by breaking the
    verb outright."""
    import frontmatter

    _write_routine(vault, "Daily", {
        "type": "routine", "name": "Daily",
        "items": [{"text": "A", "priority": "tracked"}],
    })
    config = _config(vault, tmp_path)
    assert cmd_item_add(config, record_name="Daily", item_text="New") == 0
    fm = frontmatter.load(str(vault / "routine" / "Daily.md")).metadata
    assert [i["text"] for i in fm["items"]] == ["A", "New"]
