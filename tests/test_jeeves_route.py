"""``alfred.transport.routes_jeeves`` — the capture intake (task #81, stage 2).

The receiving half of the garage ambient scribe. The pins that carry weight:
the peer is pinned by NAME against a sibling token that shares its client,
the route refuses AUDIO outright, and the fail-closed mode gate runs before
the vault sees anything.

Mandatory regression pins — run unconditionally, no ``importorskip``.
"""

from __future__ import annotations

import frontmatter
import pytest
import structlog

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import register_vault_path
from alfred.transport.routes_jeeves import (
    JEEVES_PEER_NAME,
    default_title,
    register_jeeves_routes,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState

# Obviously-fake test secrets — never a real provider prefix.
DUMMY_JEEVES_PEER_TOKEN = (
    "DUMMY_JEEVES_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234567890"
)
# A SIBLING peer carrying the SAME allowed_clients. This is the whole point
# of the peer-pin: allowed_clients cannot tell these two apart, so without
# the name pin this token would drive a capture write.
DUMMY_SIBLING_TOKEN = (
    "DUMMY_SIBLING_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456789"
)

_HEADERS = {
    "Authorization": f"Bearer {DUMMY_JEEVES_PEER_TOKEN}",
    "X-Alfred-Client": "jeeves",
    "Content-Type": "application/json",
}
_SIBLING_HEADERS = {
    "Authorization": f"Bearer {DUMMY_SIBLING_TOKEN}",
    "X-Alfred-Client": "jeeves",
    "Content-Type": "application/json",
}

CAPTURE_URL = "/vault/jeeves/capture"

#: A receiving instance in LIVE mode — the state an operator reaches once
#: the device is really deployed. Synthetic-mode behaviour gets its own
#: fixture, because refusing is the DEFAULT and deserves its own pins.
LIVE_RAW_CONFIG = {"jeeves": {"mode": "live"}}
SYNTHETIC_RAW_CONFIG: dict = {}


def _transport_config() -> TransportConfig:
    """The capture token lives under the dedicated ``jeeves`` peer — the
    production peer NAME the handler pins on. The sibling shares its
    ``allowed_clients`` so the escalation pin is real."""
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                JEEVES_PEER_NAME: AuthTokenEntry(
                    token=DUMMY_JEEVES_PEER_TOKEN,
                    allowed_clients=["jeeves"],
                ),
                "jeeves_sibling": AuthTokenEntry(
                    token=DUMMY_SIBLING_TOKEN,
                    allowed_clients=["jeeves"],
                ),
            }
        ),
        state=StateConfig(),
    )


def _make_vault(tmp_path):
    vault = tmp_path / "vault"
    for sub in ("note", "source"):
        (vault / sub).mkdir(parents=True)
    return vault


async def _build_client(aiohttp_client, tmp_path, raw_config):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    vault = _make_vault(tmp_path)
    register_vault_path(app, vault)
    mounted = register_jeeves_routes(
        app, enabled=True, instance_name="TestBox",
        max_transcript_chars=2048, jeeves_raw_config=raw_config,
    )
    assert mounted is True
    app["_vault"] = vault
    return await aiohttp_client(app)


@pytest.fixture
async def live_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    return await _build_client(aiohttp_client, tmp_path, LIVE_RAW_CONFIG)


@pytest.fixture
async def synthetic_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    return await _build_client(aiohttp_client, tmp_path, SYNTHETIC_RAW_CONFIG)


def _payload(**overrides):
    base = {
        "transcript": "the bearing is the wrong size, order the 6203",
        "verb": "route",
        "captured_at": "2026-08-11T14:03:02+00:00",
        "capture_facts": {
            "lookback_used_seconds": 45.0,
            "truncated_by_ring": False,
            "cue_confidence": 0.91,
            "stt_calls": 1,
        },
        "correlation_id": "jeeves-test-001",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


async def test_a_capture_becomes_a_note_with_the_transcript_as_its_body(live_client):
    resp = await live_client.post(CAPTURE_URL, json=_payload(), headers=_HEADERS)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "created"
    assert data["record_type"] == "note"
    assert data["instance"] == "TestBox"
    assert data["correlation_id"] == "jeeves-test-001"

    written = live_client.app["_vault"] / data["path"]
    post = frontmatter.load(written)
    assert post.content.strip().endswith(
        "the bearing is the wrong size, order the 6203")


async def test_the_provenance_frontmatter_is_stamped(live_client):
    resp = await live_client.post(CAPTURE_URL, json=_payload(), headers=_HEADERS)
    data = await resp.json()
    post = frontmatter.load(live_client.app["_vault"] / data["path"])

    assert post["captured_via"] == "jeeves"
    assert post["captured_at"] == "2026-08-11T14:03:02+00:00"
    assert post["capture_verb"] == "route"
    assert post["lookback_used_seconds"] == 45.0
    assert post["capture_correlation_id"] == "jeeves-test-001"


async def test_every_capture_is_stamped_MULTI_SPEAKER(live_client):
    """The garage is a workout/lounge and a second household voice is the
    room's NORMAL condition (Q6 trial, corrected 2026-08-11). Stamping it
    means a downstream consumer is never tempted to attribute the whole
    transcript to one person."""
    resp = await live_client.post(CAPTURE_URL, json=_payload(), headers=_HEADERS)
    data = await resp.json()
    post = frontmatter.load(live_client.app["_vault"] / data["path"])
    assert post["capture_multi_speaker"] is True


async def test_a_source_type_capture_is_accepted(live_client):
    resp = await live_client.post(
        CAPTURE_URL, json=_payload(record_type="source"), headers=_HEADERS)
    assert resp.status == 200
    assert (await resp.json())["record_type"] == "source"


async def test_an_unknown_capture_fact_is_dropped_not_refused(live_client):
    """A newer device build sending a field this instance does not know must
    not have its capture rejected — but nor may an unvetted key ride into
    frontmatter where it would be read as provenance."""
    resp = await live_client.post(CAPTURE_URL, json=_payload(capture_facts={
        "lookback_used_seconds": 12.0,
        "future_metric": "something",
        "nested": {"a": 1},
        "long_string": "x" * 200,
    }), headers=_HEADERS)
    assert resp.status == 200
    post = frontmatter.load(live_client.app["_vault"] / (await resp.json())["path"])
    assert post["lookback_used_seconds"] == 12.0
    assert "future_metric" not in post.keys()
    assert "nested" not in post.keys()
    assert "long_string" not in post.keys()


async def test_the_created_log_carries_a_length_never_the_words(live_client):
    secret = "the alarm code is nine four two seven"
    with structlog.testing.capture_logs() as captured:
        await live_client.post(
            CAPTURE_URL, json=_payload(transcript=secret), headers=_HEADERS)
    events = [c for c in captured if c.get("event") == "transport.jeeves.created"]
    assert len(events) == 1
    assert events[0]["transcript_chars"] == len(secret)
    for entry in captured:
        rendered = " ".join(str(v) for v in entry.values())
        assert "alarm code" not in rendered


# ---------------------------------------------------------------------------
# NEVER RAW AUDIO
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("audio_key", [
    "audio", "audio_b64", "audio_base64", "pcm", "wav", "raw_audio",
    "recording", "samples", "window_audio", "waveform",
])
async def test_an_audio_bearing_body_is_REFUSED(live_client, audio_key):
    """The last place the cued-transcript-only fence can be checked on the
    receiving side. A device that started sending audio is told to stop
    rather than quietly filling a vault with recordings."""
    resp = await live_client.post(
        CAPTURE_URL, json=_payload(**{audio_key: "AAAA"}), headers=_HEADERS)
    assert resp.status == 400
    data = await resp.json()
    assert data["error"] == "audio_not_accepted"
    assert audio_key in data["fields"]


async def test_the_audio_refusal_names_the_field_and_writes_NOTHING(live_client):
    """REFUSAL PIN. The right question is not "did it write the record" but
    "did it touch anything" — and the logged reason is what distinguishes
    this refusal from any other 400."""
    with structlog.testing.capture_logs() as captured:
        resp = await live_client.post(
            CAPTURE_URL, json=_payload(audio_b64="AAAA"), headers=_HEADERS)
    assert resp.status == 400

    vault = live_client.app["_vault"]
    assert list((vault / "note").iterdir()) == []
    assert list((vault / "source").iterdir()) == []

    events = [c for c in captured
              if c.get("event") == "transport.jeeves.rejected"]
    assert len(events) == 1
    assert events[0]["reason"] == "audio_not_accepted"
    assert events[0]["keys"] == ["audio_b64"]
    assert events[0]["log_level"] == "error"


async def test_audio_is_refused_BEFORE_the_mode_gate(synthetic_client):
    """Ordering pin. On a synthetic-mode instance an audio-bearing body must
    be told it sent AUDIO, not that the mode refused it — otherwise flipping
    to live would silently start accepting recordings."""
    resp = await synthetic_client.post(
        CAPTURE_URL, json=_payload(audio="AAAA"), headers=_HEADERS)
    assert resp.status == 400
    assert (await resp.json())["error"] == "audio_not_accepted"


# ---------------------------------------------------------------------------
# The peer pin
# ---------------------------------------------------------------------------


async def test_a_sibling_token_sharing_the_client_is_REFUSED(live_client):
    """THE ESCALATION PIN. The sibling clears Layer 1 (valid token, allowed
    client) and resolves to a DIFFERENT peer name. Without the pin it would
    drive a capture write — which is the documented escalation class."""
    resp = await live_client.post(
        CAPTURE_URL, json=_payload(), headers=_SIBLING_HEADERS)
    assert resp.status == 401
    assert (await resp.json())["error"] == "wrong_peer"


async def test_the_wrong_peer_refusal_is_logged_with_both_names(live_client):
    with structlog.testing.capture_logs() as captured:
        await live_client.post(
            CAPTURE_URL, json=_payload(), headers=_SIBLING_HEADERS)
    events = [c for c in captured
              if c.get("event") == "transport.jeeves.rejected"]
    assert len(events) == 1
    assert events[0]["reason"] == "wrong_peer"
    assert events[0]["peer"] == "jeeves_sibling"
    assert events[0]["expected"] == JEEVES_PEER_NAME


async def test_the_wrong_peer_refusal_writes_nothing(live_client):
    await live_client.post(CAPTURE_URL, json=_payload(), headers=_SIBLING_HEADERS)
    vault = live_client.app["_vault"]
    assert list((vault / "note").iterdir()) == []


async def test_no_token_is_refused_by_layer_1(live_client):
    resp = await live_client.post(CAPTURE_URL, json=_payload())
    assert resp.status == 401


async def test_the_pinned_name_is_the_production_peer_key(live_client):
    """The fixture pins the PRODUCTION peer name, not a name that merely
    clears allowed_clients — otherwise the escalation pin above proves
    nothing."""
    assert JEEVES_PEER_NAME == "jeeves"


# ---------------------------------------------------------------------------
# The fail-closed mode gate
# ---------------------------------------------------------------------------


async def test_synthetic_mode_REFUSES_an_untagged_capture(synthetic_client):
    """The DEFAULT posture. An instance that has not been deliberately
    flipped accepts nothing real."""
    resp = await synthetic_client.post(
        CAPTURE_URL, json=_payload(), headers=_HEADERS)
    assert resp.status == 403
    data = await resp.json()
    assert data["error"] == "capture_refused"
    assert data["reason"] == "missing_synthetic_provenance"


async def test_the_gate_refusal_writes_NOTHING(synthetic_client):
    with structlog.testing.capture_logs() as captured:
        await synthetic_client.post(
            CAPTURE_URL, json=_payload(), headers=_HEADERS)
    vault = synthetic_client.app["_vault"]
    assert list((vault / "note").iterdir()) == []

    decisions = [c for c in captured
                 if c.get("event") == "jeeves.capture_decision"]
    assert len(decisions) == 1
    assert decisions[0]["accepted"] is False
    assert decisions[0]["reason"] == "missing_synthetic_provenance"


async def test_synthetic_mode_ACCEPTS_a_tagged_capture(synthetic_client):
    """The other direction — the end-to-end dev path, where neither side has
    been flipped to live and a synthetic capture still lands."""
    resp = await synthetic_client.post(
        CAPTURE_URL,
        json=_payload(provenance={"synthetic": True}),
        headers=_HEADERS,
    )
    assert resp.status == 200


async def test_a_403_is_distinct_from_a_400(synthetic_client):
    """The request was well-formed; the instance is refusing to process it.
    A device author needs those told apart."""
    good = await synthetic_client.post(
        CAPTURE_URL, json=_payload(), headers=_HEADERS)
    malformed = await synthetic_client.post(
        CAPTURE_URL, json=_payload(transcript=""), headers=_HEADERS)
    assert good.status == 403
    assert malformed.status == 400


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_a_task_capture_is_refused(live_client):
    """RULING 4 at the route: notes-only v1."""
    resp = await live_client.post(
        CAPTURE_URL, json=_payload(record_type="task"), headers=_HEADERS)
    assert resp.status == 400
    data = await resp.json()
    assert data["error"] == "invalid_type"
    assert data["allowed"] == ["note", "source"]


async def test_an_empty_transcript_is_refused(live_client):
    resp = await live_client.post(
        CAPTURE_URL, json=_payload(transcript="   "), headers=_HEADERS)
    assert resp.status == 400
    assert (await resp.json())["error"] == "empty_transcript"


async def test_an_oversize_transcript_is_refused_with_the_cap(live_client):
    resp = await live_client.post(
        CAPTURE_URL, json=_payload(transcript="x" * 5000), headers=_HEADERS)
    assert resp.status == 413
    data = await resp.json()
    assert data["error"] == "transcript_too_large"
    assert data["max_chars"] == 2048


async def test_malformed_json_is_refused(live_client):
    resp = await live_client.post(
        CAPTURE_URL, data="not json", headers=_HEADERS)
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_json"


async def test_a_repeat_capture_at_the_same_instant_is_a_collision(live_client):
    """Idempotency-on-retry: a device that resends gets a 409 and the
    existing path, which the sink treats as already-present."""
    first = await live_client.post(CAPTURE_URL, json=_payload(), headers=_HEADERS)
    assert first.status == 200
    second = await live_client.post(CAPTURE_URL, json=_payload(), headers=_HEADERS)
    assert second.status == 409
    data = await second.json()
    assert data["error"] == "title_collision"
    assert data["path"]


async def test_no_vault_configured_is_a_503(aiohttp_client, tmp_path):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    register_jeeves_routes(
        app, enabled=True, instance_name="TestBox",
        jeeves_raw_config=LIVE_RAW_CONFIG,
    )
    client = await aiohttp_client(app)
    resp = await client.post(CAPTURE_URL, json=_payload(), headers=_HEADERS)
    assert resp.status == 503
    assert (await resp.json())["error"] == "vault_not_configured"


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------


def test_the_default_title_is_derived_from_the_capture_time():
    """The device does not invent titles — it has no idea what the utterance
    is about, and a device-generated summary would be a second LLM call for
    a string the operator renames anyway."""
    title = default_title("2026-08-11T14:03:02+00:00")
    assert title.startswith("Jeeves capture ")
    assert ":" not in title
    assert "2026-08-11" in title


async def test_a_device_supplied_title_is_used(live_client):
    resp = await live_client.post(
        CAPTURE_URL, json=_payload(title="Compressor leak"), headers=_HEADERS)
    data = await resp.json()
    assert data["path"].endswith("Compressor leak.md")


def test_an_empty_capture_time_still_yields_a_title():
    assert default_title("").startswith("Jeeves capture ")


# ---------------------------------------------------------------------------
# Opt-in inertness
# ---------------------------------------------------------------------------


async def test_disabled_mounts_nothing(aiohttp_client, tmp_path):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    with structlog.testing.capture_logs() as captured:
        mounted = register_jeeves_routes(app, enabled=False)
    assert mounted is False

    client = await aiohttp_client(app)
    resp = await client.post(CAPTURE_URL, json=_payload(), headers=_HEADERS)
    assert resp.status == 404

    events = [c for c in captured
              if c.get("event") == "transport.jeeves.disabled"]
    assert len(events) == 1


async def test_registration_announces_the_mode(aiohttp_client, tmp_path):
    """A receiving instance in synthetic mode refuses every real capture, and
    the operator should learn that from a boot log rather than from a
    garage."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    with structlog.testing.capture_logs() as captured:
        register_jeeves_routes(
            app, enabled=True, instance_name="TestBox",
            jeeves_raw_config=SYNTHETIC_RAW_CONFIG,
        )
    events = [c for c in captured
              if c.get("event") == "transport.jeeves.registered"]
    assert len(events) == 1
    assert events[0]["mode"] == "synthetic"
    assert events[0]["peer"] == JEEVES_PEER_NAME
    assert "will be refused" in events[0]["detail"]


async def test_registration_announces_live_mode_differently(aiohttp_client, tmp_path):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    with structlog.testing.capture_logs() as captured:
        register_jeeves_routes(
            app, enabled=True, instance_name="TestBox",
            jeeves_raw_config=LIVE_RAW_CONFIG,
        )
    events = [c for c in captured
              if c.get("event") == "transport.jeeves.registered"]
    assert events[0]["mode"] == "live"
    assert "written to the vault" in events[0]["detail"]


async def test_a_config_narrowing_cannot_widen_the_type_set(aiohttp_client, tmp_path):
    """``types`` intersects the code-level ceiling; a config naming ``task``
    gets ``task`` refused, not granted."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    register_vault_path(app, _make_vault(tmp_path))
    register_jeeves_routes(
        app, enabled=True, instance_name="TestBox",
        types=["note", "task"], jeeves_raw_config=LIVE_RAW_CONFIG,
    )
    client = await aiohttp_client(app)

    refused = await client.post(
        CAPTURE_URL, json=_payload(record_type="task"), headers=_HEADERS)
    assert refused.status == 400
    assert (await refused.json())["allowed"] == ["note"]

    # ...and the narrowing really narrows: source is no longer allowed.
    narrowed = await client.post(
        CAPTURE_URL, json=_payload(record_type="source"), headers=_HEADERS)
    assert narrowed.status == 400
