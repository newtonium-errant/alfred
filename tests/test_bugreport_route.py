"""#95 — ``POST /vault/bugreport``, the in-app bug reporter's save-only route.

These drive a REAL aiohttp app over a REAL socket with REAL JSON bodies. The
refusal pins assert WHY the route refused — the wire code AND the logged reason
— never merely that a non-2xx came back. A refusal for an unrelated cause
renders identically to the guard firing, so a pin that only checks the status is
green against a build with NO guard at all.

The load-bearing pin in this file is not any of the caps: it is
``test_screenshot_is_invisible_to_the_curator``. Filing a picture where the
curator can see it is exactly the #88 defect — 39 chat screenshots pushed
through ``claude -p`` a second time — and the cost of being wrong is paid in
vision-model billing, which is a bad way to find out. That test asserts the
property (the curator's own admission predicate refuses everything this route
writes except the one markdown file) rather than the implementation, so it keeps
holding if the directory is ever renamed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import structlog
from aiohttp import web

from alfred.curator.config import WatcherConfig
from alfred.curator.watcher import is_ingestable
from alfred.transport.config import (
    DEFAULT_BUGREPORT_MAX_DESCRIPTION_CHARS,
    DEFAULT_BUGREPORT_MAX_SCREENSHOT_BYTES,
    BugReportConfig,
    load_from_unified,
)
from alfred.transport.routes_bugreport import (
    ALLOWED_SHOT_MEDIA_TYPES,
    ATTACHMENT_DIRNAME,
    BUGREPORT_PEER_NAME,
    mint_report_id,
    register_bugreport_routes,
)

_INSTANCE = "Salem"

# A 1x1 PNG. The route never decodes an image, so the only thing that matters
# is that these are real bytes with a real length.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "inbox").mkdir(parents=True)
    return v


def _make_app(
    *,
    vault: Path,
    peer: str = BUGREPORT_PEER_NAME,
    notify_sink=None,
    **caps,
) -> web.Application:
    """The bugreport route on an app whose middleware stubs peer resolution.

    ``peer`` stands in for what ``auth_middleware`` would set from the matched
    ``auth.tokens`` key. It defaults to the PRODUCTION peer name — not merely a
    name that would clear ``allowed_clients`` — because the escalation this
    route's pin exists to stop is a DIFFERENT valid token carrying the same
    client name.
    """
    @web.middleware
    async def _stub_auth(request, handler):
        request["transport_peer"] = peer
        return await handler(request)

    app = web.Application(middlewares=[_stub_auth])
    from alfred.transport.peer_handlers import (
        _KEY_WEB_NOTIFY_SINK,
        register_vault_path,
    )

    register_vault_path(app, vault)
    if notify_sink is not None:
        app[_KEY_WEB_NOTIFY_SINK] = notify_sink
    register_bugreport_routes(
        app, enabled=True, instance_name=_INSTANCE, **caps,
    )
    return app


def _payload(**over) -> dict:
    body = {
        "description": "The Send button did nothing after I attached a photo.",
        "context": {
            "route": "/chat",
            "instance": _INSTANCE,
            "user_agent": "Mozilla/5.0 (iPhone)",
            "viewport_w": 390,
            "viewport_h": 844,
            "app_version": "abc1234",
            "ts": "2026-08-11T12:00:00+00:00",
        },
    }
    body.update(over)
    return body


def _reports(vault: Path) -> list[Path]:
    return sorted((vault / "inbox").glob("bugreport-*.md"))


def _attachments(vault: Path) -> list[Path]:
    d = vault / "inbox" / ATTACHMENT_DIRNAME
    return sorted(p for p in d.iterdir() if p.is_file()) if d.is_dir() else []


# ---------------------------------------------------------------------------
# The peer pin — the escalation guard
# ---------------------------------------------------------------------------


def test_peer_name_is_the_production_key() -> None:
    """The pin is worthless if it names a peer production never issues.

    Pinned as a literal so renaming the token becomes a deliberate, reviewed
    change here AND in the operator's ``auth.tokens`` block, rather than a
    silently-passing rename that leaves the live door open under the old name.
    """
    assert BUGREPORT_PEER_NAME == "web_bugreport"


@pytest.mark.parametrize("peer", ["web", "web_ingest", "web_batch", "web_feed", ""])
async def test_wrong_peer_is_refused_with_its_reason(aiohttp_client, vault, peer) -> None:
    """A valid-but-different peer token is refused 401 — and says why.

    ``web`` is the case that matters: the full-chat token and this one both
    legitimately carry ``allowed_clients: [web]``, so Layer 1 cannot tell them
    apart and only this pin stops a chat token writing into the vault.

    The logged ``reason`` is asserted because it is the ONLY thing that
    distinguishes this guard firing from a refusal for some unrelated cause —
    both produce a 401 and both leave the vault untouched.
    """
    client = await aiohttp_client(_make_app(vault=vault, peer=peer))
    with structlog.testing.capture_logs() as captured:
        res = await client.post("/vault/bugreport", json=_payload())

    assert res.status == 401
    assert (await res.json())["error"] == "wrong_peer"

    denials = [
        c for c in captured
        if c.get("event") == "transport.bugreport.rejected"
        and c.get("reason") == "wrong_peer"
    ]
    assert len(denials) == 1
    assert denials[0]["expected"] == BUGREPORT_PEER_NAME
    assert denials[0]["peer"] == (peer or "(none)")

    # Not merely "no record written" — nothing was TOUCHED. A refused write
    # that still created a directory out there is a guard that leaks.
    assert _reports(vault) == []
    assert not (vault / "inbox" / ATTACHMENT_DIRNAME).exists()


# ---------------------------------------------------------------------------
# The happy path + record shape
# ---------------------------------------------------------------------------


async def test_files_a_report_with_a_screenshot(aiohttp_client, vault) -> None:
    """The whole contract: markdown in inbox/, picture beside it, honest answer."""
    client = await aiohttp_client(_make_app(vault=vault))
    res = await client.post("/vault/bugreport", json=_payload(
        screenshot_b64=base64.b64encode(_PNG).decode(),
        screenshot_media_type="image/png",
    ))

    assert res.status == 200
    body = await res.json()
    assert body["status"] == "filed"
    assert body["instance"] == _INSTANCE
    assert body["screenshot"] is True
    assert body["path"].startswith("inbox/bugreport-")

    reports = _reports(vault)
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")

    # Frontmatter provenance — each field pinned by name, so a rename or a drop
    # fails here rather than silently degrading what the curator receives.
    assert text.startswith("---\n")
    assert "source: bug-report" in text
    assert f"bugreport_id: '{body['report_id']}'" in text
    assert "route: '/chat'" in text
    assert "app_version: 'abc1234'" in text
    assert "viewport: '390x844'" in text
    assert f"instance: '{_INSTANCE}'" in text
    # The description is in the BODY, never in frontmatter: prose in a YAML
    # scalar is the most likely thing to break the parse the curator needs.
    assert "The Send button did nothing after I attached a photo." in text
    assert text.index("---\n", 4) < text.index("The Send button did nothing")

    shots = _attachments(vault)
    assert len(shots) == 1
    assert shots[0].read_bytes() == _PNG
    assert shots[0].name == f"{body['report_id']}.png"
    # The markdown must POINT at the picture, or the picture may as well not
    # exist — nothing downstream would ever find it.
    assert f"inbox/{ATTACHMENT_DIRNAME}/{shots[0].name}" in text


async def test_files_a_report_without_a_screenshot(aiohttp_client, vault) -> None:
    """No picture is a real outcome, and the record says so in words.

    ILB: "no screenshot was attached" must be distinguishable from a screenshot
    that went missing between the browser and the disk.
    """
    client = await aiohttp_client(_make_app(vault=vault))
    res = await client.post("/vault/bugreport", json=_payload())

    assert res.status == 200
    assert (await res.json())["screenshot"] is False
    text = _reports(vault)[0].read_text(encoding="utf-8")
    assert "No screenshot was attached to this report." in text
    assert "screenshot:" not in text.split("---")[1]  # absent from frontmatter
    # The hidden directory is not created for a report that has no attachment.
    assert not (vault / "inbox" / ATTACHMENT_DIRNAME).exists()


# ---------------------------------------------------------------------------
# The #88 guard — the reason the attachment is where it is
# ---------------------------------------------------------------------------


def test_screenshot_is_invisible_to_the_curator(vault: Path) -> None:
    """The curator's OWN predicate must refuse everything but the markdown.

    Asserted against ``is_ingestable`` and the default extension list rather
    than against a hardcoded ".png is not .md": this is the exact predicate
    ``watcher.full_scan`` runs, so if the admission rule is ever widened to
    include images, THIS fails and the placement gets rethought — which is the
    moment the #88 defect would otherwise silently return.
    """
    extensions = WatcherConfig().ingestable_extensions
    assert ".md" in extensions, "fixture assumes markdown is admitted"

    attach_dir = vault / "inbox" / ATTACHMENT_DIRNAME
    attach_dir.mkdir(parents=True)
    (attach_dir / "20260811-120000-aabbcc.png").write_bytes(_PNG)
    (vault / "inbox" / "bugreport-20260811-120000-aabbcc.md").write_text("x")

    # Guard 1: the directory is hidden, so full_scan's name filter skips it —
    # and it is not a file, so the is_file() filter skips it too.
    assert ATTACHMENT_DIRNAME.startswith(".")
    assert not attach_dir.is_file()

    # Guard 2: even placed directly in inbox/, the image is not ingestable.
    assert not is_ingestable(attach_dir / "20260811-120000-aabbcc.png", extensions)
    for ext in sorted({f".{e}" for e in ("png", "jpg", "webp")}):
        assert not is_ingestable(Path(f"shot{ext}"), extensions)

    # And the report itself IS admitted — the guard must not be so broad that
    # it hides the thing the curator is supposed to read.
    assert is_ingestable(
        vault / "inbox" / "bugreport-20260811-120000-aabbcc.md", extensions,
    )


# ---------------------------------------------------------------------------
# Refusals — each with its own code, because each needs a different action
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("description", ["", "   ", "\n\t "])
async def test_empty_description_is_refused(aiohttp_client, vault, description) -> None:
    """A report with no words is refused honestly, not filed as a blank record."""
    client = await aiohttp_client(_make_app(vault=vault))
    with structlog.testing.capture_logs() as captured:
        res = await client.post("/vault/bugreport", json=_payload(description=description))

    assert res.status == 400
    assert (await res.json())["error"] == "empty_description"
    assert [
        c for c in captured
        if c.get("event") == "transport.bugreport.rejected"
        and c.get("reason") == "empty_description"
    ]
    assert _reports(vault) == []


async def test_description_over_cap_is_refused_with_the_limit(aiohttp_client, vault) -> None:
    """413 naming ``max_chars`` — the reporter must be told the number."""
    client = await aiohttp_client(_make_app(vault=vault, max_description_chars=50))
    res = await client.post("/vault/bugreport", json=_payload(description="x" * 51))

    assert res.status == 413
    body = await res.json()
    assert body["error"] == "description_too_long"
    assert body["max_chars"] == 50
    assert _reports(vault) == []


async def test_screenshot_over_cap_is_refused_before_it_is_decoded(
    aiohttp_client, vault,
) -> None:
    """The base64 LENGTH is checked first, so an oversize shot is never decoded.

    ``screenshot_too_large`` is a DIFFERENT code from ``description_too_long``
    because the two need different actions — shrink the picture vs shorten the
    words — and one code for both tells the reporter neither.
    """
    client = await aiohttp_client(_make_app(vault=vault, max_screenshot_bytes=64))
    res = await client.post("/vault/bugreport", json=_payload(
        screenshot_b64=base64.b64encode(b"\x00" * 4096).decode(),
        screenshot_media_type="image/png",
    ))

    assert res.status == 413
    body = await res.json()
    assert body["error"] == "screenshot_too_large"
    assert body["max_bytes"] == 64
    # The words were not filed either — a partial report would be worse than a
    # refusal, because the reporter would never know the picture was dropped.
    assert _reports(vault) == []
    assert not (vault / "inbox" / ATTACHMENT_DIRNAME).exists()


async def test_unsupported_media_type_is_refused_by_name(aiohttp_client, vault) -> None:
    """A type outside the allowlist is named, and the allowed set is returned."""
    client = await aiohttp_client(_make_app(vault=vault))
    res = await client.post("/vault/bugreport", json=_payload(
        screenshot_b64=base64.b64encode(_PNG).decode(),
        screenshot_media_type="image/heic",
    ))

    assert res.status == 415
    body = await res.json()
    assert body["error"] == "unsupported_media_type"
    assert body["media_type"] == "image/heic"
    assert sorted(ALLOWED_SHOT_MEDIA_TYPES) == body["allowed"]
    assert _reports(vault) == []


async def test_invalid_base64_is_refused(aiohttp_client, vault) -> None:
    """A truncated upload must not decode into a plausible shorter image."""
    client = await aiohttp_client(_make_app(vault=vault))
    res = await client.post("/vault/bugreport", json=_payload(
        screenshot_b64="!!!! not base64 !!!!",
        screenshot_media_type="image/png",
    ))

    assert res.status == 400
    assert (await res.json())["error"] == "invalid_base64"
    assert _reports(vault) == []


async def test_malformed_json_is_refused(aiohttp_client, vault) -> None:
    client = await aiohttp_client(_make_app(vault=vault))
    res = await client.post(
        "/vault/bugreport",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert res.status == 400
    assert (await res.json())["error"] == "invalid_json"


async def test_no_vault_is_refused_503(aiohttp_client, tmp_path) -> None:
    """A vault-less instance says so rather than writing somewhere arbitrary."""
    @web.middleware
    async def _stub_auth(request, handler):
        request["transport_peer"] = BUGREPORT_PEER_NAME
        return await handler(request)

    app = web.Application(middlewares=[_stub_auth])
    register_bugreport_routes(app, enabled=True, instance_name=_INSTANCE)
    client = await aiohttp_client(app)
    res = await client.post("/vault/bugreport", json=_payload())
    assert res.status == 503
    assert (await res.json())["error"] == "vault_not_configured"


# ---------------------------------------------------------------------------
# Frontmatter safety — untrusted strings become YAML
# ---------------------------------------------------------------------------


async def test_control_characters_cannot_break_the_frontmatter(
    aiohttp_client, vault,
) -> None:
    """A newline in a context field must not end the YAML value.

    This is the failure that would present as "the curator ignores my bug
    reports" rather than as anything about a user-agent string, which is why it
    is pinned rather than trusted to the sanitiser's docstring.
    """
    client = await aiohttp_client(_make_app(vault=vault))
    ctx = _payload()["context"]
    ctx["route"] = "/chat\ninjected: true\nx: "
    ctx["user_agent"] = "Mozilla'5.0\r\nevil: yes"
    res = await client.post("/vault/bugreport", json=_payload(context=ctx))
    assert res.status == 200

    text = _reports(vault)[0].read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert "\ninjected:" not in front
    assert "\nevil:" not in front

    import yaml  # frontmatter's parser — the real consumer of this block

    parsed = yaml.safe_load(front)
    assert parsed["source"] == "bug-report"
    assert "injected" not in parsed
    assert "evil" not in parsed
    # The single quote survived as CONTENT, not as a delimiter.
    assert "Mozilla'5.0" in parsed["user_agent"]


# ---------------------------------------------------------------------------
# The notification surface
# ---------------------------------------------------------------------------


async def test_notifies_and_says_so(aiohttp_client, vault) -> None:
    seen: list[dict] = []

    def _sink(*, payload, from_peer="", correlation_id=""):
        seen.append({"payload": payload, "from_peer": from_peer, "cid": correlation_id})

    client = await aiohttp_client(_make_app(vault=vault, notify_sink=_sink))
    res = await client.post("/vault/bugreport", json=_payload())

    assert res.status == 200
    body = await res.json()
    assert body["notified"] is True
    assert len(seen) == 1
    assert seen[0]["from_peer"] == BUGREPORT_PEER_NAME
    assert seen[0]["cid"] == body["report_id"]
    assert "Bug report filed" in seen[0]["payload"]["text"]
    assert seen[0]["payload"]["source"] == "bug-report"


async def test_missing_notify_sink_is_reported_not_hidden(aiohttp_client, vault) -> None:
    """No tray on this instance → ``notified: false`` and an explicit skip log.

    The report is still filed. Telling the operator it is visible in-app when
    this instance has no notification surface would be a lie they only discover
    by going to look.
    """
    client = await aiohttp_client(_make_app(vault=vault, notify_sink=None))
    with structlog.testing.capture_logs() as captured:
        res = await client.post("/vault/bugreport", json=_payload())

    assert res.status == 200
    assert (await res.json())["notified"] is False
    skips = [
        c for c in captured
        if c.get("event") == "transport.bugreport.notify_skipped"
    ]
    assert len(skips) == 1
    assert "no web notify sink" in skips[0]["reason"]
    assert len(_reports(vault)) == 1  # filed regardless


async def test_a_throwing_sink_does_not_lose_the_report(aiohttp_client, vault) -> None:
    """The report is durable BEFORE the notification runs.

    Answering 502 for a tray failure would invite a retry that files a SECOND
    copy of the same bug — the report is already on disk by then.
    """
    def _boom(**_kw):
        raise RuntimeError("tray is on fire")

    client = await aiohttp_client(_make_app(vault=vault, notify_sink=_boom))
    with structlog.testing.capture_logs() as captured:
        res = await client.post("/vault/bugreport", json=_payload())

    assert res.status == 200
    assert (await res.json())["notified"] is False
    assert len(_reports(vault)) == 1
    fails = [
        c for c in captured if c.get("event") == "transport.bugreport.notify_failed"
    ]
    assert len(fails) == 1
    assert fails[0]["error_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Observability + opt-in inertness
# ---------------------------------------------------------------------------


async def test_filed_event_carries_the_operator_fields(aiohttp_client, vault) -> None:
    """The success log is the operator's grep surface — pin its fields.

    Asserted per-field rather than "an event fired": a refactor that drops
    ``path`` or renames ``report_id`` breaks the workflow while leaving the
    event name intact, and only a field-level pin catches that.
    """
    client = await aiohttp_client(_make_app(vault=vault))
    with structlog.testing.capture_logs() as captured:
        res = await client.post(
            "/vault/bugreport",
            json=_payload(
                screenshot_b64=base64.b64encode(_PNG).decode(),
                screenshot_media_type="image/png",
            ),
            headers={"X-Alfred-BugReport-User": "Andrew"},
        )
    assert res.status == 200

    filed = [c for c in captured if c.get("event") == "transport.bugreport.filed"]
    assert len(filed) == 1
    ev = filed[0]
    assert ev["peer"] == BUGREPORT_PEER_NAME
    assert ev["user"] == "Andrew"
    assert ev["instance"] == _INSTANCE
    assert ev["route"] == "/chat"
    assert ev["screenshot_bytes"] == len(_PNG)
    assert ev["report_id"] == (await res.json())["report_id"]


def test_disabled_mounts_nothing_and_says_so() -> None:
    """Opt-in inertness: an un-opted-in instance's transport is byte-unchanged."""
    app = web.Application()
    with structlog.testing.capture_logs() as captured:
        mounted = register_bugreport_routes(
            app, enabled=False, instance_name=_INSTANCE,
        )
    assert mounted is False
    assert len(list(app.router.routes())) == 0
    disabled = [
        c for c in captured if c.get("event") == "transport.bugreport.disabled"
    ]
    assert len(disabled) == 1


def test_enabled_mounts_exactly_one_route() -> None:
    app = web.Application()
    assert register_bugreport_routes(
        app, enabled=True, instance_name=_INSTANCE,
    ) is True
    paths = [r.resource.canonical for r in app.router.routes()]
    assert paths == ["/vault/bugreport"]


async def test_header_reporter_wins_over_the_body_field(aiohttp_client, vault) -> None:
    """Provenance comes from the BFF's verified header, not the client's claim."""
    client = await aiohttp_client(_make_app(vault=vault))
    res = await client.post(
        "/vault/bugreport",
        json=_payload(reported_by="Impostor"),
        headers={"X-Alfred-BugReport-User": "Andrew"},
    )
    assert res.status == 200
    text = _reports(vault)[0].read_text(encoding="utf-8")
    assert "reported_by: 'Andrew'" in text
    assert "Impostor" not in text


async def test_report_ids_do_not_collide(aiohttp_client, vault) -> None:
    """Two reports in the same second must be two files, not one overwritten."""
    client = await aiohttp_client(_make_app(vault=vault))
    ids = set()
    for _ in range(5):
        res = await client.post("/vault/bugreport", json=_payload())
        assert res.status == 200
        ids.add((await res.json())["report_id"])
    assert len(ids) == 5
    assert len(_reports(vault)) == 5


def test_minted_ids_are_path_safe() -> None:
    """The id becomes a path segment; a minter that drifted is a traversal."""
    import re

    for _ in range(50):
        rid = mint_report_id()
        assert re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", rid), rid


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_defaults_to_disabled() -> None:
    assert BugReportConfig().enabled is False
    cfg = load_from_unified({"transport": {}})
    assert cfg.bugreport.enabled is False


def test_config_caps_are_floored_at_one() -> None:
    """A zero cap would wedge the route into refusing every report.

    That misconfiguration presents to an operator as "the bug button is
    broken", which is the hardest kind to diagnose because the surface it
    breaks is the one you would use to report it.
    """
    cfg = load_from_unified({"transport": {"bugreport": {
        "enabled": True,
        "max_description_chars": 0,
        "max_screenshot_bytes": -5,
    }}})
    assert cfg.bugreport.max_description_chars == 1
    assert cfg.bugreport.max_screenshot_bytes == 1


def test_config_garbage_falls_back_to_the_defaults() -> None:
    cfg = load_from_unified({"transport": {"bugreport": {
        "enabled": True,
        "max_description_chars": "not a number",
        "max_screenshot_bytes": None,
    }}})
    assert cfg.bugreport.max_description_chars == DEFAULT_BUGREPORT_MAX_DESCRIPTION_CHARS
    assert cfg.bugreport.max_screenshot_bytes == DEFAULT_BUGREPORT_MAX_SCREENSHOT_BYTES


# ---------------------------------------------------------------------------
# Cross-surface drift pin
# ---------------------------------------------------------------------------


def test_client_caps_match_box_caps() -> None:
    """The PWA's mirrored bounds must equal the box's, or one of them lies.

    The client caps exist for UX (an honest character counter) and for edge
    validation. A mirror that DRIFTS is worse than no mirror: the client would
    refuse a report the box would have accepted, or promise one it will not.
    Read from the TypeScript source rather than duplicated here, so this fails
    when either side moves.
    """
    src = (
        Path(__file__).resolve().parents[1]
        / "web" / "lib" / "algernon" / "bugReport.ts"
    ).read_text(encoding="utf-8")

    assert (
        f"MAX_BUGREPORT_DESCRIPTION_CHARS = {DEFAULT_BUGREPORT_MAX_DESCRIPTION_CHARS}"
        in src
    )
    assert "MAX_BUGREPORT_SCREENSHOT_BYTES = 5 * 1024 * 1024" in src
    assert DEFAULT_BUGREPORT_MAX_SCREENSHOT_BYTES == 5 * 1024 * 1024

    # The media-type allowlists are the third value that must agree.
    for media_type in ALLOWED_SHOT_MEDIA_TYPES:
        assert f"'{media_type}'" in src


def test_bff_route_pins_the_production_peer_env() -> None:
    """The BFF must reach for the DEDICATED token, never the chat one.

    Pinned against the source because the failure is silent: a BFF that sent
    the chat token would be refused by the box's peer pin with a 401 that looks
    like an auth problem rather than like a wiring mistake.
    """
    ts = (
        Path(__file__).resolve().parents[1]
        / "web" / "lib" / "algernon" / "transport.ts"
    ).read_text(encoding="utf-8")
    assert "ALFRED_WEB_BUGREPORT_TOKEN" in ts
    assert "callTransportBugReport" in ts
    assert "X-Alfred-BugReport-User" in ts
