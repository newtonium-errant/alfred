"""Arc #18 M6 — containment pins for the four writers outside the board family.

M0-M5 contained the routine/tier/board writers. These four compose a vault path
from a caller-supplied value and then WRITE, and until now did it ungated:

    daily_sync.reply_dispatch._resolve_attribution_correction   record_path
    audit.sweep.sweep_paths                                     each rel_path
    telegram.session.apply_substance_slug                       rel_path + new_slug
    instructor.executor.execute_and_record                      record_path

Each writer gets four pins, per the standard the main arc's findings set:

1. **Refusal asserts WHY.** Every escape pin reads the logged
   ``vault.containment.escape_denied`` event and asserts its ``writer`` and
   ``reason``. A refusal for an unrelated cause — a missing record, a malformed
   name, a shape check — produces the same outward result as the gate firing, so
   a pin that only checks "it refused" stays green against a build with no gate.
2. **Debris, not just contents.** The question is "did it TOUCH anything out
   there", not "did it write the file". A refused write can still create parent
   directories (``file_lock``'s ``mkdir(parents=True)`` did exactly that in the
   main arc). Every escape pin snapshots the entire out-of-vault tree and
   asserts byte-identity afterwards.
3. **The legitimate shape still works.** A containment gate that also breaks
   real paths is not a fix. Paired positive pins keep the gates honest.
4. **Production shape.** Every writer runs against ``symlinked_vault``, because
   the whole suite's ``tmp_path`` vaults are symlink-free, which makes
   ``.resolve()`` a no-op and hides an entire failure class by construction —
   that is what took down the routine verbs when M0-M3 shipped.

Not pinned here: the required-keyword-only gate template
(``tests/_required_kwarg.py``). M6 adds no new gate PARAMETER — each writer's
``vault_path`` was already required — so there is nothing for it to bind to.
``resolve_in_vault``'s own ``writer`` kwarg is pinned in
``tests/test_vault_path_containment.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.audit.sweep import sweep_paths
from alfred.daily_sync import reply_dispatch as rd
from alfred.daily_sync.assembler import ReplyCorrection
from alfred.telegram.session import apply_substance_slug
from alfred.telegram.state import StateManager

ESCAPE_EVENT = "vault.containment.escape_denied"


# ---------------------------------------------------------------------------
# Fixtures + the debris helper
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    """A vault with a REAL populated sibling directory outside it.

    The outside tree is real and non-empty on purpose: an escape that lands on
    a path which does not exist proves much less than one aimed at a directory
    a writer could plausibly clobber.
    """
    v = tmp_path / "vault"
    (v / "person").mkdir(parents=True)
    (v / "session").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pwned.md").write_text("ORIGINAL OUTSIDE CONTENT\n", encoding="utf-8")
    return v


@pytest.fixture()
def symlinked_vault(tmp_path: Path) -> Path:
    """A vault reached through a symlink — production's topology since
    2026-06-25. Returns the SYMLINK spelling, which is what config.yaml carries.
    """
    real = tmp_path / "data" / "algernon" / "alfred" / "vault"
    (real / "person").mkdir(parents=True)
    (real / "session").mkdir()
    link = tmp_path / "home_alfred_vault"
    link.symlink_to(real, target_is_directory=True)
    assert Path(link).resolve() != Path(link)  # precondition: really a symlink
    return link


def test_symlinked_vault_precondition_holds(symlinked_vault: Path) -> None:
    """Guard the guard: if this stops being a real symlink, every pin using it
    silently degrades into a duplicate of the plain-vault case."""
    assert symlinked_vault.is_symlink()
    assert symlinked_vault.resolve() != symlinked_vault


def _snapshot_outside(tmp_path: Path, vault: Path) -> dict[str, bytes | None]:
    """Every path under ``tmp_path`` that is NOT inside the vault, with contents.

    Directories map to ``None``. Comparing the whole mapping catches a created
    directory, a created file, AND a modified existing file — the three ways a
    refused write can still leave a mark outside the vault.
    """
    root = vault.resolve()
    out: dict[str, bytes | None] = {}
    for p in sorted(tmp_path.rglob("*")):
        if p.resolve() == root or root in p.resolve().parents:
            continue
        out[str(p)] = None if p.is_dir() else p.read_bytes()
    return out


def _escapes(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in captured if c.get("event") == ESCAPE_EVENT]


# ---------------------------------------------------------------------------
# Writer 1 — daily_sync attribution correction
# ---------------------------------------------------------------------------


def _attr_item(record_path: str) -> dict[str, Any]:
    return {
        "item_number": 1,
        "marker_id": "inf-20260803-salem-abc123",
        "record_path": record_path,
        "agent": "salem",
        "date": "2026-08-03T09:00:00+00:00",
        "section_title": "S",
    }


def test_attribution_escape_is_refused_with_reason(
    tmp_path: Path, vault: Path,
) -> None:
    before = _snapshot_outside(tmp_path, vault)
    corpus = tmp_path / "nested" / "attr_corpus.jsonl"

    with structlog.testing.capture_logs() as cap:
        err, did_write = rd._resolve_attribution_correction(
            ReplyCorrection(item_number=1, ok=True),
            _attr_item("../outside/pwned.md"),
            vault, str(corpus),
        )

    assert did_write is False
    assert err is not None and "not a path inside the vault" in err
    escapes = _escapes(cap)
    assert len(escapes) == 1
    assert escapes[0]["writer"] == "daily_sync.attribution.resolve_correction"
    assert escapes[0]["reason"] == "outside_vault_root"
    # The writer's OWN event too — the shared containment log names the gate,
    # this one names the operator-facing subsystem, and a grep for either has
    # to keep working (feedback_log_emission_test_pattern).
    local = [
        c for c in cap
        if c.get("event") == "daily_sync.attribution.path_escape_denied"
    ]
    assert len(local) == 1 and local[0]["marker_id"] == "inf-20260803-salem-abc123"
    # Debris: the outside tree is byte-identical, and the corpus (and the
    # directory it would have lived in) was never created.
    assert _snapshot_outside(tmp_path, vault) == before
    assert not corpus.exists() and not corpus.parent.exists()


def test_attribution_absolute_path_is_refused(
    tmp_path: Path, vault: Path,
) -> None:
    """An absolute record_path is the shape ``attribution_section``'s old
    ``except ValueError`` fallback could emit. pathlib would silently discard
    the vault root and honour it, so the gate refuses rather than joins."""
    before = _snapshot_outside(tmp_path, vault)
    with structlog.testing.capture_logs() as cap:
        err, did_write = rd._resolve_attribution_correction(
            ReplyCorrection(item_number=1, ok=True),
            _attr_item(str(tmp_path / "outside" / "pwned.md")),
            vault, str(tmp_path / "c.jsonl"),
        )
    assert did_write is False and err is not None
    assert _escapes(cap)[0]["reason"] == "outside_vault_root"
    assert _snapshot_outside(tmp_path, vault) == before


def test_attribution_legitimate_path_is_not_refused(
    tmp_path: Path, vault: Path,
) -> None:
    """Paired positive: a real in-vault record clears the gate. It then fails
    on its own terms (no matching marker), which is the point — the failure is
    the writer's, not the gate's."""
    (vault / "person" / "Ben.md").write_text(
        "---\ntype: person\nname: Ben\n---\n\nBody.\n", encoding="utf-8",
    )
    with structlog.testing.capture_logs() as cap:
        err, _ = rd._resolve_attribution_correction(
            ReplyCorrection(item_number=1, ok=True),
            _attr_item("person/Ben.md"),
            vault, str(tmp_path / "c.jsonl"),
        )
    assert _escapes(cap) == []
    assert err is None or "not a path inside the vault" not in err


def test_attribution_works_through_a_symlinked_vault(
    tmp_path: Path, symlinked_vault: Path,
) -> None:
    (symlinked_vault / "person" / "Ben.md").write_text(
        "---\ntype: person\nname: Ben\n---\n\nBody.\n", encoding="utf-8",
    )
    with structlog.testing.capture_logs() as cap:
        err, _ = rd._resolve_attribution_correction(
            ReplyCorrection(item_number=1, ok=True),
            _attr_item("person/Ben.md"),
            symlinked_vault, str(tmp_path / "c.jsonl"),
        )
    assert _escapes(cap) == []
    assert err is None or "not a path inside the vault" not in err


# ---------------------------------------------------------------------------
# Writer 2 — audit sweep
# ---------------------------------------------------------------------------


_RECORD_WITH_SOURCE = (
    "---\ntype: person\nname: Ben\n---\n\n"
    "- A claim about Ben. _source: talker conversation_\n"
)


def test_sweep_escape_is_refused_with_reason(tmp_path: Path, vault: Path) -> None:
    before = _snapshot_outside(tmp_path, vault)

    with structlog.testing.capture_logs() as cap:
        result = sweep_paths(vault, ["../outside/pwned.md"], apply=True)

    assert result.errors == [("../outside/pwned.md", "path escapes the vault root")]
    escapes = _escapes(cap)
    assert len(escapes) == 1
    assert escapes[0]["writer"] == "audit.sweep.infer_markers"
    assert escapes[0]["reason"] == "outside_vault_root"
    assert _snapshot_outside(tmp_path, vault) == before


def test_sweep_one_escape_does_not_abort_the_other_records(
    tmp_path: Path, vault: Path,
) -> None:
    """The refusal maps onto the EXISTING per-record error channel, so a poisoned
    entry costs that entry and nothing else. Raising instead would let one bad
    path silently drop an entire sweep's worth of legitimate work."""
    (vault / "person" / "Ben.md").write_text(_RECORD_WITH_SOURCE, encoding="utf-8")
    before = _snapshot_outside(tmp_path, vault)

    result = sweep_paths(
        vault, ["../outside/pwned.md", "person/Ben.md"], apply=True,
    )

    assert ("../outside/pwned.md", "path escapes the vault root") in result.errors
    # The good record was still processed — it produced a candidate and the
    # sweep reached its write path.
    assert result.marked >= 1 or result.candidates
    assert _snapshot_outside(tmp_path, vault) == before


def test_sweep_legitimate_path_still_sweeps(tmp_path: Path, vault: Path) -> None:
    (vault / "person" / "Ben.md").write_text(_RECORD_WITH_SOURCE, encoding="utf-8")
    with structlog.testing.capture_logs() as cap:
        result = sweep_paths(vault, ["person/Ben.md"], apply=True)
    assert result.errors == []
    assert _escapes(cap) == []


def test_sweep_works_through_a_symlinked_vault(symlinked_vault: Path) -> None:
    (symlinked_vault / "person" / "Ben.md").write_text(
        _RECORD_WITH_SOURCE, encoding="utf-8",
    )
    with structlog.testing.capture_logs() as cap:
        result = sweep_paths(symlinked_vault, ["person/Ben.md"], apply=True)
    assert result.errors == []
    assert _escapes(cap) == []


# ---------------------------------------------------------------------------
# Writer 3 — talker session substance-slug rename (TWO gated paths)
# ---------------------------------------------------------------------------


_SESSION_REL = "session/conversation-2026-08-04-old-slug-abc123.md"
_SESSION_BODY = (
    "---\ntype: session\nname: 'Conversation — 2026-08-04 old slug'\n---\n\nBody.\n"
)


def _state(tmp_path: Path) -> StateManager:
    return StateManager(tmp_path / "talker_state.json")


def test_session_src_escape_is_refused_with_reason(
    tmp_path: Path, vault: Path,
) -> None:
    """Escape via ``rel_path`` — caught by the SRC gate."""
    before = _snapshot_outside(tmp_path, vault)
    escaping = "../outside/conversation-2026-08-04-old-abc123.md"

    with structlog.testing.capture_logs() as cap:
        out = apply_substance_slug(
            _state(tmp_path), str(vault), escaping, "new-slug", "sess-1",
        )

    assert out == escaping  # unchanged — the rename never happened
    escapes = _escapes(cap)
    assert escapes and escapes[0]["writer"] == "talker.session.substance_slug.src"
    assert escapes[0]["reason"] == "outside_vault_root"
    local = [
        c for c in cap
        if c.get("event") == "talker.session.substance_slug_failed"
    ]
    assert len(local) == 1 and local[0]["stage"] == "path_escape"
    assert _snapshot_outside(tmp_path, vault) == before


def test_session_dst_escape_is_refused_with_reason(
    tmp_path: Path, vault: Path,
) -> None:
    """Escape via ``new_slug`` — the MODEL-generated input, and the only value in
    this arc that is neither operator- nor vault-derived. ``src`` is legitimate
    here, so this pin only passes if the DST gate exists too: a migration that
    gated one of the two paths would leave it red.
    """
    (vault / _SESSION_REL).write_text(_SESSION_BODY, encoding="utf-8")
    before = _snapshot_outside(tmp_path, vault)
    original = (vault / _SESSION_REL).read_bytes()

    with structlog.testing.capture_logs() as cap:
        out = apply_substance_slug(
            _state(tmp_path), str(vault), _SESSION_REL,
            "../../../../outside/evil", "sess-1",
        )

    assert out == _SESSION_REL
    escapes = _escapes(cap)
    assert escapes and escapes[0]["writer"] == "talker.session.substance_slug.dst"
    assert escapes[0]["reason"] == "outside_vault_root"
    assert _snapshot_outside(tmp_path, vault) == before
    # The source record is untouched too — the gate precedes the in-place
    # frontmatter rewrite, not just the rename.
    assert (vault / _SESSION_REL).read_bytes() == original


def test_session_legitimate_rename_still_works(tmp_path: Path, vault: Path) -> None:
    (vault / _SESSION_REL).write_text(_SESSION_BODY, encoding="utf-8")
    with structlog.testing.capture_logs() as cap:
        out = apply_substance_slug(
            _state(tmp_path), str(vault), _SESSION_REL, "new-slug", "sess-1",
        )
    assert _escapes(cap) == []
    assert out == "session/conversation-2026-08-04-new-slug-abc123.md"
    assert (vault / out).exists()
    assert not (vault / _SESSION_REL).exists()


def test_session_rename_works_through_a_symlinked_vault(
    tmp_path: Path, symlinked_vault: Path,
) -> None:
    """The shape that broke the routine verbs: the writer now holds RESOLVED
    paths while the configured root is still the symlink spelling."""
    (symlinked_vault / _SESSION_REL).write_text(_SESSION_BODY, encoding="utf-8")
    with structlog.testing.capture_logs() as cap:
        out = apply_substance_slug(
            _state(tmp_path), str(symlinked_vault), _SESSION_REL,
            "new-slug", "sess-1",
        )
    assert _escapes(cap) == []
    assert out == "session/conversation-2026-08-04-new-slug-abc123.md"
    assert (symlinked_vault / out).exists()


# ---------------------------------------------------------------------------
# Writer 4 — instructor execute_and_record
# ---------------------------------------------------------------------------


def _instructor_config(vault_root: Path, tmp_path: Path):
    from alfred.instructor.config import (
        AnthropicConfig,
        InstanceConfig,
        InstructorConfig,
        LoggingConfig,
        StateConfig,
        VaultConfig,
    )

    return InstructorConfig(
        vault=VaultConfig(path=str(vault_root)),
        anthropic=AnthropicConfig(
            api_key="DUMMY_ANTHROPIC_TEST_KEY",
            model="claude-sonnet-4-6",
            max_tokens=4096,
        ),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
        state=StateConfig(path=str(tmp_path / "instr_state.json")),
        logging=LoggingConfig(file=str(tmp_path / "instructor.log")),
        poll_interval_seconds=60,
        max_retries=3,
        audit_window_size=5,
    )


async def test_instructor_escape_is_refused_before_the_model_runs(
    tmp_path: Path, vault: Path,
) -> None:
    """``client=None`` is the assertion, not a shortcut: the gate short-circuits
    BEFORE ``execute`` is awaited, so a directive naming an out-of-vault record
    never reaches the model. If the gate moved below the call, this raises on
    the None client instead of returning cleanly."""
    from alfred.instructor.executor import execute_and_record
    from alfred.instructor.state import InstructorState

    before = _snapshot_outside(tmp_path, vault)
    with structlog.testing.capture_logs() as cap:
        result = await execute_and_record(
            client=None,
            directive="do a thing",
            record_path="../outside/pwned.md",
            config=_instructor_config(vault, tmp_path),
            state=InstructorState(tmp_path / "s.json"),
            skills_dir=tmp_path / "skills",
        )

    assert result.status == "error"
    assert "not inside the vault" in result.summary
    escapes = _escapes(cap)
    assert len(escapes) == 1
    assert escapes[0]["writer"] == "instructor.execute_and_record"
    assert escapes[0]["reason"] == "outside_vault_root"
    assert [
        c for c in cap if c.get("event") == "instructor.path_escape_denied"
    ]
    assert _snapshot_outside(tmp_path, vault) == before


async def test_instructor_absolute_record_path_is_refused(
    tmp_path: Path, vault: Path,
) -> None:
    from alfred.instructor.executor import execute_and_record
    from alfred.instructor.state import InstructorState

    before = _snapshot_outside(tmp_path, vault)
    result = await execute_and_record(
        client=None,
        directive="do a thing",
        record_path=str(tmp_path / "outside" / "pwned.md"),
        config=_instructor_config(vault, tmp_path),
        state=InstructorState(tmp_path / "s.json"),
        skills_dir=tmp_path / "skills",
    )
    assert result.status == "error"
    assert _snapshot_outside(tmp_path, vault) == before


async def test_instructor_gate_accepts_a_legitimate_path_on_a_symlinked_vault(
    tmp_path: Path, symlinked_vault: Path,
) -> None:
    """Standard (d) for the fourth writer — production's shape.

    The assertion is the RAISE TYPE, not a return value. A legitimate record on
    a symlinked vault must clear the gate and enter ``execute``, which then dies
    in ``_load_skill`` on the absent skills bundle. That FileNotFoundError is
    the proof of depth: if the containment gate had wrongly refused the resolved
    path, ``execute_and_record`` would have RETURNED an error result instead and
    never raised at all.
    """
    from alfred.instructor.executor import execute_and_record
    from alfred.instructor.state import InstructorState

    (symlinked_vault / "person" / "Ben.md").write_text(
        "---\ntype: person\nname: Ben\n---\n\nBody.\n", encoding="utf-8",
    )

    with structlog.testing.capture_logs() as cap:
        with pytest.raises(FileNotFoundError):
            await execute_and_record(
                client=None,
                directive="do a thing",
                record_path="person/Ben.md",
                config=_instructor_config(symlinked_vault, tmp_path),
                state=InstructorState(tmp_path / "s.json"),
                skills_dir=tmp_path / "absent_skills",
            )

    assert _escapes(cap) == []


def test_instructor_execute_no_longer_composes_a_dead_path() -> None:
    """``execute`` used to build ``md_path = vault_path / record_path`` and never
    read it. M6 removed the composition rather than gating it — a gate on a path
    nothing dereferences would be a refusal branch that can never fire, plus a
    pin that asserts nothing about production.

    Pinned by source inspection because there is no behaviour to observe: that
    IS the point. If someone reintroduces the composition, it needs a gate, and
    this fails to say so.
    """
    import inspect

    from alfred.instructor.executor import execute

    src = inspect.getsource(execute)
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    assert not any("md_path" in ln for ln in code_lines), (
        "execute() composes md_path again — either dereference it through "
        "resolve_in_vault or drop it; an ungated composition must not return"
    )


# ---------------------------------------------------------------------------
# The three lexical relative_to sites — a SOURCE-level drift guard, on purpose
# ---------------------------------------------------------------------------


def test_daily_sync_sections_do_not_use_lexical_relative_to() -> None:
    """``attribution_section`` (x2) and ``triage_section`` must compose their
    vault-relative strings through ``vault_relative``, never bare
    ``relative_to(vault_path)``.

    THIS IS A SOURCE PIN AND THAT IS DELIBERATE — a behavioural one would be
    hollow. Both sides of those comparisons are always the same spelling today:
    ``md_file`` comes from ``root.rglob("*.md")`` where ``root`` is derived from
    ``vault_path``, so the lexical form cannot fail. MEASURED while writing this
    (Python 3.12.3): ``rglob`` does not descend symlinked directories, so the
    walk can't even yield a path whose resolved location differs from its
    lexical one. There is no input that separates the old code from the new,
    which means any behavioural pin here would pass against BOTH and assert
    nothing.

    The change is preemptive: the moment an upstream writer hands these
    functions a RESOLVED path — which is exactly what arc #18 does to writers —
    the lexical form raises, and the ``except ValueError`` branches these
    replaced degraded SILENTLY with wrong values (an absolute ``record_path``
    for attribution, a hand-rebuilt ``task/<name>`` for triage). So what needs
    guarding is the shape of the code, and that is what this asserts.

    ``vault_relative`` itself is behaviourally pinned in
    ``tests/test_vault_path_containment.py::test_vault_relative_survives_a_symlinked_vault``.
    """
    import inspect

    from alfred.daily_sync import attribution_section, triage_section

    for module in (attribution_section, triage_section):
        src = inspect.getsource(module)
        offenders = [
            ln.strip() for ln in src.splitlines()
            if "relative_to(vault_path" in ln and not ln.lstrip().startswith("#")
        ]
        assert not offenders, (
            f"{module.__name__} uses lexical relative_to(vault_path): "
            f"{offenders}. Use vault_relative(vault_path, p) — it resolves BOTH "
            f"sides, so a resolved path from an upstream writer still works."
        )
        assert "vault_relative" in src, (
            f"{module.__name__} no longer imports/uses vault_relative at all"
        )
