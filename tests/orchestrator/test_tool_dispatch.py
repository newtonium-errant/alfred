"""Dispatch table tests — ``TOOL_RUNNERS`` contents and signature branching.

The orchestrator has seven registered tool runners. Three of them
(surveyor, mail, brief) take ``(raw, suppress_stdout)``; the other four
(curator, janitor, distiller, talker) take
``(raw, skills_dir, suppress_stdout)``.

``run_all`` picks the arity based on a hard-coded tuple literal
``tool in ("surveyor", "mail", "brief")``. These tests guard that
contract: if someone adds a new tool and forgets to update the arity
check, the dispatcher will pass the wrong number of args and ``Process``
will swallow the ``TypeError`` inside the child.
"""

from __future__ import annotations

import inspect

import alfred.orchestrator as orchestrator


EXPECTED_TOOLS = {
    "curator", "janitor", "distiller",
    "surveyor", "mail", "brief", "talker",
}

TWO_ARG_TOOLS = {"surveyor", "mail", "brief"}
THREE_ARG_TOOLS = {"curator", "janitor", "distiller", "talker"}


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
