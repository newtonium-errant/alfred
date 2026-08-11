"""Every registered reply-dispatch item kind is threaded at EVERY touchpoint.

THE BUG CLASS. ``reply_dispatch`` is 3,500 lines and adding an item kind means
threading one name through a dozen places: a reader, the item-number union, the
type-flags dict, the verb-applicability check, the hint signature and its
any-check, a per-kind map and counter in the main loop, the increment, two
totals, the hint call, the summary log and the result dict. Miss ONE and there
is no error anywhere — the card renders, the operator replies ``N confirm``, and
nothing happens. He sees "I confirmed it and it's still there"; every per-layer
unit test stays green, because each layer really does work in isolation.

That is accepted-then-ignored, and it is invisible by construction: the failure
is an ABSENCE, and absences do not raise. This file makes the absence loud. It
reads the module's source and asserts, per kind, that each touchpoint family is
present — so kind #7 cannot silently skip one the way kind #6 nearly did.

WHY SOURCE INSPECTION RATHER THAN BEHAVIOUR. A behavioural test needs a fixture
per kind per touchpoint, and the touchpoints that fail SILENTLY are exactly the
ones with no observable behaviour to assert on (a kind missing from
``_batch_item_numbers`` only changes a smart-routing nudge three call layers
away). The function bodies are located by AST so an assertion is scoped to the
right function, not matched anywhere in 3,500 lines.

Adding a kind? Add it to :data:`KINDS` and make this pass. If a touchpoint
genuinely does not apply, exempt it HERE with the reason — an exemption someone
has to write down is the point.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import alfred.daily_sync.reply_dispatch as rd

#: Every item kind whose names follow the regular scheme. ``email`` is
#: deliberately absent — see :func:`test_email_is_the_named_exemption`.
KINDS = (
    "attribution",
    "proposal",
    "demotion",
    "pending",
    "routine_match",
    "capture_close",
)

_SOURCE = Path(inspect.getfile(rd)).read_text(encoding="utf-8")
_TREE = ast.parse(_SOURCE)


def _function_source(name: str) -> str:
    """The source of one top-level function, so assertions cannot match text
    from somewhere else in a 3,500-line module."""
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SOURCE, node) or ""
    raise AssertionError(f"reply_dispatch has no top-level function {name!r}")


def _defined_functions() -> set[str]:
    return {
        n.name for n in _TREE.body if isinstance(n, ast.FunctionDef)
    }


# ---------------------------------------------------------------------------
# Module-level touchpoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_kind_has_a_last_batch_reader(kind: str) -> None:
    """Without a reader the kind's items are simply invisible to the reply."""
    assert f"_last_batch_{kind}_items" in _defined_functions()


@pytest.mark.parametrize("kind", KINDS)
def test_reader_is_in_the_item_number_union(kind: str) -> None:
    """``_batch_item_numbers`` backs the smart-route mistyped-calibration
    detector. A kind missing here makes a real item number look incidental, so
    the operator's typo goes un-nudged."""
    assert f"_last_batch_{kind}_items" in _function_source("_batch_item_numbers")


@pytest.mark.parametrize("kind", KINDS)
def test_kind_has_a_type_flag(kind: str) -> None:
    assert f'"has_{kind}"' in _function_source("_batch_type_flags")


@pytest.mark.parametrize("kind", KINDS)
def test_kind_is_in_the_applicable_verbs_check(kind: str) -> None:
    """The G1 near-miss detector only nudges on a typo of a verb that applies to
    THIS batch. A kind missing here means a typo of ``confirm`` on its card gets
    no help."""
    assert f'has_{kind}' in _function_source("_applicable_calibration_verbs")


@pytest.mark.parametrize("kind", KINDS)
def test_kind_is_a_calibration_hint_parameter(kind: str) -> None:
    assert f"has_{kind}" in _function_source("_compose_calibration_hint")


# ---------------------------------------------------------------------------
# The main loop — where a miss is silent and total
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_main_loop_reads_and_maps_the_kind(kind: str) -> None:
    body = _function_source("handle_daily_sync_reply")
    assert f"_last_batch_{kind}_items(config)" in body
    assert f"{kind}_by_num" in body


@pytest.mark.parametrize("kind", KINDS)
def test_main_loop_counts_the_kind(kind: str) -> None:
    """THE ONE THAT BITES. A counter that is initialised but never incremented
    reports zero forever: the resolver ran, the write landed, and the operator
    is told nothing was applied."""
    body = _function_source("handle_daily_sync_reply")
    assert f"{kind}_written = 0" in body
    assert f"{kind}_written += 1" in body


@pytest.mark.parametrize("kind", KINDS)
def test_kind_reaches_both_totals(kind: str) -> None:
    """``written_count`` gates ``mark_batch_replied``; ``corrections_count`` is
    what the operator is told. A kind missing from either is applied-but-
    invisible."""
    body = _function_source("handle_daily_sync_reply")
    written_block = body.split("written_count = (", 1)[1].split(")", 1)[0]
    corrections_block = body.split("corrections_count = (", 1)[1].split(")", 1)[0]
    assert f"{kind}_written" in written_block
    assert f"{kind}_written" in corrections_block


@pytest.mark.parametrize("kind", KINDS)
def test_kind_flows_into_the_calibration_hint_call(kind: str) -> None:
    body = _function_source("handle_daily_sync_reply")
    assert f"has_{kind}=bool({kind}_items)" in body


@pytest.mark.parametrize("kind", KINDS)
def test_kind_is_in_the_summary_log(kind: str) -> None:
    """Observability: a kind absent from ``daily_sync.reply_processed`` cannot
    be diagnosed from the log when the operator says nothing happened."""
    body = _function_source("handle_daily_sync_reply")
    assert f"{kind}_written={kind}_written" in body


@pytest.mark.parametrize("kind", KINDS)
def test_kind_is_in_the_result_dict(kind: str) -> None:
    body = _function_source("handle_daily_sync_reply")
    assert f'"{kind}_count"' in body


# ---------------------------------------------------------------------------
# The exemption, written down
# ---------------------------------------------------------------------------

def test_email_is_the_named_exemption() -> None:
    """``email`` predates the regular scheme and breaks it in two places: its
    reader is ``_last_batch_items`` (no kind segment, it was the only kind), and
    ``corrections_count`` uses ``email_items_corrected`` rather than
    ``email_written`` because one email item can fan out to several corpus rows
    across a cluster. Both are deliberate; pinned here so the irregularity stays
    a known exemption rather than looking like a miss."""
    assert "_last_batch_items" in _defined_functions()
    assert "_last_batch_email_items" not in _defined_functions()
    body = _function_source("handle_daily_sync_reply")
    assert "email_items_corrected" in body
    assert 'has_email=bool(email_items)' in body
    assert '"email_count"' in body


def test_every_kind_in_the_list_is_reachable_from_the_flags() -> None:
    """``KINDS`` must not drift from what ``_batch_type_flags`` actually
    returns — a kind dropped from this file's list would silently stop being
    checked, which is the same absence one level up."""
    flags_src = _function_source("_batch_type_flags")
    declared = {
        line.split('"')[1].removeprefix("has_")
        for line in flags_src.splitlines()
        if '"has_' in line
    }
    assert declared == set(KINDS) | {"email"}
