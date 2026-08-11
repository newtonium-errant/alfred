"""#83 item 1 — ``POST /vault/batch``, the save-only bulk scan intake route.

These drive a REAL aiohttp app over a REAL socket with REAL multipart bodies,
not a stubbed handler. That is deliberate and load-bearing for this route in a
way it would not be for a JSON one: the whole design rests on a measured aiohttp
behaviour — ``request.multipart()`` does not enforce ``client_max_size`` — and a
test that called the handler directly with a fake request would prove nothing
about it. ``test_multipart_bypasses_the_app_client_max_size`` pins the mechanism
itself, so a future aiohttp upgrade that starts enforcing the ceiling fails HERE
with a clear reason rather than as a mysterious HTML 413 in production.

The refusal pins assert WHY the route refused and WHAT STATE IT LEFT — the wire
code plus the filesystem — never merely that a non-2xx came back. A refusal for
an unrelated reason renders identically to the guard firing, and a build with no
guard at all still returns a non-2xx for a malformed request.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from alfred.batch.manifest import load_manifest
from alfred.batch.paths import batch_root, images_dir, manifest_path
from alfred.transport.routes_batch import (
    ALLOWED_SCAN_MEDIA_TYPES,
    BATCH_PEER_NAME,
    mint_batch_id,
    register_batch_routes,
)

_INSTANCE = "Salem"

# A tiny valid-enough JPEG-ish payload. The route never decodes an image (that
# is the campaign's job), so byte content only has to be distinct per scan.
def _scan(seed: int, size: int = 64) -> bytes:
    return b"\xff\xd8\xff" + bytes([seed % 256]) * (size - 3)


def _saved_batches(data_dir: Path | str) -> list[str]:
    """Batch directories that survived the request — the debris assertion.

    Deliberately not "does ``batch_root`` exist": the per-instance root is
    created on the way in and REUSED across submissions, so its presence says
    nothing. What must be true after a refusal is that no BATCH was left
    behind, which is this list being empty.
    """
    root = batch_root(data_dir, _INSTANCE)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


# ---------------------------------------------------------------------------
# App under test
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "note").mkdir(parents=True)
    return v


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


def _make_app(
    *,
    vault: Path,
    data_dir: Path,
    peer: str = BATCH_PEER_NAME,
    client_max_size: int = 14 * 1024 * 1024,
    **caps,
) -> web.Application:
    """The batch route on an app whose middleware stubs the peer resolution.

    ``peer`` stands in for what ``auth_middleware`` would set from the matched
    ``auth.tokens`` key. Pinning it to the PRODUCTION peer name (rather than any
    name that happens to clear ``allowed_clients``) is the point of the fixture:
    the escalation this route's pin exists to stop is a DIFFERENT valid token
    with the same client name.
    """
    @web.middleware
    async def _stub_auth(request, handler):
        request["transport_peer"] = peer
        return await handler(request)

    app = web.Application(
        middlewares=[_stub_auth], client_max_size=client_max_size,
    )
    from alfred.transport.peer_handlers import register_vault_path

    register_vault_path(app, vault)
    register_batch_routes(
        app,
        enabled=True,
        instance_name=_INSTANCE,
        data_dir=str(data_dir),
        **caps,
    )
    return app


def _form(
    images: list[tuple[str, bytes, str]],
    *,
    instruction: str | None = "Transcribe the invoice total.",
    title: str | None = None,
    force_multipart: bool = False,
) -> "object":
    """Build the request body the browser would send.

    ``force_multipart`` exists for one case: aiohttp's ``FormData`` optimises a
    body with no file parts down to ``application/x-www-form-urlencoded``, but
    the browser's ``FormData`` is ALWAYS ``multipart/form-data``. Without the
    flag, a zero-image submission would arrive here as urlencoded and be refused
    as ``not_multipart`` — testing aiohttp's client optimisation instead of the
    route's empty-batch guard, which is the shape a real operator hits.
    Attaching an explicit content type to a text field flips FormData back to
    multipart, which is the browser's wire shape.
    """
    from aiohttp import FormData

    form = FormData()
    if instruction is not None:
        form.add_field(
            "instruction", instruction,
            **({"content_type": "text/plain"} if force_multipart else {}),
        )
    if title is not None:
        form.add_field("title", title)
    for name, payload, media_type in images:
        form.add_field("images", payload, filename=name, content_type=media_type)
    return form


# ---------------------------------------------------------------------------
# The measured mechanism this route is built on
# ---------------------------------------------------------------------------


async def test_multipart_bypasses_the_app_client_max_size(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """MEASURED, and the foundation of every cap in this route.

    The app ceiling here is 1 KiB and the body is far over it. If aiohttp ever
    starts enforcing ``client_max_size`` on ``multipart()``, this fails and the
    route's whole refusal taxonomy is known to be unreachable — which is a fact
    worth failing loudly for, since the symptom otherwise is a bare HTML 413
    that no error code explains.
    """
    client = await aiohttp_client(
        _make_app(vault=vault, data_dir=data_dir, client_max_size=1024),
    )
    payload = _scan(1, size=200 * 1024)
    resp = await client.post(
        "/vault/batch", data=_form([("a.jpg", payload, "image/jpeg")]),
    )
    assert resp.status == 200, (
        f"multipart no longer bypasses client_max_size (got {resp.status}); "
        "this route's caps are now unreachable"
    )
    body = await resp.json()
    assert body["bytes"] == len(payload)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_submission_saves_images_manifest_and_record(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """The route's whole contract, asserted as three durable artifacts."""
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", data=_form([
        ("one.jpg", _scan(1), "image/jpeg"),
        ("two.png", _scan(2), "image/png"),
    ], instruction="Read the total from each invoice."))
    assert resp.status == 200
    body = await resp.json()

    # "saved" because this fixture wires no consumer; the queued-vs-saved
    # distinction has its own pins below. What this test is about is that the
    # three durable artifacts exist either way.
    assert body["status"] == "saved"
    assert body["images"] == 2
    batch_id = body["batch_id"]

    # 1. The images are on disk, named by content hash, OUTSIDE the vault.
    stored = sorted(p.name for p in images_dir(data_dir, _INSTANCE, batch_id).iterdir())
    assert len(stored) == 2
    assert not any(vault.rglob("*.jpg")), "scans must not land in the vault"

    # 2. The manifest is the frozen work-list, and it points at the record.
    manifest = load_manifest(manifest_path(data_dir, _INSTANCE, batch_id))
    assert manifest is not None
    assert manifest.batch_id == batch_id
    assert manifest.instruction == "Read the total from each invoice."
    assert len(manifest.images) == 2
    assert manifest.record_path == body["path"]
    # Every manifest filename must actually exist — the worker reads by name.
    for img in manifest.images:
        assert (images_dir(data_dir, _INSTANCE, batch_id) / img.filename).is_file()

    # 3. The carried record is minted OPEN and owned by this batch.
    from alfred.vault.ops import vault_read

    record = vault_read(vault, manifest.record_path)
    fm = record["frontmatter"]
    assert fm["batch_id"] == batch_id
    assert fm["batch_status"] == "open"
    assert fm["batch_items_total"] == 2
    assert fm["batch_items_done"] == 0


async def test_the_minted_record_is_regenerable_by_the_worker(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """The seal guard must ACCEPT what this route mints.

    The route and the worker are the two ends of one contract, and a record
    minted without ``batch_status: open`` or without the ownership marker would
    be refused by ``assert_regenerable`` on the very first scan — after the
    upload, after the model call was queued. Cheaper to pin here.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", data=_form([("a.jpg", _scan(1), "image/jpeg")]))
    body = await resp.json()

    from alfred.batch.seal import assert_regenerable
    from alfred.vault.ops import vault_read

    record = vault_read(vault, body["path"])
    assert_regenerable(
        record["frontmatter"], batch_id=body["batch_id"], record_path=body["path"],
    )


async def test_the_work_list_the_campaign_builds_matches_what_was_saved(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """End-to-end across the seam: submit here, then read with the campaign.

    This is the pin that catches a path divergence between the route's writer
    and the campaign's reader. Unit tests on either side pass while the feature
    is completely dead, because each is self-consistent about the wrong path.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", data=_form([
        ("a.jpg", _scan(1), "image/jpeg"),
        ("b.jpg", _scan(2), "image/jpeg"),
        ("c.jpg", _scan(3), "image/jpeg"),
    ]))
    body = await resp.json()

    from alfred.drip.campaigns import BatchImageCampaign

    campaign = BatchImageCampaign(
        data_dir=data_dir, instance=_INSTANCE, vault_path=vault,
        model="claude-sonnet-5", max_tokens=1024,
        api_key="DUMMY_ANTHROPIC_TEST_KEY",
    )
    worklist = campaign.worklist()
    assert len(worklist) == 3, worklist
    assert all(i.startswith(f"{body['batch_id']}::") for i in worklist), worklist


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


async def test_a_retransmitted_identical_scan_dedupes_to_one_item(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """Same bytes twice = one item, and the response SAYS so.

    Two manifest entries for one content hash would be two drip items sharing
    one ledger row: the second could never verify, so the batch would never
    drain. The ``duplicates`` count is what keeps this from reading as silent
    loss when the operator picked 3 files and the record shows 2.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    same = _scan(7)
    resp = await client.post("/vault/batch", data=_form([
        ("first.jpg", same, "image/jpeg"),
        ("second.jpg", same, "image/jpeg"),
        ("other.jpg", _scan(8), "image/jpeg"),
    ]))
    body = await resp.json()
    assert body["images"] == 2
    assert body["duplicates"] == 1

    manifest = load_manifest(manifest_path(data_dir, _INSTANCE, body["batch_id"]))
    assert len({i.item_id for i in manifest.images}) == 2


async def test_a_clean_submission_reports_no_duplicates_key(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """The key appears only when it has something to say."""
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", data=_form([("a.jpg", _scan(1), "image/jpeg")]))
    assert "duplicates" not in await resp.json()


# ---------------------------------------------------------------------------
# The peer pin
# ---------------------------------------------------------------------------


async def test_the_chat_peer_cannot_submit_a_batch(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """THE escalation pin — a DIFFERENT valid peer, not an absent one.

    ``web`` is a real token with ``allowed_clients: [web]``, exactly like
    ``web_batch``, so Layer 1 passes and only the handler's pin stands between a
    full-chat token and queued paid vision work. Asserts the reason code AND
    that NOTHING was written — a 401 alone would also be produced by an
    unrelated auth failure.
    """
    client = await aiohttp_client(
        _make_app(vault=vault, data_dir=data_dir, peer="web"),
    )
    resp = await client.post("/vault/batch", data=_form([("a.jpg", _scan(1), "image/jpeg")]))
    assert resp.status == 401
    assert (await resp.json())["error"] == "wrong_peer"
    # The victim state: no batch directory, no vault record, nothing on disk.
    assert _saved_batches(data_dir) == []
    assert not list(vault.rglob("*.md"))


async def test_an_unauthenticated_peer_cannot_submit_a_batch(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir, peer=""))
    resp = await client.post("/vault/batch", data=_form([("a.jpg", _scan(1), "image/jpeg")]))
    assert resp.status == 401
    assert (await resp.json())["error"] == "wrong_peer"
    assert _saved_batches(data_dir) == []


# ---------------------------------------------------------------------------
# The three caps — three codes, three remedies, and no debris
# ---------------------------------------------------------------------------


async def test_an_oversize_image_is_refused_by_its_own_cap(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """PER-IMAGE. The remedy is 'send a smaller scan'."""
    client = await aiohttp_client(
        _make_app(vault=vault, data_dir=data_dir, max_image_bytes=1024),
    )
    resp = await client.post("/vault/batch", data=_form([
        ("huge.jpg", _scan(1, size=8192), "image/jpeg"),
    ]))
    assert resp.status == 413
    body = await resp.json()
    assert body["error"] == "image_too_large"
    assert body["max_bytes"] == 1024
    assert body["filename"] == "huge.jpg"
    assert _saved_batches(data_dir) == [], "refusal left a batch behind"


async def test_too_many_images_is_refused_by_the_count_cap(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """COUNT. The remedy is 'send fewer scans' — a different action."""
    client = await aiohttp_client(
        _make_app(vault=vault, data_dir=data_dir, max_images=2),
    )
    resp = await client.post("/vault/batch", data=_form([
        (f"s{i}.jpg", _scan(i), "image/jpeg") for i in range(4)
    ]))
    assert resp.status == 413
    body = await resp.json()
    assert body["error"] == "too_many_images"
    assert body["max_images"] == 2
    assert _saved_batches(data_dir) == []


async def test_an_oversize_batch_is_refused_by_the_total_cap(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """TOTAL BYTES — and each image is individually UNDER the per-image cap.

    Sized so only the total cap can be the one that fires: a build that
    enforced only the per-image budget would accept this, and a test whose
    images were also individually oversize could not tell the two apart.
    """
    client = await aiohttp_client(_make_app(
        vault=vault, data_dir=data_dir,
        max_image_bytes=4096, max_total_bytes=6000,
    ))
    resp = await client.post("/vault/batch", data=_form([
        ("a.jpg", _scan(1, size=3000), "image/jpeg"),
        ("b.jpg", _scan(2, size=3000), "image/jpeg"),
        ("c.jpg", _scan(3, size=3000), "image/jpeg"),
    ]))
    assert resp.status == 413
    body = await resp.json()
    assert body["error"] == "batch_too_large"
    assert body["max_bytes"] == 6000
    assert _saved_batches(data_dir) == []


def test_the_three_caps_are_distinct_codes() -> None:
    """A drift pin: collapsing any two into one code loses the remedy.

    'Make it smaller', 'send fewer' and 'split the batch' are three different
    actions, so an operator handed one code for all three cannot act on it.
    """
    assert len({"image_too_large", "too_many_images", "batch_too_large"}) == 3


# ---------------------------------------------------------------------------
# Content and shape refusals
# ---------------------------------------------------------------------------


async def test_a_batch_with_no_images_is_refused_with_words(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """ILB: an empty batch must not mint a record that can never fill in."""
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", data=_form([], force_multipart=True))
    assert resp.status == 400
    assert (await resp.json())["error"] == "no_images"
    assert not list(vault.rglob("*.md")), "an empty batch minted a record"
    assert _saved_batches(data_dir) == []


async def test_a_missing_instruction_is_refused(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """Every scan is processed against the instruction; there is no default."""
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", data=_form(
        [("a.jpg", _scan(1), "image/jpeg")], instruction=None,
    ))
    assert resp.status == 400
    assert (await resp.json())["error"] == "empty_instruction"
    assert not list(vault.rglob("*.md"))
    assert _saved_batches(data_dir) == []


async def test_a_whitespace_only_instruction_is_refused(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """`||`-style emptiness: whitespace is truthy and must not pass as prose."""
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", data=_form(
        [("a.jpg", _scan(1), "image/jpeg")], instruction="   \n  ",
    ))
    assert resp.status == 400
    assert (await resp.json())["error"] == "empty_instruction"


async def test_an_overlong_instruction_is_refused_with_its_limit(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """It is resent with every per-image call, so its cost scales with N."""
    client = await aiohttp_client(
        _make_app(vault=vault, data_dir=data_dir, max_instruction_chars=50),
    )
    resp = await client.post("/vault/batch", data=_form(
        [("a.jpg", _scan(1), "image/jpeg")], instruction="x" * 200,
    ))
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "instruction_too_long"
    assert body["max_chars"] == 50
    assert _saved_batches(data_dir) == []


async def test_a_non_image_part_is_refused_naming_what_is_allowed(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", data=_form([
        ("notes.pdf", b"%PDF-1.4 not an image", "application/pdf"),
    ]))
    assert resp.status == 415
    body = await resp.json()
    assert body["error"] == "unsupported_media_type"
    assert body["media_type"] == "application/pdf"
    assert "image/jpeg" in body["allowed"]
    assert _saved_batches(data_dir) == []


async def test_an_empty_image_part_is_refused(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """A zero-byte scan is a failed read at the client, not a valid scan."""
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", data=_form([
        ("empty.jpg", b"", "image/jpeg"),
    ]))
    assert resp.status == 400
    assert (await resp.json())["error"] == "empty_image"
    assert _saved_batches(data_dir) == []


async def test_a_non_multipart_body_is_refused(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """The JSON shape the sibling ingest route takes is not this route's."""
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    resp = await client.post("/vault/batch", json={"instruction": "hi"})
    assert resp.status == 415
    assert (await resp.json())["error"] == "not_multipart"


async def test_an_unconfigured_batch_path_is_refused_not_guessed(
    aiohttp_client, vault: Path,
) -> None:
    """Fail loud: an unscoped batch dir is shared across instances on the box.

    A guessed default here would mix Salem's and KAL-LE's batches — the
    2026-07-31 feed-store incident, in a directory nobody inspects.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=""))
    resp = await client.post("/vault/batch", data=_form([("a.jpg", _scan(1), "image/jpeg")]))
    assert resp.status == 503
    assert (await resp.json())["error"] == "batch_not_configured"


# ---------------------------------------------------------------------------
# The on-submit kick (#83 item 5)
# ---------------------------------------------------------------------------


async def test_a_submission_kicks_one_drip_run(
    aiohttp_client, vault: Path, data_dir: Path, monkeypatch,
) -> None:
    """THE wiring pin for the kick, driven through the production route.

    A kick helper that nothing calls is the standing trap here: its own unit
    tests pass, the route returns 200, and every batch quietly waits for the
    hourly timer. Only a test that goes through the route can see it.
    """
    calls: list[dict] = []
    import alfred.drip.kick as kick_mod

    monkeypatch.setattr(
        kick_mod, "kick_drip_run",
        lambda **kw: calls.append(kw) or 999,
    )

    client = await aiohttp_client(_make_app(
        vault=vault, data_dir=data_dir,
        config_path="/etc/alfred/config.vera.yaml", kick_enabled=True,
    ))
    resp = await client.post("/vault/batch", data=_form([("a.jpg", _scan(1), "image/jpeg")]))
    assert resp.status == 200
    assert (await resp.json())["status"] == "queued"

    assert len(calls) == 1, "the route did not kick a run"
    assert calls[0]["campaign"] == "batch_image"
    assert calls[0]["config_path"] == "/etc/alfred/config.vera.yaml"
    # The kicked run must look in the directory the route just wrote to.
    assert str(calls[0]["data_dir"]) == str(data_dir)


async def test_exactly_one_run_is_kicked_per_submission(
    aiohttp_client, vault: Path, data_dir: Path, monkeypatch,
) -> None:
    """One submission, one kick — not one per image.

    Sixty scans kicking sixty runs would be sixty processes contending for one
    lock, of which 59 immediately stand down. Harmless by luck, wasteful by
    design, and the sort of thing that only shows up on a big real batch.
    """
    calls: list[dict] = []
    import alfred.drip.kick as kick_mod

    monkeypatch.setattr(kick_mod, "kick_drip_run", lambda **kw: calls.append(kw) or 1)

    client = await aiohttp_client(_make_app(
        vault=vault, data_dir=data_dir, config_path="c.yaml", kick_enabled=True,
    ))
    await client.post("/vault/batch", data=_form([
        (f"s{i}.jpg", _scan(i), "image/jpeg") for i in range(5)
    ]))
    assert len(calls) == 1


async def test_a_refused_submission_kicks_nothing(
    aiohttp_client, vault: Path, data_dir: Path, monkeypatch,
) -> None:
    """There is nothing to drain, so there is nothing to start."""
    calls: list[dict] = []
    import alfred.drip.kick as kick_mod

    monkeypatch.setattr(kick_mod, "kick_drip_run", lambda **kw: calls.append(kw) or 1)

    client = await aiohttp_client(_make_app(
        vault=vault, data_dir=data_dir, config_path="c.yaml", kick_enabled=True,
    ))
    resp = await client.post("/vault/batch", data=_form(
        [("a.jpg", _scan(1), "image/jpeg")], instruction=None,
    ))
    assert resp.status == 400
    assert calls == []


async def test_with_no_consumer_the_answer_is_saved_not_queued(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """ILB, and the most important signal on this route.

    With the batch_image campaign disabled the scans ARE saved but nothing will
    ever read them. Answering "queued" would send the operator away to wait for
    results that cannot arrive, so the two facts get two different words — and
    the warn line names the campaign they need to enable.
    """
    import structlog

    client = await aiohttp_client(_make_app(
        vault=vault, data_dir=data_dir, kick_enabled=False,
    ))
    with structlog.testing.capture_logs() as captured:
        resp = await client.post(
            "/vault/batch", data=_form([("a.jpg", _scan(1), "image/jpeg")]),
        )

    body = await resp.json()
    assert resp.status == 200
    assert body["status"] == "saved", "promised processing with no consumer"
    # The submission is still fully durable — this is not a refusal.
    assert body["images"] == 1
    assert _saved_batches(data_dir) != []

    warned = [c for c in captured if c.get("event") == "transport.batch.no_consumer"]
    assert len(warned) == 1
    assert warned[0]["campaign"] == "batch_image"


def test_a_kick_without_a_config_path_is_disabled_and_says_so() -> None:
    """Enabled-but-unusable must not present as enabled.

    ``kick_enabled`` and ``config_path`` are ANDed at registration rather than
    checked per request, so the route cannot promise "queued" while every kick
    silently declines for a missing ``--config``.
    """
    import structlog

    app = web.Application()
    with structlog.testing.capture_logs() as captured:
        register_batch_routes(
            app, enabled=True, instance_name=_INSTANCE, data_dir="/tmp/x",
            kick_enabled=True, config_path="",
        )
    events = {c["event"]: c for c in captured}
    assert "transport.batch.kick_unavailable" in events
    assert events["transport.batch.registered"]["kick_enabled"] is False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_route_is_not_mounted_when_disabled() -> None:
    """Opt-in inertness: an un-opted-in instance's app is byte-unchanged."""
    app = web.Application()
    assert register_batch_routes(app, enabled=False, instance_name=_INSTANCE) is False
    assert [r.resource.canonical for r in app.router.routes()] == []


def test_the_route_is_mounted_when_enabled(tmp_path: Path) -> None:
    app = web.Application()
    assert register_batch_routes(
        app, enabled=True, instance_name=_INSTANCE, data_dir=str(tmp_path),
    ) is True
    assert "/vault/batch" in [r.resource.canonical for r in app.router.routes()]


def test_disabled_registration_says_so(caplog) -> None:
    """ILB: 'no batch route' must be distinguishable from 'wiring skipped'."""
    import structlog

    with structlog.testing.capture_logs() as captured:
        register_batch_routes(web.Application(), enabled=False, instance_name=_INSTANCE)
    events = [c["event"] for c in captured]
    assert "transport.batch.disabled" in events


# ---------------------------------------------------------------------------
# Cross-surface agreement + id minting
# ---------------------------------------------------------------------------


def test_accepted_scan_types_match_the_chat_composer() -> None:
    """Drift pin for an INTENTIONALLY shared value held on two surfaces.

    The constants are deliberately separate (transport must not import web),
    but an image the chat composer accepts and the batch door refuses would be
    an arbitrary difference to an operator. If either side moves, this fails
    and the change becomes a decision rather than an accident.
    """
    from alfred.web.routes_chat import ALLOWED_IMAGE_MEDIA_TYPES

    assert ALLOWED_SCAN_MEDIA_TYPES == ALLOWED_IMAGE_MEDIA_TYPES


def test_a_minted_batch_id_is_a_safe_path_segment() -> None:
    """The id becomes a directory name, so the minter must satisfy the validator.

    Asserted against the REAL validator rather than a copy of its regex: a
    minter that drifted from the rule is a directory traversal waiting to be
    written, and two copies of a regex is how that drift happens.
    """
    from alfred.batch.paths import validate_batch_id

    for _ in range(50):
        batch_id = mint_batch_id()
        assert validate_batch_id(batch_id) == batch_id


def test_minted_batch_ids_do_not_collide() -> None:
    """Two submissions in the same second must not share a directory."""
    assert len({mint_batch_id() for _ in range(200)}) == 200
