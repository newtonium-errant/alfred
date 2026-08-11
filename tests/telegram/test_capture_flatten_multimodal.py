"""#96 part 1 — captioned photo turns must survive transcript flattening.

THE DEFECT, and why it was invisible. ``_flatten_transcript`` did
``elif isinstance(content, list): continue`` under a comment saying the lists
were tool_results. That was true when written and false by the time it
mattered: ``vision.build_user_content`` returns a LIST whenever a turn carries
images, so a captioned photo turn took the tool_result branch and vanished
WHOLE — caption included.

THE COST was the #64 motivating case. The operator said he would attach
workout-plan screenshots and then attached four of six IN THE SAME SESSION.
Every one of those turns was a list, so the structurer saw neither the captions
nor any sign that anything had arrived, and emitted the promise as a fresh open
task while the evidence sat in the same transcript it was reading.

The pins below therefore assert on BOTH halves of what a marker means: the
caption text survives (real content), and an attachment is COUNTABLE (arrival,
not content). Nothing here claims the structurer can see the images — it cannot,
and the prompt layer says so explicitly.
"""

from __future__ import annotations

from typing import Any

import pytest

from alfred.telegram.capture_batch import IMAGE_MARKER, _flatten_transcript
from alfred.telegram.vision import build_user_content


def _img(n: int = 1) -> list[dict[str, Any]]:
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": f"fake{i}"}}
        for i in range(n)
    ]


def _turn(content: Any, ts: str = "2026-08-05T14:23:00Z") -> dict[str, Any]:
    return {"role": "user", "content": content, "_ts": ts}


# ---------------------------------------------------------------------------
# The motivating shape
# ---------------------------------------------------------------------------

def test_the_64_incident_shape_survives() -> None:
    """A captioned screenshot turn, built by the REAL composer.

    Driven through ``build_user_content`` rather than a hand-built list: the
    defect was that production's shape and the flattener's assumption had
    drifted apart, so a hand-built fixture could agree with the flattener while
    production disagreed with both.
    """
    caption = "here are the workout plans"
    content = build_user_content(caption, _img(1))
    assert isinstance(content, list), "the composer no longer returns a list"

    flat = _flatten_transcript([_turn(content)])

    assert caption in flat, "the caption vanished — the #64 defect"
    assert IMAGE_MARKER in flat, "no sign an attachment arrived"


def test_four_of_six_screenshots_are_countable() -> None:
    """The operator sent six and Salem noticed four. The structurer must be
    able to COUNT what arrived — one marker per image block is what makes the
    discrepancy expressible at all."""
    flat = _flatten_transcript([
        _turn(build_user_content("first batch", _img(4))),
    ])

    assert flat.count(IMAGE_MARKER) == 4


def test_markers_accumulate_across_turns() -> None:
    flat = _flatten_transcript([
        _turn(build_user_content("first", _img(2))),
        _turn(build_user_content("second", _img(2))),
    ])

    assert flat.count(IMAGE_MARKER) == 4
    assert "first" in flat and "second" in flat


# ---------------------------------------------------------------------------
# The original intent is preserved, not overturned
# ---------------------------------------------------------------------------

def test_a_pure_tool_result_turn_is_still_skipped() -> None:
    """The branch existed for a reason; the fix must not resurrect tool noise."""
    flat = _flatten_transcript([
        _turn([{"type": "tool_result", "tool_use_id": "t1", "content": "42 rows"}]),
    ])

    assert flat == ""


def test_tool_results_are_dropped_but_the_caption_beside_them_is_not() -> None:
    flat = _flatten_transcript([
        _turn([
            {"type": "tool_result", "tool_use_id": "t1", "content": "noise"},
            {"type": "text", "text": "the part he actually said"},
        ]),
    ])

    assert flat.strip().endswith("the part he actually said")
    assert "noise" not in flat


def test_an_unknown_block_type_contributes_nothing() -> None:
    """Default-deny on block types: a future block kind must not leak raw
    payload into the structurer's prompt."""
    flat = _flatten_transcript([
        _turn([
            {"type": "some_future_kind", "payload": "SHOULD-NOT-APPEAR"},
            {"type": "text", "text": "kept"},
        ]),
    ])

    assert "SHOULD-NOT-APPEAR" not in flat
    assert "kept" in flat


def test_a_malformed_block_does_not_crash_the_flattener() -> None:
    flat = _flatten_transcript([
        _turn(["not a dict", None, {"type": "text", "text": "still here"}]),
    ])

    assert "still here" in flat


# ---------------------------------------------------------------------------
# Unchanged behaviour (regression surface)
# ---------------------------------------------------------------------------

def test_plain_string_turns_are_untouched() -> None:
    flat = _flatten_transcript([_turn("just talking")])

    assert flat == "[14:23] just talking"


def test_a_string_turn_keeps_its_timestamp_prefix_with_images_present() -> None:
    flat = _flatten_transcript([_turn(build_user_content("cap", _img(1)))])

    assert flat.startswith("[14:23] ")


def test_assistant_turns_are_still_skipped() -> None:
    flat = _flatten_transcript([
        {"role": "assistant", "content": "I should not be here", "_ts": ""},
        _turn("he spoke"),
    ])

    assert "I should not be here" not in flat
    assert "he spoke" in flat


def test_an_imageless_turn_gets_no_marker() -> None:
    """The marker asserts arrival; inventing one for a text-only turn would
    make the count lie in the other direction."""
    content = build_user_content("no pictures here", None)
    assert isinstance(content, str), "imageless turns must stay bare strings"

    flat = _flatten_transcript([_turn(content)])

    assert IMAGE_MARKER not in flat
    assert "no pictures here" in flat


def test_an_empty_transcript_is_empty_not_an_error() -> None:
    assert _flatten_transcript([]) == ""


# ---------------------------------------------------------------------------
# The prompt-layer contract
# ---------------------------------------------------------------------------

def test_the_marker_is_a_stable_exported_constant() -> None:
    """The (a) prompt line quotes this literal verbatim, so it is a contract
    across the code/prompt boundary — not an implementation detail. A rename
    that updates only one side leaves the model reading a token it was never
    told about."""
    assert IMAGE_MARKER == "[image attached]"
