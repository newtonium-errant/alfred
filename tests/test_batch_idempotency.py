"""#100 — wire-level idempotency on ``POST /vault/batch``.

THE FAILURE UNDER TEST: a submit lands, the box saves the scans and answers, and
the ANSWER is lost. The operator presses Submit again. Without a key on the wire
that second request mints a SECOND batch — two records, twice the drip items,
the vision spend paid twice for one stack of paper.

Every route pin here drives the REAL aiohttp app over a REAL socket with REAL
multipart bodies, for the same reason ``test_batch_route.py`` does: the key
rides an HTTP header through a route whose body is deliberately never parsed by
anything above it, and a directly-called handler would prove nothing about that.

EVERY DEDUPE ASSERTION CARRIES A POSITIVE CONTROL. "Same key ⇒ one batch" is
vacuous on its own — it passes identically against a build where submission is
broken and NO batch is ever created. So each one is paired with the nearest
admissible neighbour (a different key, a malformed key, no key at all) that must
still mint, and the batch directories on disk are counted rather than inferred
from the response.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog
from aiohttp import web

from alfred.batch.paths import batch_root
from alfred.batch.submit_keys import (
    MAX_SUBMIT_KEYS,
    SUBMIT_KEY_TTL_SECONDS,
    SubmitKeyEntry,
    clean_key,
    find_receipt,
    load_entries,
    record_receipt,
    store_path,
)
from alfred.transport.routes_batch import (
    BATCH_IDEMPOTENCY_HEADER,
    BATCH_PEER_NAME,
    register_batch_routes,
)

_INSTANCE = "Salem"
_KEY = "11111111-2222-4333-8444-555555555555"
_OTHER_KEY = "99999999-8888-4777-8666-555555555555"


def _scan(seed: int, size: int = 64) -> bytes:
    return b"\xff\xd8\xff" + bytes([seed % 256]) * (size - 3)


def _saved_batches(data_dir: Path) -> list[str]:
    """Batch directories on disk — the ground truth for "was a batch minted?".

    Counted rather than inferred from the response body: a route that answered
    with a cached receipt while ALSO creating a directory would pass a
    response-only assertion and still double-bill the drip.
    """
    root = batch_root(data_dir, _INSTANCE)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


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


def _make_app(*, vault: Path, data_dir: Path, peer: str = BATCH_PEER_NAME) -> web.Application:
    @web.middleware
    async def _stub_auth(request, handler):
        request["transport_peer"] = peer
        return await handler(request)

    app = web.Application(middlewares=[_stub_auth])
    from alfred.transport.peer_handlers import register_vault_path

    register_vault_path(app, vault)
    register_batch_routes(
        app, enabled=True, instance_name=_INSTANCE, data_dir=str(data_dir),
    )
    return app


def _form(images: list[tuple[str, bytes, str]], *, instruction: str | None = "Read the total."):
    from aiohttp import FormData

    form = FormData()
    if instruction is not None:
        form.add_field("instruction", instruction, content_type="text/plain")
    for name, payload, media_type in images:
        form.add_field("images", payload, filename=name, content_type=media_type)
    return form


def _headers(key: str | None) -> dict[str, str]:
    return {BATCH_IDEMPOTENCY_HEADER: key} if key is not None else {}


# ---------------------------------------------------------------------------
# The central pin: a retry must not mint a second batch
# ---------------------------------------------------------------------------


async def test_same_key_replays_the_receipt_and_mints_no_second_batch(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """THE #100 pin. The retry gets the ORIGINAL answer; disk gains nothing.

    The response equality matters as much as the directory count: an operator
    whose first response was lost needs the real batch id and record path back,
    not an acknowledgement that something once happened.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))

    first = await client.post(
        "/vault/batch",
        data=_form([("a.jpg", _scan(1), "image/jpeg")]),
        headers=_headers(_KEY),
    )
    assert first.status == 200
    first_body = await first.json()
    assert first_body.get("deduped") is not True, "the FIRST submit is not a replay"
    after_first = _saved_batches(data_dir)
    assert len(after_first) == 1

    # The retry: same key, same staged set, as the client would resend it.
    second = await client.post(
        "/vault/batch",
        data=_form([("a.jpg", _scan(1), "image/jpeg")]),
        headers=_headers(_KEY),
    )
    assert second.status == 200
    second_body = await second.json()

    assert second_body["deduped"] is True
    assert second_body["batch_id"] == first_body["batch_id"]
    assert second_body["path"] == first_body["path"]
    assert second_body["images"] == first_body["images"]
    assert _saved_batches(data_dir) == after_first, (
        "the retry created a second batch directory — the key did not dedupe"
    )


async def test_positive_control_a_different_key_does_mint_a_second_batch(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """The control for the pin above.

    Without this, "same key ⇒ one directory" would pass just as well against a
    build where the route never creates anything at all.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))

    first = await client.post(
        "/vault/batch",
        data=_form([("a.jpg", _scan(1), "image/jpeg")]),
        headers=_headers(_KEY),
    )
    assert first.status == 200
    second = await client.post(
        "/vault/batch",
        data=_form([("a.jpg", _scan(1), "image/jpeg")]),
        headers=_headers(_OTHER_KEY),
    )
    assert second.status == 200
    second_body = await second.json()

    assert second_body.get("deduped") is not True
    assert second_body["batch_id"] != (await first.json())["batch_id"]
    assert len(_saved_batches(data_dir)) == 2, (
        "a genuinely new submission was swallowed by the dedupe gate"
    )


async def test_no_key_still_mints_every_time(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """The pre-#100 client shape: no header ⇒ no dedupe, and nothing breaks.

    Also the second positive control — it proves the route mints on a repeat
    submission whenever the key is what is missing, which is what makes the
    dedupe pin's single directory attributable to the KEY.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    for _ in range(2):
        resp = await client.post(
            "/vault/batch", data=_form([("a.jpg", _scan(1), "image/jpeg")]),
        )
        assert resp.status == 200
        assert (await resp.json()).get("deduped") is not True
    assert len(_saved_batches(data_dir)) == 2


async def test_key_survives_a_restart(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """The store is on DISK, not in the process.

    A second app over the same data dir is a restart in every way that matters
    here: no Python state carries over, so a dedupe that still holds can only
    have come from the file. This is the pin that fails if the seen-store is
    ever "optimised" into a module-level dict.
    """
    first_client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    first = await first_client.post(
        "/vault/batch",
        data=_form([("a.jpg", _scan(1), "image/jpeg")]),
        headers=_headers(_KEY),
    )
    assert first.status == 200
    first_body = await first.json()
    assert store_path(data_dir, _INSTANCE).is_file(), "no seen-store was written"

    # A wholly separate app instance — the restart.
    second_client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))
    second = await second_client.post(
        "/vault/batch",
        data=_form([("a.jpg", _scan(1), "image/jpeg")]),
        headers=_headers(_KEY),
    )
    assert second.status == 200
    second_body = await second.json()

    assert second_body["deduped"] is True
    assert second_body["batch_id"] == first_body["batch_id"]
    assert len(_saved_batches(data_dir)) == 1


async def test_a_refused_submission_does_not_burn_the_key(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """Only a submission that CREATED a batch is worth replaying.

    A refusal (here: no instruction) must leave the key unused, or an operator
    who fixes the problem and resubmits would be handed a receipt for a batch
    that was never made — told their scans were filed when nothing exists.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))

    refused = await client.post(
        "/vault/batch",
        data=_form([("a.jpg", _scan(1), "image/jpeg")], instruction=None),
        headers=_headers(_KEY),
    )
    assert refused.status == 400
    assert (await refused.json())["error"] == "empty_instruction"
    assert _saved_batches(data_dir) == []
    assert find_receipt(store_path(data_dir, _INSTANCE), _KEY) is None

    # The correction, same key — must really run.
    fixed = await client.post(
        "/vault/batch",
        data=_form([("a.jpg", _scan(1), "image/jpeg")], instruction="Read the total."),
        headers=_headers(_KEY),
    )
    assert fixed.status == 200
    fixed_body = await fixed.json()
    assert fixed_body.get("deduped") is not True
    assert len(_saved_batches(data_dir)) == 1


async def test_a_malformed_key_is_ignored_not_relied_on(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """A key the route will not accept must leave the submission WORKING.

    Fail-open is right here and only here: the key is a double-submit guard, not
    an authorisation. Refusing a malformed one would turn a client bug into lost
    scans. The positive control is the well-formed key below, which does dedupe
    against the same route in the same test.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))

    for _ in range(2):
        resp = await client.post(
            "/vault/batch",
            data=_form([("a.jpg", _scan(1), "image/jpeg")]),
            headers=_headers("has spaces and: punctuation"),
        )
        assert resp.status == 200
        assert (await resp.json()).get("deduped") is not True
    assert len(_saved_batches(data_dir)) == 2, "a malformed key must not dedupe"

    # POSITIVE CONTROL, same route, same test: a well-formed key DOES dedupe.
    for _ in range(2):
        ok = await client.post(
            "/vault/batch",
            data=_form([("a.jpg", _scan(1), "image/jpeg")]),
            headers=_headers(_KEY),
        )
        assert ok.status == 200
    assert len(_saved_batches(data_dir)) == 3, (
        "the well-formed key did not dedupe — the malformed-key result above "
        "proves nothing without this"
    )


async def test_dedupe_and_missing_key_are_both_logged(
    aiohttp_client, vault: Path, data_dir: Path,
) -> None:
    """Observability pin — the operator greps for both of these.

    ``transport.batch.deduped`` is how "the retry was absorbed" is
    distinguishable from "the retry silently vanished", and
    ``no_idempotency_key`` is the ILB signal that a client is submitting with no
    protection at all. A refactor that drops either leaves both states looking
    identical in the log.
    """
    client = await aiohttp_client(_make_app(vault=vault, data_dir=data_dir))

    with structlog.testing.capture_logs() as captured:
        await client.post("/vault/batch", data=_form([("a.jpg", _scan(1), "image/jpeg")]))
        await client.post(
            "/vault/batch",
            data=_form([("b.jpg", _scan(2), "image/jpeg")]),
            headers=_headers(_KEY),
        )
        await client.post(
            "/vault/batch",
            data=_form([("b.jpg", _scan(2), "image/jpeg")]),
            headers=_headers(_KEY),
        )

    missing = [c for c in captured if c.get("event") == "transport.batch.no_idempotency_key"]
    assert len(missing) == 1
    assert missing[0]["header"] == BATCH_IDEMPOTENCY_HEADER

    deduped = [c for c in captured if c.get("event") == "transport.batch.deduped"]
    assert len(deduped) == 1
    assert deduped[0]["idempotency_key_prefix"] == _KEY[:8]
    # The WHOLE key must never reach the log — only its prefix, as the chat
    # turn's own dedupe log does.
    assert _KEY not in json.dumps(deduped[0])


# ---------------------------------------------------------------------------
# The store itself
# ---------------------------------------------------------------------------


def test_store_is_instance_scoped(data_dir: Path) -> None:
    """Two instances on one box must not share a key namespace.

    The #53 shape: every instance on the box shares a WorkingDirectory and
    differs only by ``--config``, so an unscoped store would let KAL-LE's key
    replay a receipt for Salem's batch.
    """
    salem = store_path(data_dir, "Salem")
    kalle = store_path(data_dir, "KAL-LE")
    assert salem != kalle
    record_receipt(salem, _KEY, {"batch_id": "salem-1"})
    assert find_receipt(kalle, _KEY) is None
    assert find_receipt(salem, _KEY) == {"batch_id": "salem-1"}


def test_load_is_schema_tolerant(data_dir: Path) -> None:
    """The load-time schema-tolerance contract, both directions.

    A store written by a NEWER build (an extra field) and one written by an
    OLDER build (a missing field) must both load. Without the filter, a rollback
    or a roll-forward turns a bookkeeping file into a crash on every submission.
    """
    path = store_path(data_dir, _INSTANCE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 99,
        "entries": [
            {  # a FUTURE build's row
                "key": _KEY,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "receipt": {"batch_id": "b1"},
                "unknown_future_field": {"nested": True},
            },
            {  # an OLDER build's row: no `receipt` at all
                "key": _OTHER_KEY,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            {"created_at": "…"},   # keyless — unusable, dropped
            "not-a-dict",          # junk — dropped
        ],
    }), encoding="utf-8")

    entries = load_entries(path)
    assert [e.key for e in entries] == [_KEY, _OTHER_KEY]
    assert entries[0].receipt == {"batch_id": "b1"}
    assert entries[1].receipt == {}


def test_a_corrupt_store_reads_empty_rather_than_raising(data_dir: Path) -> None:
    """A truncated bookkeeping file must not take the submission route down."""
    path = store_path(data_dir, _INSTANCE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"entries": [{"key": "a"', encoding="utf-8")
    assert load_entries(path) == []
    assert find_receipt(path, _KEY) is None


def test_entries_expire_on_the_named_window(data_dir: Path) -> None:
    """The TTL is real, and it is the constant the module names."""
    path = store_path(data_dir, _INSTANCE)
    now = datetime.now(timezone.utc)
    record_receipt(path, _KEY, {"batch_id": "old"}, now=now)

    just_inside = now + timedelta(seconds=SUBMIT_KEY_TTL_SECONDS - 60)
    assert find_receipt(path, _KEY, now=just_inside) == {"batch_id": "old"}

    past_it = now + timedelta(seconds=SUBMIT_KEY_TTL_SECONDS + 60)
    assert find_receipt(path, _KEY, now=past_it) is None


def test_an_unparseable_timestamp_expires_rather_than_sticking(data_dir: Path) -> None:
    """A corrupt row must not hold a slot forever.

    Both costs are small and bounded: dropping it costs one un-deduped retry,
    keeping it would cost a slot held indefinitely. It could NOT cost a wrong
    answer — a replay needs an exact match on a client-minted UUID, so a stale
    row can only ever replay the receipt it was written for.
    """
    path = store_path(data_dir, _INSTANCE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "entries": [{"key": _KEY, "created_at": "whenever", "receipt": {"batch_id": "x"}}],
    }), encoding="utf-8")
    assert load_entries(path) == []


def test_the_store_is_bounded_and_keeps_the_newest(data_dir: Path) -> None:
    """The count cap, which the TTL alone does not provide on a busy day."""
    path = store_path(data_dir, _INSTANCE)
    now = datetime.now(timezone.utc)
    for i in range(MAX_SUBMIT_KEYS + 25):
        record_receipt(path, f"key-{i:05d}", {"batch_id": f"b{i}"}, now=now)

    entries = load_entries(path, now=now)
    assert len(entries) == MAX_SUBMIT_KEYS
    assert entries[-1].key == f"key-{MAX_SUBMIT_KEYS + 24:05d}", "newest must be kept"
    assert find_receipt(path, "key-00000", now=now) is None, "oldest must be evicted"


def test_rewriting_a_key_does_not_duplicate_it(data_dir: Path) -> None:
    """One row per key — the last answer wins rather than accumulating."""
    path = store_path(data_dir, _INSTANCE)
    record_receipt(path, _KEY, {"batch_id": "first"})
    record_receipt(path, _KEY, {"batch_id": "second"})
    entries = load_entries(path)
    assert len(entries) == 1
    assert entries[0].receipt == {"batch_id": "second"}


def test_a_write_failure_is_swallowed_not_raised(data_dir: Path, monkeypatch) -> None:
    """The batch is already saved and answered by the time this runs.

    Turning a bookkeeping failure into an exception would surface as a 502 and
    tell the operator their scans were lost when they are on disk.
    """
    path = store_path(data_dir, _INSTANCE)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)
    record_receipt(path, _KEY, {"batch_id": "b1"})  # must not raise


@pytest.mark.parametrize("raw", ["", None, "   ", "has space", "new\nline", "a" * 201, "semi;colon"])
def test_clean_key_rejects_unusable_keys(raw) -> None:
    assert clean_key(raw) == ""


@pytest.mark.parametrize("raw", [_KEY, "batch-abc-def", "A_b-1", "x"])
def test_clean_key_accepts_well_formed_keys(raw) -> None:
    """The positive control for the rejection table above."""
    assert clean_key(raw) == raw


def test_no_key_never_touches_disk(data_dir: Path) -> None:
    """The pre-feature path must not pay a file read or write."""
    path = store_path(data_dir, _INSTANCE)
    assert find_receipt(path, "") is None
    record_receipt(path, "", {"batch_id": "b1"})
    assert not path.exists()


def test_entry_from_dict_coerces_a_non_dict_receipt() -> None:
    """A hand-edited store must not put a list into a JSON response body."""
    entry = SubmitKeyEntry.from_dict({"key": "k", "created_at": "t", "receipt": ["nope"]})
    assert entry.receipt == {}
