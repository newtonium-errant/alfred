"""Truth pins for the instructor's scope + mutation-log story.

Every pin here asserts a MEASURED fact about master 8c5aac11. Several of
them pin a GAP (something that does NOT happen). Those are written so
that closing the gap turns them RED — that is the point: the day someone
threads ``scope=`` or ``session_path`` through, these fail and point at
the three docstrings that must be rewritten in the same commit
(``instructor/executor.py`` module docstring, the
``SCOPE_RULES["instructor"]`` comment, and
``_check_body_mutation_allowed``'s gcal-remedy caveat).

A gap pin is worthless without a positive control proving the detector
can see the opposite, so every one here carries its control inline —
usually the talker or batch call site, which DOES thread what the
instructor omits.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
from pathlib import Path

import pytest

from alfred.telegram import skill_audit
from alfred.vault import ops, scope
from alfred.vault.mutation_log import log_mutation

SRC = Path(__file__).resolve().parents[1] / "src" / "alfred"


def _call_kwargs(path: Path, func_name: str) -> list[list[str]]:
    """Every call to ``func_name`` in ``path``, as sorted kwarg-name lists."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == func_name:
                out.append(sorted(k.arg for k in node.keywords if k.arg))
    return out


# ---------------------------------------------------------------------------
# 1. The scope kwarg is NOT threaded into vault_edit from the instructor
# ---------------------------------------------------------------------------


def test_instructor_vault_edit_omits_scope_while_peers_thread_it() -> None:
    """GAP PIN + its own positive control in one test.

    The instructor calls ``ops.vault_edit`` with no ``scope=``, so every
    in-``ops`` ``check_scope`` (which sits behind ``if scope is not
    None``) is skipped: the per-type rules, the ``_BODY_MUTATE_DENIED_
    TYPES`` set, and the ``allow_body_*`` allowlists never fire on that
    path.

    The CONTROL is the same detector run against the talker and batch
    callers, which DO thread it. Without that half this pin would pass
    identically if ``_call_kwargs`` were broken and returned nothing.
    """
    instructor_calls = _call_kwargs(SRC / "instructor" / "executor.py", "vault_edit")
    assert instructor_calls, "detector found no vault_edit call in executor.py"
    assert all("scope" not in kws for kws in instructor_calls), (
        f"instructor now threads scope= into vault_edit ({instructor_calls}). "
        "If that is intended, this pin should be INVERTED and the three "
        "docstrings naming the gap must be rewritten in the same commit."
    )

    # POSITIVE CONTROL — the detector CAN see a scope kwarg when present.
    talker_calls = _call_kwargs(SRC / "telegram" / "conversation.py", "vault_edit")
    batch_calls = _call_kwargs(SRC / "batch" / "worker.py", "vault_edit")
    assert any("scope" in kws for kws in talker_calls), (
        "CONTROL FAILED: talker's vault_edit should thread scope="
    )
    assert any("scope" in kws for kws in batch_calls), (
        "CONTROL FAILED: batch worker's vault_edit should thread scope="
    )


def test_instructor_create_and_move_also_omit_scope() -> None:
    """Same gap on the other two write ops — the omission is uniform, not
    a one-call slip. Control: the calls exist at all."""
    ex = SRC / "instructor" / "executor.py"
    for fn in ("vault_create", "vault_move"):
        calls = _call_kwargs(ex, fn)
        assert calls, f"detector found no {fn} call in executor.py"
        assert all("scope" not in kws for kws in calls), (
            f"instructor now threads scope= into {fn} ({calls}) — see the "
            "module docstring, which claims it does not."
        )


# ---------------------------------------------------------------------------
# 2. The PRE-DISPATCH gate is live — and is op-level only
# ---------------------------------------------------------------------------


def test_predispatch_gate_cannot_deny_any_reachable_op() -> None:
    """THE GATE IS DEFENCE-IN-DEPTH, NOT A LIVE CONTROL — pinned so that
    stops being silently true.

    ``_TOOL_TO_OP`` is the complete set of ops a directive can reach, and
    every one of them is ``True`` for instructor, so ``check_scope`` can
    refuse nothing on this path today. Measured: neutering the gate reds
    0 of these 55 pins and 0 of 1168 across all instructor-touching test
    files — live, unpinned code.

    This pin is the tripwire. Map a denied op into ``_TOOL_TO_OP`` (or
    flip any reachable op to False) and it goes red, at which point the
    executor module docstring's "cannot currently deny anything"
    paragraph must be rewritten — AND a route-level refusal pin must land
    in the same commit. A TRIPWIRE SAYS "GO LOOK", NOT "THE GATE WORKS":
    all this one can see is the SHAPE of ``_TOOL_TO_OP`` × ``SCOPE_RULES``.
    It never drives a directive, so it cannot tell a gate that refuses from
    a gate that was rewired around. Rewriting the docstring alone would
    leave the first real refusal on this path unpinned. See the failure
    message for what that pin owes.

    The CONTROL is the ``delete`` half: the scope entry really does carry
    a False, so this test is asserting an all-True *reachable* set rather
    than an all-True dict — without it the pin would pass on a scope
    whose every key was True.
    """
    from alfred.instructor.executor import _TOOL_TO_OP

    rules = scope.SCOPE_RULES["instructor"]
    reachable = sorted(set(_TOOL_TO_OP.values()))
    denied_but_reachable = [op for op in reachable if rules.get(op) is not True]
    assert not denied_but_reachable, (
        f"these reachable ops are no longer all-allow: {denied_but_reachable}. "
        "The pre-dispatch gate can now refuse something. TWO things are owed "
        "in the SAME commit, not one. (1) Rewrite the 'cannot currently deny "
        "anything' paragraph in executor.py's module docstring, which this "
        "pin exists to keep honest. (2) ADD A REFUSAL PIN AT THE ROUTE: a "
        "test that drives a directive for one of these ops through the "
        "executor and asserts the gate ACTUALLY REFUSES it — asserting the "
        "refusal's REASON (the logged denial event and its reason field), not "
        "merely that something raised and the target went untouched, because "
        "a denial for an unrelated cause (unknown record, type-gate, bad "
        "path) reads identically and would leave the pin green against a "
        "build with no gate at all. This tripwire says GO LOOK; it does not "
        "say THE GATE WORKS."
    )
    # CONTROL — the scope entry is not vacuously all-True.
    assert rules.get("delete") is False, "instructor scope no longer denies delete"
    assert "delete" not in reachable, (
        "'delete' became reachable via _TOOL_TO_OP — the gate now has "
        "something to refuse, and the docstring is stale. Same two "
        "obligations as above, and this half is the likelier one to fire: "
        "the commit that makes delete reachable owes a route-level pin "
        "driving a delete directive and asserting the REASON it was refused, "
        "not just a docstring edit."
    )


def test_vault_delete_is_refused_by_the_tool_lookup_not_the_scope_gate() -> None:
    """ROUTE-LEVEL pin — drives ``_dispatch_tool`` rather than calling the
    predicate inline.

    The earlier version of this pin called
    ``check_scope("instructor","delete")`` directly, saw it raise, and
    concluded the gate protects the delete path. It does not: there is no
    ``vault_delete`` key in ``_TOOL_TO_OP``, so the tool LOOKUP refuses
    first and the gate is never consulted. Calling a predicate inline and
    inferring route behaviour is the same mistake this lane fixed twice
    elsewhere (the skill_audit discriminator, and its own rider) — pinned
    at the route so it cannot recur here.

    Positive control in the same test: a MAPPED tool executes past the
    lookup, proving the probe reaches the dispatch body at all.
    """
    import json

    from alfred.instructor.executor import _dispatch_tool

    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        (vault / "task").mkdir(parents=True)
        (vault / "task" / "x.md").write_text(
            "---\ntype: task\nname: X\n---\n\nbody\n", encoding="utf-8")

        refused = json.loads(_dispatch_tool(
            "vault_delete", {"path": "task/x.md"}, vault, False, None, []))
        assert refused.get("error", "").startswith("Unknown tool"), (
            f"expected the LOOKUP to refuse vault_delete, got {refused!r}"
        )
        # The file is untouched — the refusal is real, not cosmetic.
        assert (vault / "task" / "x.md").exists()

        # POSITIVE CONTROL — a mapped tool runs the dispatch body.
        ok = json.loads(_dispatch_tool(
            "vault_read", {"path": "task/x.md"}, vault, False, None, []))
        assert "frontmatter" in ok, (
            f"CONTROL FAILED: vault_read did not execute: {ok!r}"
        )


def test_vault_edit_tool_schema_has_no_type_so_predispatch_type_is_always_blank() -> None:
    """PREMISE PIN for the module docstring's claim that ``record_type``
    is always ``""`` at the pre-dispatch gate for an edit.

    The premise is a property of the TOOL SCHEMA, not of the gate: the
    executor reads ``tool_input.get("type", "")`` and the vault_edit
    schema declares no ``type`` property, so no directive can populate
    it. Control: vault_create's schema DOES declare one.
    """
    from alfred.instructor import executor as ex

    tools = {t["name"]: t for t in ex.VAULT_TOOLS}
    edit_props = tools["vault_edit"]["input_schema"]["properties"]
    assert "type" not in edit_props, (
        "vault_edit schema now has a `type` property — the pre-dispatch "
        "gate can now discriminate by type; update the module docstring."
    )
    # POSITIVE CONTROL — a schema in the same registry that DOES have it.
    assert "type" in tools["vault_create"]["input_schema"]["properties"], (
        "CONTROL FAILED: vault_create should declare a `type` property"
    )


# ---------------------------------------------------------------------------
# 3. Mutation logging is inert on the production path
# ---------------------------------------------------------------------------


def test_daemon_call_site_does_not_pass_session_path() -> None:
    """GAP PIN — the sole production caller omits ``session_path``, so
    every ``log_mutation`` in ``_dispatch_tool`` is a no-op and instructor
    mutations are ABSENT from the trail.

    "Sole caller" is exact: ``instructor/cli.py::cmd_run`` imports
    ``daemon.run`` as ``run_daemon`` and calls it, and that function
    contains the one ``execute_and_record`` call — so the CLI-foreground
    path funnels through the same site rather than being a second one.

    Control: the parameter exists and defaults to None (so the omission
    is meaningful rather than a misspelling), and the detector finds the
    call at all.
    """
    from alfred.instructor.executor import execute, execute_and_record

    calls = _call_kwargs(SRC / "instructor" / "daemon.py", "execute_and_record")
    assert len(calls) == 1, f"expected exactly one production call site, got {calls}"
    assert "session_path" not in calls[0], (
        "the daemon now threads session_path — instructor mutations are "
        "being logged; rewrite the module docstring's logging paragraph."
    )
    # CONTROL — the kwarg is real and optional on both entry points.
    for fn in (execute, execute_and_record):
        assert inspect.signature(fn).parameters["session_path"].default is None


def test_log_mutation_is_a_noop_without_a_session_path() -> None:
    """The mechanism behind the gap pin above, with a live control.

    Control runs FIRST and must actually write — a silent instrument
    would make the None-case assertion vacuous.
    """
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "session.jsonl"

        # POSITIVE CONTROL — a real path DOES produce a row carrying the scope.
        log_mutation(str(probe), "edit", "control/rec.md", scope="instructor")
        assert probe.exists(), "CONTROL FAILED: log_mutation wrote nothing"
        control_text = probe.read_text(encoding="utf-8")
        assert '"scope": "instructor"' in control_text, (
            f"CONTROL FAILED: scope not in row: {control_text!r}"
        )

        # THE GAP: session_path=None writes nothing at all.
        log_mutation(None, "edit", "prod/rec.md", scope="instructor")
        assert probe.read_text(encoding="utf-8") == control_text, (
            "log_mutation wrote a row for session_path=None"
        )


def test_audit_log_row_shape_carries_no_scope_field() -> None:
    """OMITTED-FIELD PIN on the LIVE path — ``data/vault_audit.log`` rows
    have no ``scope`` key for ANY tool, so the audit log cannot answer
    "what was allowed to write this record?" even when rows exist.

    Driven through the real writer (not by reading the source) so the
    pin proves the field is absent rather than proving the path is dead:
    the positive control asserts the row IS produced and carries its
    other four fields.
    """
    import json

    from alfred.vault.mutation_log import append_to_audit_log

    with tempfile.TemporaryDirectory() as td:
        audit = Path(td) / "vault_audit.log"
        append_to_audit_log(
            audit, "instructor",
            {"files_created": [], "files_modified": ["note/x.md"], "files_deleted": []},
            detail="directive-1",
        )
        # POSITIVE CONTROL — the row exists and is well-formed.
        assert audit.exists(), "CONTROL FAILED: no audit row written"
        row = json.loads(audit.read_text(encoding="utf-8").strip())
        assert set(row) == {"ts", "tool", "op", "path", "detail"}, (
            f"audit row shape changed: {sorted(row)}"
        )
        assert "scope" not in row


# ---------------------------------------------------------------------------
# 4. The C enumeration — pinned BOTH directions
# ---------------------------------------------------------------------------


def _edit(rtype: str, sc: str | None, extra_fm: str = "", **kwargs):
    """Run a real vault_edit against a temp vault. Returns 'ALLOW' or the
    ScopeError message."""
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        (vault / rtype).mkdir(parents=True, exist_ok=True)
        (vault / rtype / "rec.md").write_text(
            f"---\ntype: {rtype}\nname: Rec\n{extra_fm}---\n\n# Rec\n\nbody\nMARKER\n",
            encoding="utf-8",
        )
        try:
            ops.vault_edit(vault, f"{rtype}/rec.md", scope=sc, **kwargs)
            return "ALLOW"
        except scope.ScopeError as exc:
            return str(exc)


# The deny-set members that a directive could plausibly be parked on.
#
# DELIBERATE SUBSET, named as such: _BODY_MUTATE_DENIED_TYPES has 13
# members and the refusal applies to both body_replace and body_insert_at,
# so the full matrix is 13 x 2 = 26 combinations. DIRECTION-1 below drives
# body_replace across these 5 — a sampled 5 of 26 — chosen as the shapes an
# operator would plausibly park a directive on (a transcript, a learning
# atom, an operator-canonical commitment, a rendered-body record).
#
# NO body_insert_at row is driven ANYWHERE in this file. That surface
# appears here only in prose and in the SKILL-absence premise check below;
# there is no ``body_insert_at=`` kwarg in any drive. An earlier revision of
# this comment claimed "plus one body_insert_at row in the earlier
# enumeration" — false, and self-contradicting, since "5 of 26" is the right
# arithmetic only if no such row exists. Worth naming plainly: a comment
# written to state coverage HONESTLY is exactly where this lane's own defect
# class landed, so it now gets the same treatment as the pins it describes —
# every figure in it was recounted against the file and the registry rather
# than read. (13 = len(_BODY_MUTATE_DENIED_TYPES); 5 = len(_DENIED), each a
# member of it; both pinned in the premise test below.)
#
# The remaining combinations are covered by the same single code path:
# ``_check_body_mutation_allowed``'s membership test against that frozenset.
# That path does carry one per-type branch (``clinical_note``, itself a
# deny-set member), but it is gated on ``scope == "stayc_clinical"``, so
# under ``scope="instructor"`` it falls through to the same universal deny as
# every other member. Widen here if a per-type branch ever becomes REACHABLE
# FROM THIS SCOPE. This is UNDER-PINNING BY CHOICE, not a claim that only 5
# are refused.
_DENIED = ["session", "conversation", "decision", "preference", "routine"]


@pytest.mark.parametrize("rtype", _DENIED)
def test_threading_scope_would_deny_body_replace_on_denyset_types(rtype: str) -> None:
    """DIRECTION 1 of the enumeration: what threading WOULD refuse.

    Asserts the REASON (the universal-deny rule names itself), not just
    that something raised — a refusal for an unrelated cause reads
    identically otherwise.

    The ``scope=None`` half is the today-state control: the SAME write
    succeeds right now, which is precisely the gap the docstrings
    describe. If this half ever starts refusing, the gap closed by some
    other route and the docstrings are stale.
    """
    denied = _edit(rtype, "instructor", body_replace="NEW")
    assert denied != "ALLOW", f"{rtype} body_replace was allowed under instructor scope"
    assert "universally denied" in denied, (
        f"refused for the wrong reason on {rtype}: {denied!r}"
    )

    assert _edit(rtype, None, body_replace="NEW") == "ALLOW", (
        f"{rtype} body_replace already refuses with scope=None — the "
        "instructor path is no longer unguarded; update the docstrings."
    )


def test_threading_scope_would_deny_gcal_event_and_ingested_records() -> None:
    """DIRECTION 1, the two non-type-keyed rules, each asserting its own
    reason so they cannot be confused with the universal deny above."""
    gcal = _edit("event", "instructor", "gcal_event_id: abc123\n", body_replace="NEW")
    assert "gcal" in gcal.lower() or "calendar" in gcal.lower(), (
        f"gcal carve-out did not name itself: {gcal!r}"
    )
    assert _edit("event", None, "gcal_event_id: abc123\n", body_replace="NEW") == "ALLOW"

    ingested = _edit("note", "instructor", "ingested_via: web_ingest\n", body_replace="NEW")
    assert ingested != "ALLOW" and "body" in ingested.lower(), (
        f"ingest provenance lock did not fire or did not name itself: {ingested!r}"
    )
    assert _edit("note", None, "ingested_via: web_ingest\n", body_replace="NEW") == "ALLOW"


@pytest.mark.parametrize(
    "surface,kwargs",
    [
        ("set_fields", {"set_fields": {"tags": ["q2"]}}),
        ("append_fields", {"append_fields": {"tags": ["x"]}}),
        ("body_append", {"body_append": "appended text"}),
    ],
)
@pytest.mark.parametrize("rtype", _DENIED + ["note", "project", "task"])
def test_threading_scope_would_still_allow_every_skill_advertised_surface(
    surface: str, kwargs: dict, rtype: str,
) -> None:
    """DIRECTION 2 of the enumeration — the half that makes the proposal
    safe, and the half a one-directional battery would miss.

    ``vault-instructor/SKILL.md`` advertises vault_edit as
    ``{path, set_fields?, append_fields?, body_append?}``. All three stay
    ALLOWED under ``scope="instructor"``, INCLUDING on the body-mutate-
    denied types — so threading refuses nothing the SKILL instructs the
    instructor to do. Coverage of THIS pin, stated exactly: 8 types x 3
    surfaces = 24 rows (5 deny-set members + note/project/task).

    THAT IS NOT THE WHOLE RISK, and the difference is why the threading
    proposal stops for an operator ruling instead of shipping here: the
    two surfaces the deny set DOES refuse, ``body_replace`` and
    ``body_insert_at``, are absent from the SKILL line but PRESENT in the
    ``vault_edit`` tool DESCRIPTION, which the model reads at
    tool-selection time ("body_replace (full rewrite)"). So they are
    actively advertised and genuinely reachable — a directive such as
    "rewrite the summary section" parked on a session / routine /
    preference / decision record would succeed today and refuse after
    threading. Reachability is measured from the tool surface, not
    assumed absent from the SKILL's silence.
    """
    assert _edit(rtype, "instructor", **kwargs) == "ALLOW", (
        f"{surface} on {rtype} would be refused under instructor scope — "
        "this breaks a SKILL-advertised surface and the C proposal's "
        "safety argument with it."
    )


def test_skill_advertises_only_the_surfaces_that_stay_allowed() -> None:
    """PREMISE PIN for the test above, against an INDEPENDENT source.

    The safety argument rests on what the SKILL advertises. Read it from
    the bundled SKILL rather than restating it here, so the argument
    moves if the prompt layer moves. Sampled, not minted.
    """
    from alfred._data import get_skills_dir

    skill = (get_skills_dir() / "vault-instructor" / "SKILL.md").read_text(encoding="utf-8")
    edit_line = next(
        ln for ln in skill.splitlines() if ln.startswith("Input:") and "append_fields" in ln
    )
    for advertised in ("set_fields", "append_fields", "body_append"):
        assert advertised in edit_line, f"SKILL no longer advertises {advertised}"
    for not_advertised in ("body_replace", "body_insert_at"):
        assert not_advertised not in edit_line, (
            f"SKILL now advertises {not_advertised} on vault_edit — the C "
            "proposal's blast-radius argument (that threading refuses "
            "nothing the SKILL advertises) no longer holds as written."
        )


def test_body_mutate_denied_types_membership_against_schema_registry() -> None:
    """PREMISE PIN — the deny-set members used above are real record
    types, taken from the registry rather than minted by this test.

    Without this, a typo'd fixture type would make the DIRECTION-1 pins
    pass for the wrong reason (an unknown type refuses at gate 1).
    """
    from alfred.vault.schema import KNOWN_TYPES, LEARN_TYPES

    registry = set(KNOWN_TYPES) | set(LEARN_TYPES)
    for rtype in _DENIED:
        assert rtype in registry, f"{rtype!r} is not a registered record type"
        assert rtype in scope._BODY_MUTATE_DENIED_TYPES, (
            f"{rtype!r} left the body-mutation deny set"
        )


# ---------------------------------------------------------------------------
# 5. Rider — skill_audit's truthy/falsy discriminator
# ---------------------------------------------------------------------------


def _instance_raw(block):
    """A config that reaches the TypeError discriminator with ``instance``
    set to ``block``. ``bot_token`` + ``anthropic`` are present so the
    load failure is attributable to the instance block alone."""
    return {
        "telegram": {
            "bot_token": "DUMMY_TELEGRAM_TEST_TOKEN",
            "anthropic": {"api_key": "DUMMY_ANTHROPIC_TEST_KEY"},
            "instance": block,
        },
    }


@pytest.mark.parametrize("block", ["a string", [1, 2], 5, 3.5, True])
def test_truthy_non_dict_instance_block_never_reaches_the_discriminator(block) -> None:
    """MEASURED 2026-08-21, and it falsified the prescription this pin was
    written to satisfy.

    The prescription was to call this class "the LOUD branch". It is not:
    ``load_talker_config`` ACCEPTS a truthy non-dict (``_build`` assigns
    the raw value straight through), so no TypeError is raised and the
    discriminator is never entered. ``audit_skill`` then dereferences
    ``config.instance.name`` and raises AttributeError.

    Pinned as-is rather than "fixed" because the OUTCOME is already the
    correct one — see the companion test below, which drives the health
    surface and shows the caller converts this into a non-quiet WARN.
    The mechanism differs from the comment; the severity does not.
    """
    with pytest.raises(AttributeError, match="has no attribute 'name'"):
        skill_audit.audit_skill(_instance_raw(block))


@pytest.mark.parametrize("block", ["a string", [1, 2], 5, 3.5, True])
def test_truthy_non_dict_still_surfaces_LOUD_at_the_health_probe(block) -> None:
    """The severity half — what an operator actually sees.

    Uses the canonical ``is_attention_status`` predicate rather than
    re-localizing the ok/skip comparison (per the CLAUDE.md health-status
    rule). This is what makes the raise above acceptable to leave alone:
    the probe's ``except Exception -> WARN`` path is doing real work, and
    a malformed instance block does produce a card.
    """
    from alfred.brief.health_section import is_attention_status
    from alfred.telegram.health import _check_skill_capability_audit

    result = _check_skill_capability_audit(_instance_raw(block))
    assert is_attention_status(result.status.value), (
        f"malformed instance block {block!r} produced a QUIET status "
        f"({result.status.value}) — this is the misattribution-generator "
        "shape the 2026-08-20 fix closed for the other arm."
    )
    assert "AttributeError" in result.detail, (
        f"WARN detail should name the underlying cause: {result.detail!r}"
    )


@pytest.mark.parametrize("block", [None, "", [], 0, False, {}])
def test_falsy_non_dict_instance_block_takes_the_skip_branch(block) -> None:
    """The half the comment over-generalized about, now corrected.

    Behaviour is RIGHT here and always was — ``load_from_unified``
    coerces these same shapes with ``or {}``, so they are genuinely
    indistinguishable from an absent block and SKIP is correct. Only
    the sentence was wrong, and only for this half.
    """
    result = skill_audit.audit_skill(_instance_raw(block))
    assert result.instance_missing is True, (
        f"falsy non-dict {block!r} should take the SKIP branch, got "
        f"config_error={result.config_error}"
    )
    assert result.config_error is False
    assert result.is_clean, "the SKIP branch is deliberately quiet"
