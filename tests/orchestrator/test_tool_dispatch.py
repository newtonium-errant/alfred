"""Dispatch table tests — ``TOOL_RUNNERS`` contents and signature branching.

``run_all`` picks the arity based on a hard-coded tuple literal in
``start_process`` (``tool in ("surveyor", ...)``). These tests guard
the contract three ways:

  1. ``test_tool_runners_covers_every_registered_tool`` — TOOL_RUNNERS
     keys match EXPECTED_TOOLS. Forces test-update when a new
     daemon ships.
  2. ``test_two_arg_runners_have_expected_signature`` /
     ``test_three_arg_runners_have_expected_signature`` — each runner
     IS the arity its set claims. Catches an arity drift in the
     runner definition itself.
  3. ``test_dispatcher_two_arg_branch_matches_two_arg_tools`` — the
     dispatcher's ``if tool in (...)`` literal matches TWO_ARG_TOOLS
     exactly. **Load-bearing**: catches the regression class where a
     new two-arg runner gets registered in TOOL_RUNNERS + listed in
     TWO_ARG_TOOLS, but the author forgets to add it to the
     dispatcher tuple, and the orchestrator passes 3 args to a 2-arg
     runner on first spawn (digest pre-fix; radar_day before
     1b95015; friction_analyzer before 5423fb1; same shape three
     times — the documentation-trigger pattern).
"""

from __future__ import annotations

import inspect
import re

import alfred.orchestrator as orchestrator


EXPECTED_TOOLS = {
    "curator", "janitor", "distiller", "instructor",
    "surveyor", "mail", "brief", "talker",
    "bit", "daily_sync", "brief_digest_push", "digest",
    # Pending Items Queue Phase 1 — periodic flush + outbound-failure
    # detector. Auto-starts on instances with a ``pending_items`` block
    # + ``enabled: true``.
    "pending_items_pusher",
    # KAL-LE distiller-radar Phase 3 — daily radar auto-fire daemon.
    # Auto-starts on instances with ``distiller.radar_day.enabled``.
    "radar_day",
    # KAL-LE Daily Sync K3 — friction analyzer (reads bash_exec.jsonl,
    # writes friction events for the section provider). Auto-starts
    # on instances with ``daily_sync.friction_analyzer.enabled``.
    "friction_analyzer",
    # Cloudflared tunnel supervisor — wraps the ``cloudflared`` Go
    # binary so the Outlook → mail webhook tunnel auto-restarts with
    # the other daemons. Auto-starts on instances with
    # ``cloudflared.enabled: true``.
    "cloudflared",
}

TWO_ARG_TOOLS = {
    "surveyor", "mail", "brief", "bit",
    "daily_sync", "brief_digest_push", "digest",
    "pending_items_pusher",
    "radar_day",
    "friction_analyzer",
    "cloudflared",
}
THREE_ARG_TOOLS = {"curator", "janitor", "distiller", "instructor", "talker"}


def test_tool_runners_covers_every_registered_tool() -> None:
    """Every tool mentioned in the auto-start logic is in TOOL_RUNNERS."""
    assert set(orchestrator.TOOL_RUNNERS.keys()) == EXPECTED_TOOLS


def test_tool_runners_partitions_between_two_arg_and_three_arg() -> None:
    """Two-arg and three-arg sets partition the registered runners."""
    assert TWO_ARG_TOOLS | THREE_ARG_TOOLS == EXPECTED_TOOLS
    assert TWO_ARG_TOOLS & THREE_ARG_TOOLS == set()


def test_two_arg_runners_have_expected_signature() -> None:
    """surveyor/mail/brief runners take ``(raw, suppress_stdout)``."""
    for tool in TWO_ARG_TOOLS:
        runner = orchestrator.TOOL_RUNNERS[tool]
        sig = inspect.signature(runner)
        params = list(sig.parameters.values())
        # Two positional-or-keyword params: raw, suppress_stdout
        assert len(params) == 2, (
            f"{tool} runner expected 2 params, got {len(params)} "
            f"({[p.name for p in params]})"
        )
        assert params[0].name == "raw", f"{tool}: first param must be 'raw'"
        assert params[1].name == "suppress_stdout", (
            f"{tool}: second param must be 'suppress_stdout'"
        )


def test_three_arg_runners_have_expected_signature() -> None:
    """curator/janitor/distiller/talker runners take ``(raw, skills_dir, suppress_stdout)``."""
    for tool in THREE_ARG_TOOLS:
        runner = orchestrator.TOOL_RUNNERS[tool]
        sig = inspect.signature(runner)
        params = list(sig.parameters.values())
        assert len(params) == 3, (
            f"{tool} runner expected 3 params, got {len(params)} "
            f"({[p.name for p in params]})"
        )
        assert params[0].name == "raw"
        assert params[1].name == "skills_dir"
        assert params[2].name == "suppress_stdout"


def test_missing_deps_exit_code_constant() -> None:
    """The exit-78 contract is a named constant (no magic numbers)."""
    # The contract is load-bearing for surveyor etc. — constant should not drift.
    assert orchestrator._MISSING_DEPS_EXIT == 78


def test_tool_runners_are_not_shared_across_tools() -> None:
    """Each tool has its OWN function — orchestrator dispatches per-tool.

    Guard against accidental aliasing (e.g., someone sets
    ``TOOL_RUNNERS["mail"] = _run_brief`` during a refactor).
    """
    ids = {tool: id(runner) for tool, runner in orchestrator.TOOL_RUNNERS.items()}
    # All ids should be distinct.
    assert len(set(ids.values())) == len(ids), (
        f"Duplicate runner detected: {ids}"
    )


def test_dispatcher_two_arg_branch_matches_two_arg_tools() -> None:
    """The dispatcher's ``if tool in (...)`` literal in
    ``orchestrator.run_all → start_process`` MUST match
    ``TWO_ARG_TOOLS`` exactly.

    Without this pin, a new two-arg runner can be registered in
    TOOL_RUNNERS + listed in TWO_ARG_TOOLS but accidentally omitted
    from the dispatcher tuple — orchestrator then passes 3 args to a
    2-arg runner and ``Process`` swallows the ``TypeError`` inside the
    child. The runner-signature test passes (the runner IS 2-arg);
    the dispatch crashes on first spawn. Same regression class hit
    digest, radar_day, and friction_analyzer.

    Implementation: parse the literal tuple out of orchestrator.py's
    source. Source-pin rather than runtime introspection because the
    tuple isn't reified at module level — it's an inline expression
    inside ``start_process``. A future refactor could lift it to a
    module constant; until then this regex captures what the runtime
    actually checks.
    """
    import inspect as _inspect

    src = _inspect.getsource(orchestrator.run_all)
    # Find the `if tool in (...):` line. Match content between the
    # parens; tolerate trailing whitespace/newlines inside the tuple.
    match = re.search(
        r'if tool in \((?P<tuple_body>[^)]+)\):',
        src,
    )
    assert match, (
        "Could not locate the ``if tool in (...):`` dispatcher literal "
        "in orchestrator.run_all. If the literal was lifted to a module "
        "constant or restructured, update this test to read the new "
        "source location."
    )
    tuple_body = match.group("tuple_body")
    # Extract every quoted string inside the tuple.
    dispatcher_tools = set(re.findall(r'"([^"]+)"', tuple_body))
    assert dispatcher_tools == TWO_ARG_TOOLS, (
        f"Dispatcher tuple does not match TWO_ARG_TOOLS.\n"
        f"  In dispatcher tuple but not TWO_ARG_TOOLS: "
        f"{dispatcher_tools - TWO_ARG_TOOLS}\n"
        f"  In TWO_ARG_TOOLS but not dispatcher tuple: "
        f"{TWO_ARG_TOOLS - dispatcher_tools}\n"
        f"This is the regression class that bit digest / radar_day / "
        f"friction_analyzer — a runner registered as 2-arg in "
        f"TOOL_RUNNERS but missing from the dispatcher tuple causes "
        f"the orchestrator to pass 3 args on first spawn."
    )


def test_tool_runners_are_top_level_pickleable(orch_dirs) -> None:
    """Every runner must be importable by qualified name for multiprocessing.

    The orchestrator spawns tool processes with ``multiprocessing.Process``,
    which (on spawn/forkserver start methods) pickles the target callable
    by module+qualname. A runner defined as a nested function or a
    ``functools.partial`` would fail to pickle and the child would never
    start. This test guards against that class of regression.
    """
    import pickle

    for tool, runner in orchestrator.TOOL_RUNNERS.items():
        # pickle.dumps of a module-level function writes a GLOBAL opcode
        # referencing module+qualname. If the runner is a closure or local,
        # pickle raises PicklingError.
        data = pickle.dumps(runner)
        revived = pickle.loads(data)
        assert revived is runner, (
            f"{tool} runner did not round-trip through pickle as the same object"
        )
