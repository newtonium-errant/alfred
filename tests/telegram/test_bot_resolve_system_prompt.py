"""Regression pin for the ``_resolve_system_prompt`` shape-normalisation
helper at ``bot.update_handler``'s read site.

Background (#49 → 32 telegram failures, fixed 2026-05-09 in Batch A):

  ``bot_data[_KEY_SYSTEM]`` may carry EITHER a zero-arg provider
  callable (production path; ``build_app`` wraps daemon-supplied
  hot-reload providers and ``str`` literals into callables) OR a
  plain string (test fixtures + legacy callers that bypass
  ``build_app`` and write ``bot_data`` directly). Both shapes are
  valid inputs to ``build_app`` per its docstring.

  Pre-2026-05-09, the read site at ``update_handler`` assumed the
  ``build_app`` wrap was always in place and called
  ``ctx.application.bot_data[_KEY_SYSTEM]()`` unconditionally. Tests
  that wrote a string directly into ``bot_data`` exploded with
  ``TypeError: 'str' object is not callable`` on 32 different test
  cases across 10+ test files (test_inline_commands,
  test_instance_templating, test_model_switch, test_reply_context,
  test_silent_capture, test_calibration_writes, test_capture_extract,
  test_fiction_command, test_idle_tick, test_implicit_escalation, ...).

  The fix introduces ``_resolve_system_prompt`` at the read site to
  mirror ``build_app``'s input contract. Tests bypassing ``build_app``
  no longer need fixture sweeps.

Per ``feedback_regression_pin_unconditional.md``: this file is
isolated regression-pin home. NO ``pytest.importorskip`` at module
level — these tests run on every pytest invocation regardless of
optional dependency availability. Imports are stdlib + ``alfred``.
"""
from __future__ import annotations

import pytest

from alfred.telegram.bot import _resolve_system_prompt


# ---------------------------------------------------------------------------
# Two-shape contract — string OR callable
# ---------------------------------------------------------------------------


def test_string_returned_as_is() -> None:
    """The legacy + test-fixture path: ``bot_data[_KEY_SYSTEM]`` holds a
    plain string. Helper returns the string unchanged.

    This is the bug shape from #49 — pre-fix the read site called
    ``"sys"()`` which raises ``TypeError``. Post-fix the helper takes
    the string at face value.
    """
    assert _resolve_system_prompt("sys") == "sys"
    assert _resolve_system_prompt("") == ""
    assert _resolve_system_prompt("multi-line\nprompt\ncontent") == (
        "multi-line\nprompt\ncontent"
    )


def test_callable_invoked_for_string() -> None:
    """The production path: ``bot_data[_KEY_SYSTEM]`` holds a zero-arg
    provider callable. Helper invokes it and returns the result.

    The hot-reload contract: a daemon-supplied provider re-reads
    SKILL.md on each call so on-disk edits take effect on the next
    turn without a restart. The helper preserves that semantic by
    invoking fresh per call (no caching).
    """
    call_count = {"n": 0}

    def my_provider() -> str:
        call_count["n"] += 1
        return f"prompt-content-call-{call_count['n']}"

    assert _resolve_system_prompt(my_provider) == "prompt-content-call-1"
    assert _resolve_system_prompt(my_provider) == "prompt-content-call-2"
    assert call_count["n"] == 2


def test_callable_returning_empty_string_returns_empty() -> None:
    """A provider that legitimately returns ``""`` is a valid case
    (e.g. a SKILL.md file that's been emptied on disk). Helper must
    not coerce empty results to anything else.
    """
    assert _resolve_system_prompt(lambda: "") == ""


def test_lambda_provider_works_same_as_function() -> None:
    """Lambdas are common in test fixtures that DO wrap their static
    prompt explicitly. The helper handles them identically to a named
    function — both are callables.
    """
    assert _resolve_system_prompt(lambda: "from-lambda") == "from-lambda"


# ---------------------------------------------------------------------------
# Defensive coverage — non-callable, non-string inputs
# ---------------------------------------------------------------------------


def test_non_string_non_callable_coerced_to_string() -> None:
    """If a future caller stashes an unexpected shape (e.g. a config
    object that isn't callable and isn't a string), the helper coerces
    it via ``str()`` rather than raising. Mirrors the
    ``str(system_prompt_provider)`` fallback already in ``build_app``
    — fail loud-but-safe rather than crash inside the message handler.

    None coerces to ``"None"`` — operationally weird, but operators
    will see the literal "None" in the prompt and immediately know to
    fix the wiring rather than chase a crash trace.
    """
    assert _resolve_system_prompt(None) == "None"
    assert _resolve_system_prompt(42) == "42"


# ---------------------------------------------------------------------------
# Build-app integration — the production contract still holds
# ---------------------------------------------------------------------------


def test_build_app_callable_normalisation_still_works() -> None:
    """Belt-and-braces: even though the read site now accepts both
    shapes, ``build_app`` STILL wraps strings into constant-returning
    callables. That contract is pinned by
    ``test_skill_md_hot_reload.TestBuildAppProviderWiring``; this test
    just confirms the helper produces the same string for either side
    of that contract.

    For a provider wrapping the same static string, ``_resolve``
    must produce identical output whether called on the raw string
    OR on the wrapped lambda — i.e. the read site is invariant
    under the wrap.
    """
    text = "static-prompt-text"
    wrapped = lambda: text  # noqa: E731 — test fixture
    assert _resolve_system_prompt(text) == _resolve_system_prompt(wrapped)
