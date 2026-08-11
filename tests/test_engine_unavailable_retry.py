"""#94(b) — an upstream 5xx must be offered as retryable, not declared final.

THE INCIDENT, 2026-08-11. Two Anthropic 500s at 03:32:49 and 03:33:11Z
(req_011CdvFpoMNP77eBm93fwrLk, req_011CdvFtEjvkX7M3Wx3Pg9LD) killed a session's
turns. The classifier abstained (it only knew the many-image 400), so the
backend sent a bare ``engine_error``, and the client's local recoverable-code
list did not contain it — the pending turn was CLEARED and no retry affordance
appeared. The operator recovered by typing "Try now" by hand.

The two failure classes are opposites in the only way that matters, and this
module already had the vocabulary for it:

  image_too_large     deterministic — retrying repeats a guaranteed failure,
                      the fix is a new chat
  engine_unavailable  transient — nothing is wrong with the request, and
                      resending IS the fix

ORDERING IS LOAD-BEARING. A request can be both malformed and unlucky. The
deterministic check runs first so a recognised permanent failure is never
masked by a transient one — telling the operator to retry something that cannot
succeed is the precise harm this module was written to stop.
"""

from __future__ import annotations

import pytest

from alfred.telegram.api_errors import (
    CODE_ENGINE_UNAVAILABLE,
    CODE_IMAGE_TOO_LARGE,
    classification_payload,
    classify_engine_error,
)

# The live 400 text from #82, kept verbatim so the ordering pin below is driven
# by the real thing rather than a paraphrase.
LIVE_400_TEXT = (
    "messages.0.content.1.image: image dimensions exceed max allowed size for "
    "many-image requests: 2000 pixels"
)


class _FakeStatusError(Exception):
    """Shaped like an Anthropic ``APIStatusError``: carries ``status_code``."""

    def __init__(self, status: int, message: str = "upstream boom") -> None:
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"type": "api_error", "message": message}}


class _FakeConnectionError(Exception):
    """An SDK transport failure — no status at all."""

    def __init__(self) -> None:
        super().__init__("connection reset by peer")


_FakeConnectionError.__name__ = "APIConnectionError"


# ---------------------------------------------------------------------------
# The transient class
# ---------------------------------------------------------------------------


def test_the_live_incident_500_is_now_classified() -> None:
    """THE incident pin — the exact failure that cleared the operator's turn."""
    result = classify_engine_error(_FakeStatusError(500))
    assert result is not None, "an upstream 500 is still unclassified"
    assert result.code == CODE_ENGINE_UNAVAILABLE
    assert result.retryable is True


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
def test_every_transient_upstream_status_is_retryable(status: int) -> None:
    """429 included deliberately: rate-limiting is transient by construction."""
    result = classify_engine_error(_FakeStatusError(status))
    assert result is not None, status
    assert result.code == CODE_ENGINE_UNAVAILABLE
    assert result.retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_a_4xx_is_not_treated_as_transient(status: int) -> None:
    """THE negative pin. A client-error resend fails identically forever.

    Widening this to "any error is retryable" would recreate the #82 harm from
    the other direction — an endless retry button on a request that cannot
    succeed.
    """
    assert classify_engine_error(_FakeStatusError(status)) is None


def test_a_statusless_connection_failure_is_still_transient() -> None:
    """A reset or read-timeout mid-request carries no status.

    Refusing to classify it would leave the operator with a cleared turn and no
    retry button for the most obviously retryable failure there is.
    """
    result = classify_engine_error(_FakeConnectionError())
    assert result is not None
    assert result.code == CODE_ENGINE_UNAVAILABLE
    assert result.retryable is True


def test_an_unrecognised_failure_still_abstains() -> None:
    """The module's stated contract: abstain rather than guess.

    A confidently wrong explanation sends the operator down the wrong path, so
    anything unfamiliar keeps the caller's generic handling.
    """
    assert classify_engine_error(ValueError("something else entirely")) is None


# ---------------------------------------------------------------------------
# Ordering — deterministic beats transient
# ---------------------------------------------------------------------------


def test_a_deterministic_400_is_not_masked_by_the_transient_branch() -> None:
    """The many-image 400 must still classify as PERMANENT.

    Its status (400) is not in the transient set, so this holds today by two
    independent facts. Pinned anyway: if the transient set ever widened to
    include 400, the deterministic verdict must still win, and this is the
    test that would say so rather than the operator discovering it as a retry
    button that never works.
    """
    exc = _FakeStatusError(400, LIVE_400_TEXT)
    result = classify_engine_error(exc)
    assert result is not None
    assert result.code == CODE_IMAGE_TOO_LARGE
    assert result.retryable is False


def test_a_500_carrying_the_image_text_still_reads_as_deterministic() -> None:
    """The adversarial ordering case, and the reason the check order matters.

    A 500 whose body carries the many-image message is both transient BY STATUS
    and deterministic BY CAUSE. Retrying cannot clear the image, so the
    deterministic verdict has to win — which it does only because the text
    check runs first.
    """
    result = classify_engine_error(_FakeStatusError(500, LIVE_400_TEXT))
    assert result is not None
    assert result.code == CODE_IMAGE_TOO_LARGE
    assert result.retryable is False


# ---------------------------------------------------------------------------
# The copy, and the wire
# ---------------------------------------------------------------------------


def test_the_message_invites_a_resend_and_does_not_send_them_to_a_new_chat() -> None:
    """Opposite remedy to image_too_large, and it must say so.

    "Start a new chat" is exactly wrong here: the conversation is fine and
    abandoning it would lose the thread over a blip.
    """
    result = classify_engine_error(_FakeStatusError(500))
    assert result is not None
    lowered = result.message.lower()
    assert "send it again" in lowered
    assert "new chat" not in lowered
    # And it says the request was not at fault, so the operator does not go
    # hunting for something to change about their message.
    assert "nothing is wrong with your message" in lowered


def test_the_message_names_the_upstream_status_when_there_is_one() -> None:
    """Diagnosable without a log dive; omitted when there is nothing to name."""
    assert "upstream 503" in classify_engine_error(_FakeStatusError(503)).message
    assert "upstream" not in classify_engine_error(_FakeConnectionError()).message


def test_the_wire_payload_carries_the_retryable_verdict() -> None:
    """The field the CLIENT reads (#94).

    Both chat paths ship ``classification_payload``; if ``retryable`` were
    missing from it the client would fall back to its local code list, which
    does not contain this code — reproducing the incident with the classifier
    apparently fixed.
    """
    payload = classification_payload(classify_engine_error(_FakeStatusError(500)))
    assert payload["error"] == CODE_ENGINE_UNAVAILABLE
    assert payload["retryable"] is True
    assert payload["detail"]


def test_the_deterministic_payload_still_says_not_retryable() -> None:
    payload = classification_payload(
        classify_engine_error(_FakeStatusError(400, LIVE_400_TEXT))
    )
    assert payload["error"] == CODE_IMAGE_TOO_LARGE
    assert payload["retryable"] is False
