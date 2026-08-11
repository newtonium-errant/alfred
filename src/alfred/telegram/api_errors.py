"""Classification of Anthropic API errors into actionable operator messages.

The chat paths historically caught every engine failure with one blanket
``except Exception`` and rendered one blanket string — "The assistant hit a
snag answering that. Try again in a moment." For a transient overload that copy
is correct. For a **deterministic 400 it is actively harmful**: retrying cannot
succeed, so the operator is invited to repeat an action that is guaranteed to
fail, with no hint of what would actually clear it.

The 2026-08-11 VERA incident is the worked example. Anthropic applies a
stricter per-image dimension limit — 2000px on either edge — once a request
carries more than 20 image blocks (document blocks share that count on some
platforms, though not on this one); over it, the request is rejected
with an ``invalid_request_error`` naming "many-image requests". Every chat turn
resends the whole transcript, so once an oversized screenshot is in history the
same 400 fires on every subsequent turn: the SESSION WEDGES. The operator
retried three times, got three identical 400s, and saw "try again in a moment"
each time.

This module is deliberately narrow. It classifies conditions we understand and
can give real advice about, and returns ``None`` for everything else so the
caller keeps its existing generic handling — a classifier that guesses is worse
than one that abstains, because a confidently wrong explanation sends the
operator down the wrong path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The machine-readable code carried to the front end. A new code needs a
# matching case in BOTH web/lib/algernon/useChat.ts and
# web/components/player/usePlayerAsk.ts — they switch on the code, and an
# unknown one silently renders the generic fallback.
CODE_IMAGE_TOO_LARGE = "image_too_large"

#: An UPSTREAM failure at Anthropic — a 5xx / ``api_error`` / overloaded. The
#: opposite of ``image_too_large`` in the only way that matters to the operator:
#: retrying is exactly the right move, because nothing about the request is
#: wrong. Two Anthropic 500s at 03:32:49 and 03:33:11Z on 2026-08-11
#: (req_011CdvFpoMNP77eBm93fwrLk, req_011CdvFtEjvkX7M3Wx3Pg9LD) killed a
#: session's turns and the client cleared the pending turn with no retry
#: affordance — the operator recovered by typing "Try now" by hand.
CODE_ENGINE_UNAVAILABLE = "engine_unavailable"

#: Upstream statuses that mean "the request was fine, the service was not".
#: 429 is included deliberately: rate-limiting is transient by construction and
#: a resend after a moment is the correct response to it.
_TRANSIENT_UPSTREAM_STATUSES = frozenset({429, 500, 502, 503, 504, 529})


@dataclass(frozen=True)
class EngineErrorClassification:
    """A recognised engine failure, with copy the operator can act on."""

    code: str
    #: User-facing text. States what happened and what actually clears it —
    #: never "try again" for a deterministic failure.
    message: str
    #: False when retrying the same request cannot possibly succeed.
    retryable: bool


def extract_error_text(exc: BaseException) -> str:
    """Best-effort error-text extraction from an Anthropic exception.

    Anthropic's ``BadRequestError`` carries the structured message at
    ``exc.body["error"]["message"]`` and also in ``str(exc)``; other SDK
    exceptions vary. Mirrors ``health.tool_schema_validator._extract_error_text``
    (the existing precedent) — prefer the nested form, fall back to the
    stringification so this never returns empty.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg:
                return msg
    return f"{exc.__class__.__name__}: {exc}"


def _is_image_dimension_error(text: str) -> bool:
    """True for the many-image dimension rejection.

    Matched on two independent substrings rather than the full sentence: the
    exact wording carries a pixel count that Anthropic can change ("2000
    pixels" today), and a brittle full-string match would silently stop
    classifying the day it moves — degrading to the generic message, which is
    precisely the failure this module exists to fix.
    """
    lowered = text.lower()
    if "image dimensions" not in lowered:
        return False
    return "many-image" in lowered or "many image" in lowered


def _upstream_status(exc: BaseException) -> int | None:
    """The HTTP status Anthropic returned, when the exception carries one.

    Read off the attribute rather than the class name: the SDK's exception
    hierarchy (``APIStatusError`` and its subclasses) is free to change, and
    every one of them exposes ``status_code``. Matching on class names would be
    a second thing to keep in sync with a library we do not control.
    """
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _is_transient_upstream(exc: BaseException) -> bool:
    """True for an upstream failure that a resend can plausibly clear.

    Status FIRST, text second. A 500 is a 500 whatever its prose says, and the
    message wording is the part Anthropic can change without notice — the same
    reasoning that keeps ``_is_image_dimension_error`` off a full-string match.

    The text fallback exists for SDK errors that carry no status: a connection
    reset or a read timeout mid-request is transient in exactly the same way,
    and refusing to classify it would leave the operator with a cleared turn
    and no retry button for the most obviously retryable failure there is.
    """
    status = _upstream_status(exc)
    if status is not None:
        return status in _TRANSIENT_UPSTREAM_STATUSES
    name = exc.__class__.__name__.lower()
    return (
        "overloaded" in name
        or "apiconnection" in name
        or "apitimeout" in name
        or "internalserver" in name
    )


def classify_engine_error(exc: BaseException) -> EngineErrorClassification | None:
    """Classify ``exc``, or return ``None`` to leave it to generic handling."""
    text = extract_error_text(exc)
    # Deterministic causes are checked FIRST. A request can be both malformed
    # and unlucky, and telling the operator to retry something that cannot
    # succeed is the exact harm this module was written to stop — so a
    # recognised deterministic failure must never be masked by a transient one.
    if _is_image_dimension_error(text):
        return EngineErrorClassification(
            code=CODE_IMAGE_TOO_LARGE,
            message=(
                "One of the images earlier in this conversation is too large "
                "for a chat with this many images in it. Retrying won't clear "
                "it — the image is part of the history every message resends. "
                "Start a new chat and re-attach the images you still need, and "
                "send fewer at a time."
            ),
            retryable=False,
        )
    if _is_transient_upstream(exc):
        status = _upstream_status(exc)
        return EngineErrorClassification(
            code=CODE_ENGINE_UNAVAILABLE,
            message=(
                "The assistant's engine was briefly unavailable"
                + (f" (upstream {status})" if status else "")
                + ". Nothing is wrong with your message — send it again."
            ),
            retryable=True,
        )
    return None


def classification_payload(c: EngineErrorClassification) -> dict[str, Any]:
    """The wire shape carried to the front end alongside the error code."""
    return {"error": c.code, "detail": c.message, "retryable": c.retryable}


__all__ = [
    "CODE_ENGINE_UNAVAILABLE",
    "CODE_IMAGE_TOO_LARGE",
    "EngineErrorClassification",
    "classification_payload",
    "classify_engine_error",
    "extract_error_text",
]
