"""#82 — the three guards against the many-image session wedge.

THE INCIDENT (VERA, 2026-08-11, session 64c4a0fa). Anthropic applies a stricter
per-image dimension limit — 2000px on either edge — once a request carries more
than 20 image/document blocks. The operator was reconciling claims with ~24
screenshots; once the conversation crossed that threshold, an oversized scan in
the history produced ``invalid_request_error`` ... "image dimensions exceed max
allowed size for many-image requests: 2000 pixels". Because every chat turn
resends the whole transcript, the SAME 400 fired on every subsequent turn — the
session wedged, three retries produced three identical 400s, and the operator
saw only "The assistant hit a snag answering that. Try again in a moment."

Three guards ship, and EITHER of the first two alone prevents the wedge:

  1. downscale at intake  (web/lib/algernon/imageDownscale.ts — pinned in
     web/tests/imageDownscale.test.ts) keeps every image under 2000px;
  2. history trim         (this file) keeps the image COUNT under the
     >20 threshold that makes the dimension limit apply at all;
  3. honest error         (this file) — because a guard can still be defeated
     by an image that arrived before the guards shipped, and a deterministic
     400 must never be reported as "try again".

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import pytest

from alfred.telegram.api_errors import (
    CODE_IMAGE_TOO_LARGE,
    classification_payload,
    classify_engine_error,
    extract_error_text,
)
from alfred.telegram.conversation import (
    MAX_HISTORY_IMAGE_BLOCKS,
    _messages_for_api,
    _trim_history_images,
)

# The live 400's message, verbatim from the incident log.
LIVE_ERROR_TEXT = (
    "messages.11.content.0.image.source.base64: image dimensions exceed max "
    "allowed size for many-image requests: 2000 pixels"
)


def _image_block(tag: str = "x") -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": tag},
    }


def _turn(role: str, *blocks: dict) -> dict:
    return {"role": role, "content": list(blocks)}


# ===========================================================================
# Guard 2 — history trim
# ===========================================================================

def test_untouched_when_under_the_cap() -> None:
    # The overwhelming majority of conversations. Byte-identical passthrough
    # matters: this runs on EVERY turn of every chat, not just image ones.
    messages = [_turn("user", _image_block(), {"type": "text", "text": "hi"})]
    out, replaced = _trim_history_images(messages, max_images=12)
    assert replaced == 0
    assert out is messages


def test_trims_down_to_the_cap_keeping_the_newest() -> None:
    messages = [_turn("user", _image_block(f"i{n}")) for n in range(20)]
    out, replaced = _trim_history_images(messages, max_images=12)

    assert replaced == 8
    kept = [b for m in out for b in m["content"] if b.get("type") == "image"]
    assert len(kept) == 12
    # The NEWEST twelve survive — in a working session the operator is asking
    # about what they just attached.
    assert [b["source"]["data"] for b in kept] == [f"i{n}" for n in range(8, 20)]


def test_replaced_images_become_numbered_placeholders() -> None:
    # Not a bare deletion: the model must still see that an image was there,
    # or its own earlier reply about it reads as a non-sequitur.
    messages = [_turn("user", _image_block(f"i{n}")) for n in range(14)]
    out, _ = _trim_history_images(messages, max_images=12)

    first = out[0]["content"][0]
    assert first["type"] == "text"
    # Numbered CHRONOLOGICALLY (image 1 is the oldest), so the number matches
    # the order the operator sent them.
    assert first["text"] == (
        "[image 1 of 14 — sent earlier in this conversation, no longer attached]"
    )
    assert out[1]["content"][0]["text"].startswith("[image 2 of 14")


def test_trim_does_not_mutate_the_persisted_transcript() -> None:
    """The load-bearing isolation property.

    ``session.transcript`` is the durable record and is re-read every turn. If
    the trim mutated it, the first trimmed turn would DESTROY the images
    permanently — a data-loss bug far worse than the wedge it fixes.
    """
    messages = [_turn("user", _image_block(f"i{n}")) for n in range(14)]
    snapshot = [[dict(b) for b in m["content"]] for m in messages]

    _trim_history_images(messages, max_images=12)

    assert [[dict(b) for b in m["content"]] for m in messages] == snapshot
    assert all(m["content"][0]["type"] == "image" for m in messages)


def test_trims_within_a_single_multi_image_turn() -> None:
    # Images accumulate inside one turn too (the composer allows several), so
    # the walk has to be per-block, not per-message.
    messages = [_turn("user", *[_image_block(f"i{n}") for n in range(6)])]
    out, replaced = _trim_history_images(messages, max_images=2)
    assert replaced == 4
    types = [b["type"] for b in out[0]["content"]]
    assert types == ["text", "text", "text", "text", "image", "image"]


def test_non_list_content_is_passed_through() -> None:
    # Text-only turns store `content` as a bare string (build_user_content's
    # single-modal shape). Touching those would corrupt every ordinary chat.
    messages = [{"role": "user", "content": "just text"}, _turn("user", _image_block())]
    out, replaced = _trim_history_images(messages, max_images=0)
    assert replaced == 1
    assert out[0]["content"] == "just text"


def test_zero_cap_replaces_every_image() -> None:
    messages = [_turn("user", _image_block(), _image_block())]
    out, replaced = _trim_history_images(messages, max_images=0)
    assert replaced == 2
    assert all(b["type"] == "text" for b in out[0]["content"])


def test_negative_cap_is_a_no_op_not_a_wipe() -> None:
    # A misconfigured negative must not silently delete all image context.
    messages = [_turn("user", _image_block())]
    out, replaced = _trim_history_images(messages, max_images=-1)
    assert replaced == 0
    assert out is messages


# --- the cap itself --------------------------------------------------------

def test_default_cap_sits_below_the_api_threshold() -> None:
    """The default is not arbitrary — it must be under Anthropic's own limit.

    The stricter per-image dimension rule applies above 20 image/document
    blocks. The cap has to leave room beneath that (document blocks share the
    count on some platforms), which is the entire reason the guard works.
    """
    assert MAX_HISTORY_IMAGE_BLOCKS < 20


def test_trim_is_wired_into_turn_assembly_by_default() -> None:
    """The trap this pin exists to close.

    ``_messages_for_api`` is the ONE seam both engines assemble through. If the
    cap were an opt-in parameter that production forgot to thread, every pin
    above would stay green while the wedge shipped unfixed. So the parameter
    defaults to ENABLED and this asserts the default path — no caller has to
    remember anything.
    """
    transcript = [_turn("user", _image_block(f"i{n}")) for n in range(30)]
    out = _messages_for_api(transcript)
    kept = [b for m in out for b in m["content"] if b.get("type") == "image"]
    assert len(kept) == MAX_HISTORY_IMAGE_BLOCKS


def test_turn_assembly_emits_the_trim_signal() -> None:
    """Dropping content from a request must not be silent.

    Per ``feedback_intentionally_left_blank.md`` — "why did it forget the first
    screenshot?" needs an answer in the log.
    """
    import structlog

    transcript = [_turn("user", _image_block(f"i{n}")) for n in range(30)]
    with structlog.testing.capture_logs() as captured:
        _messages_for_api(transcript)

    matches = [c for c in captured
               if c.get("event") == "conversation.history_images_trimmed"]
    assert len(matches) == 1
    assert matches[0]["replaced"] == 30 - MAX_HISTORY_IMAGE_BLOCKS
    assert matches[0]["kept"] == MAX_HISTORY_IMAGE_BLOCKS
    assert matches[0]["reason"] == "many_image_request_guard"


def test_no_trim_signal_on_an_ordinary_turn() -> None:
    # The other half of ILB: the event means something only if it stays absent
    # when nothing was dropped.
    import structlog

    with structlog.testing.capture_logs() as captured:
        _messages_for_api([{"role": "user", "content": "hello"}])
    assert not [c for c in captured
                if c.get("event") == "conversation.history_images_trimmed"]


# ===========================================================================
# Guard 3 — honest error classification
# ===========================================================================

class _FakeAPIError(Exception):
    """Shaped like an Anthropic SDK error: message in .body and in str()."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.body = {"error": {"type": "invalid_request_error", "message": message}}


def test_classifies_the_live_incident_error() -> None:
    result = classify_engine_error(_FakeAPIError(LIVE_ERROR_TEXT))
    assert result is not None
    assert result.code == CODE_IMAGE_TOO_LARGE
    assert result.retryable is False


def test_the_message_does_not_tell_the_operator_to_retry() -> None:
    """The specific harm being fixed.

    "Try again in a moment" is not merely unhelpful for a deterministic 400 —
    it is an instruction to repeat an action guaranteed to fail. The copy has
    to say what actually clears it instead.
    """
    result = classify_engine_error(_FakeAPIError(LIVE_ERROR_TEXT))
    assert result is not None
    lowered = result.message.lower()
    assert "try again" not in lowered
    assert "retrying won't clear it" in lowered
    # And it names the action that does work.
    assert "new chat" in lowered


def test_classification_survives_a_changed_pixel_count() -> None:
    # The wording carries a number Anthropic can change. Matching the full
    # sentence would silently stop classifying the day it moves — degrading to
    # the generic copy, i.e. straight back to the bug.
    moved = LIVE_ERROR_TEXT.replace("2000 pixels", "1500 pixels")
    assert classify_engine_error(_FakeAPIError(moved)) is not None


def test_reads_the_message_from_str_when_body_is_absent() -> None:
    # Not every SDK exception carries .body.
    assert classify_engine_error(Exception(LIVE_ERROR_TEXT)) is not None


@pytest.mark.parametrize("text", [
    "rate_limit_error: too many requests",
    "overloaded_error",
    "messages.0.content: image dimensions exceed 8000 pixels",  # different rule
    "",
])
def test_abstains_on_everything_else(text: str) -> None:
    """A classifier that guesses is worse than one that abstains.

    Returning None leaves the caller's existing generic handling in place. Note
    the third case: a plain oversized-image error is NOT the many-image wedge
    and must not borrow its "start a new chat" advice, which would be wrong.
    """
    assert classify_engine_error(_FakeAPIError(text)) is None


def test_extract_error_text_prefers_the_structured_body() -> None:
    assert extract_error_text(_FakeAPIError("inner")) == "inner"


def test_extract_error_text_falls_back_to_a_typed_string() -> None:
    # Never empty — an operator grepping the log needs the exception class.
    out = extract_error_text(ValueError("boom"))
    assert "ValueError" in out and "boom" in out


def test_payload_carries_the_message_as_detail() -> None:
    """The front end renders `detail`, so the box owns the copy.

    Both `useChat.ts` and `usePlayerAsk.ts` switch on the CODE and read
    `detail` for this case — one source of truth for the wording rather than
    three drifting literals.
    """
    result = classify_engine_error(_FakeAPIError(LIVE_ERROR_TEXT))
    assert result is not None
    payload = classification_payload(result)
    assert payload["error"] == CODE_IMAGE_TOO_LARGE
    assert payload["detail"] == result.message
    assert payload["retryable"] is False
