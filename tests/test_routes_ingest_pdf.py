"""#57 PDF half — ``POST /vault/ingest`` with a base64 PDF body.

These drive the PRODUCTION ENTRY POINT (the mounted aiohttp route), not the
helper underneath it. That distinction is the point: a feature exercised only
by direct invocation is the standing trap where every unit pin is green and the
route never threads what it needs. The CSV half shipped with the same
discipline and this half inherits it.

What is held here:

* the happy path writes a record whose body is the extracted text, FENCED, with
  provenance saying it arrived as a PDF and how large that PDF was;
* **six DISTINCT refusals** — empty, invalid base64, oversize file, oversize
  extracted text, encrypted, corrupt, no-text-layer — each with its own wire
  code and its own logged reason. A refusal that only proves "it said no" is
  green against a build that returns the same code for everything, which is the
  operator-facing version of silence;
* the **peer-pin still holds** with a PDF payload, pinned to the production
  peer NAME (per CLAUDE.md's asserted-identity rule);
* the pre-#57 TEXT path is byte-for-byte unchanged.
"""

from __future__ import annotations

import base64

import frontmatter
import pytest
import structlog

from alfred.documents.pdf import MAX_PDF_BYTES
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import register_vault_path
from alfred.transport.routes_ingest import (
    INGEST_PEER_NAME,
    fence_extracted_text,
    register_ingest_routes,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState

from .pdf_fixtures import (
    corrupt_pdf,
    encrypted_pdf,
    not_a_pdf,
    oversize_text_pdf,
    scanned_pdf,
    text_layer_pdf,
)

# Obviously-fake test secrets — never a real provider prefix (builder.md
# GitGuardian rule).
DUMMY_INGEST_PEER_TOKEN = (
    "DUMMY_WEB_INGEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
)
DUMMY_WEB_CHAT_TOKEN = (
    "DUMMY_WEB_CHAT_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_012345678"
)

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_INGEST_PEER_TOKEN}",
    "X-Alfred-Client": "web",
    "Content-Type": "application/json",
}

MAX_BODY_CHARS = 2048


def _transport_config() -> TransportConfig:
    """The ingest token sits under the PRODUCTION peer name the handler pins
    on (``web_ingest``), with a sibling chat ``web`` peer sharing
    ``allowed_clients`` so the escalation test can present a token that clears
    Layer 1 and must still be refused."""
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                INGEST_PEER_NAME: AuthTokenEntry(
                    token=DUMMY_INGEST_PEER_TOKEN, allowed_clients=["web"],
                ),
                "web": AuthTokenEntry(
                    token=DUMMY_WEB_CHAT_TOKEN, allowed_clients=["web"],
                ),
            }
        ),
        state=StateConfig(),
    )


@pytest.fixture
async def ingest_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    vault = tmp_path / "vault"
    for sub in ("document", "note", "source"):
        (vault / sub).mkdir(parents=True)
    register_vault_path(app, vault)
    assert register_ingest_routes(
        app, enabled=True, instance_name="Salem",
        max_body_chars=MAX_BODY_CHARS,
    ) is True
    app["_vault"] = vault
    return await aiohttp_client(app)


def _pdf_payload(pdf_bytes: bytes, **overrides):
    base = {
        "record_type": "document",
        "title": "Bank Statement July 2026",
        "body_format": "pdf",
        "body_b64": base64.b64encode(pdf_bytes).decode("ascii"),
        "source": "uploaded from the ingest page",
        "ingested_by": "andrew",
        "correlation_id": "ingest-pdf-001",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


async def test_a_text_layer_pdf_lands_as_a_fenced_record(ingest_client) -> None:
    vault = ingest_client.app["_vault"]
    pdf = text_layer_pdf(["Opening balance 1,204.55", "ACME UTILITIES 84.20"])

    with structlog.testing.capture_logs() as captured:
        resp = await ingest_client.post(
            "/vault/ingest", json=_pdf_payload(pdf), headers=_PEER_HEADERS,
        )
    assert resp.status == 200, await resp.text()
    payload = await resp.json()
    assert payload["status"] == "created"

    post = frontmatter.load(vault / payload["path"])
    assert "Opening balance 1,204.55" in post.content
    assert "ACME UTILITIES 84.20" in post.content
    assert "```" in post.content, "extracted text is FENCED, like the CSV half"

    created = [e for e in captured if e.get("event") == "transport.ingest.created"]
    assert len(created) == 1
    assert created[0]["body_format"] == "pdf"
    assert created[0]["pdf_bytes"] == len(pdf)


async def test_the_record_records_that_it_arrived_as_a_pdf(ingest_client) -> None:
    """Once extracted and fenced, nothing in the body says it began as a PDF.
    A reader six months on should not have to guess."""
    vault = ingest_client.app["_vault"]
    pdf = text_layer_pdf()
    resp = await ingest_client.post(
        "/vault/ingest", json=_pdf_payload(pdf), headers=_PEER_HEADERS,
    )
    payload = await resp.json()
    meta = frontmatter.load(vault / payload["path"]).metadata
    assert meta["ingested_format"] == "pdf"
    assert meta["ingested_source_bytes"] == len(pdf)
    assert meta["ingested_via"] == "web"
    assert meta["ingested_by"] == "andrew"


async def test_the_extraction_log_reports_both_axes(ingest_client) -> None:
    """Observability pin. Bytes in and characters out are the two numbers that
    explain a later refusal, so both are on the success line too."""
    with structlog.testing.capture_logs() as captured:
        await ingest_client.post(
            "/vault/ingest", json=_pdf_payload(text_layer_pdf()),
            headers=_PEER_HEADERS,
        )
    events = [e for e in captured
              if e.get("event") == "transport.ingest.pdf_extracted"]
    assert len(events) == 1
    assert events[0]["file_bytes"] > 0
    assert events[0]["text_chars"] > 0


# ---------------------------------------------------------------------------
# the six DISTINCT refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_pdf,status,code",
    [
        (scanned_pdf, 422, "pdf_no_text_layer"),
        (encrypted_pdf, 422, "pdf_encrypted"),
        (corrupt_pdf, 400, "pdf_unreadable"),
        (not_a_pdf, 400, "pdf_unreadable"),
    ],
)
async def test_each_bad_pdf_is_refused_with_its_own_code(
    ingest_client, make_pdf, status, code,
) -> None:
    with structlog.testing.capture_logs() as captured:
        resp = await ingest_client.post(
            "/vault/ingest", json=_pdf_payload(make_pdf()),
            headers=_PEER_HEADERS,
        )
    assert resp.status == status
    assert (await resp.json())["error"] == code

    rejected = [e for e in captured
                if e.get("event") == "transport.ingest.rejected"]
    assert len(rejected) == 1, "the refusal is logged exactly once"
    assert rejected[0]["reason"] == code, (
        "the LOGGED reason must match the wire code — a refusal for an "
        "unrelated cause reads identically on the wire otherwise"
    )


async def test_a_zero_byte_upload_is_its_own_refusal(ingest_client) -> None:
    resp = await ingest_client.post(
        "/vault/ingest", json=_pdf_payload(b""), headers=_PEER_HEADERS,
    )
    # An empty b64 string is an EMPTY BODY, distinct from a file that decoded
    # to nothing — both are refused, and neither is "unreadable".
    assert resp.status == 400
    assert (await resp.json())["error"] in {"empty_body", "empty_file"}


async def test_malformed_base64_is_refused_before_extraction(
    ingest_client,
) -> None:
    """``validate=True`` on the decode: stray bytes are an error rather than
    silently skipped, so a truncated upload cannot decode into a plausible
    shorter PDF and get written as if whole."""
    with structlog.testing.capture_logs() as captured:
        resp = await ingest_client.post(
            "/vault/ingest",
            json=_pdf_payload(b"", body_b64="!!!! not base64 at all !!!!"),
            headers=_PEER_HEADERS,
        )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_base64"
    assert any(e.get("reason") == "invalid_base64" for e in captured)


async def test_an_oversize_FILE_names_the_byte_limit(
    ingest_client, monkeypatch,
) -> None:
    """The byte axis. Patched on ``routes_ingest`` rather than on the defining
    module because the route binds the name at IMPORT time — patching
    ``documents.pdf.MAX_PDF_BYTES`` would leave the route's already-bound copy
    untouched and the test green against an unenforced cap. The real value is
    pinned separately."""
    from alfred.transport import routes_ingest

    monkeypatch.setattr(routes_ingest, "MAX_PDF_BYTES", 200)
    resp = await ingest_client.post(
        "/vault/ingest", json=_pdf_payload(text_layer_pdf()),
        headers=_PEER_HEADERS,
    )
    assert resp.status == 413
    body = await resp.json()
    assert body["error"] == "file_too_large"
    assert body["max_bytes"] == 200, "the refusal names the limit it enforced"


async def test_an_oversize_EXTRACTION_is_a_different_refusal(
    ingest_client,
) -> None:
    """The character axis, and the reason the two are separate codes: this file
    is UNDER the byte cap but OVER the character cap. Telling the operator to
    shrink the file would be advice that cannot work — the file was never the
    problem. (Measured: extraction shrinks a PDF rather than growing it, 22,980
    bytes in to 19,199 characters out, so the fixture clears the byte cap on
    both counts.)"""
    with structlog.testing.capture_logs() as captured:
        resp = await ingest_client.post(
            "/vault/ingest", json=_pdf_payload(oversize_text_pdf(400)),
            headers=_PEER_HEADERS,
        )
    assert resp.status == 413
    body = await resp.json()
    assert body["error"] == "extracted_text_too_large", (
        "distinct from body_too_large — the file was fine, the text was not"
    )
    assert body["max_chars"] == MAX_BODY_CHARS
    assert any(e.get("reason") == "extracted_text_too_large" for e in captured)


def test_the_refusal_codes_are_all_distinct() -> None:
    """Guards the parametrized rows from a mutation that maps every reason to
    one code, which would leave them all green."""
    from alfred.transport.routes_ingest import _PDF_REASON_STATUS

    codes = {code for _status, code in _PDF_REASON_STATUS.values()}
    assert len(codes) == len(_PDF_REASON_STATUS)


# ---------------------------------------------------------------------------
# the peer-pin still holds on the new payload shape
# ---------------------------------------------------------------------------


async def test_the_chat_web_token_cannot_drive_a_pdf_ingest(
    ingest_client,
) -> None:
    """CLAUDE.md's asserted-identity rule, re-pinned for the PDF shape. The
    chat ``web`` token clears Layer 1 (same ``allowed_clients``), so without
    the peer-pin it would drive a deterministic vault write. A new payload
    shape must not become a new door."""
    with structlog.testing.capture_logs() as captured:
        resp = await ingest_client.post(
            "/vault/ingest", json=_pdf_payload(text_layer_pdf()),
            headers={**_PEER_HEADERS,
                     "Authorization": f"Bearer {DUMMY_WEB_CHAT_TOKEN}"},
        )
    assert resp.status == 401
    assert (await resp.json())["error"] == "wrong_peer"
    assert any(e.get("reason") == "wrong_peer" for e in captured)


async def test_an_unauthenticated_pdf_upload_is_refused(ingest_client) -> None:
    resp = await ingest_client.post(
        "/vault/ingest", json=_pdf_payload(text_layer_pdf()),
        headers={"X-Alfred-Client": "web", "Content-Type": "application/json"},
    )
    assert resp.status == 401


# ---------------------------------------------------------------------------
# the pre-#57 text path must not move
# ---------------------------------------------------------------------------


async def test_a_plain_text_body_is_still_written_verbatim(
    ingest_client,
) -> None:
    """Regression. .md / .txt / .csv send a ready body string and must land
    byte-for-byte, with no fence and no PDF provenance."""
    vault = ingest_client.app["_vault"]
    body_text = "# Heading\n\nLine one.\n\n- bullet A\n- bullet B\n"
    resp = await ingest_client.post(
        "/vault/ingest",
        json={"record_type": "document", "title": "Plain Text Doc",
              "body": body_text, "ingested_by": "andrew"},
        headers=_PEER_HEADERS,
    )
    assert resp.status == 200
    payload = await resp.json()
    post = frontmatter.load(vault / payload["path"])
    assert post.content.strip() == body_text.strip()
    assert "ingested_format" not in post.metadata, (
        "a text upload is not stamped as a PDF"
    )


async def test_an_absent_body_format_takes_the_text_path(ingest_client) -> None:
    """Absent and ``"text"`` mean the same thing, so an older client that
    never learned the field keeps working unchanged."""
    # Titles must differ beyond case: the vault refuses NEAR-matches, so
    # "Doc fmt text" and "Doc fmt TEXT" would collide on the second write and
    # the 409 would look like a body_format bug.
    for n, fmt in enumerate((None, "text", "TEXT", "  Text  ")):
        payload = {"record_type": "note", "title": f"Older Client Doc {n}",
                   "body": "hello", "ingested_by": "andrew"}
        if fmt is not None:
            payload["body_format"] = fmt
        resp = await ingest_client.post(
            "/vault/ingest", json=payload, headers=_PEER_HEADERS,
        )
        assert resp.status == 200, f"format {fmt!r}: {await resp.text()}"


# ---------------------------------------------------------------------------
# fencing
# ---------------------------------------------------------------------------


def test_the_fence_grows_past_backticks_in_the_content() -> None:
    """A statement quoting a code block must not close the fence early —
    the same rule the CSV half applies, because the same document should read
    identically whichever door it came through."""
    fenced = fence_extracted_text("before\n```\ninner\n```\nafter")
    assert fenced.startswith("````"), "fence is longer than the longest run"
    assert fenced.rstrip("\n").endswith("````")


def test_the_fence_leaves_the_content_alone() -> None:
    text = "Line one\nLine two"
    fenced = fence_extracted_text(text)
    assert "Line one\nLine two" in fenced


def test_the_fence_closes_on_its_own_line_when_text_lacks_a_newline() -> None:
    """The single documented departure from byte-for-byte: a closing fence
    must begin a line."""
    fenced = fence_extracted_text("no trailing newline")
    assert "\n```" in fenced


def test_the_byte_cap_the_route_enforces_is_the_ratified_one() -> None:
    """Pinned separately from the behaviour test above, which patches it."""
    from alfred.transport import routes_ingest

    assert routes_ingest.MAX_PDF_BYTES == MAX_PDF_BYTES == 10 * 1024 * 1024
