"""In-app bug reporting — ``POST /vault/bugreport``, a SAVE-ONLY route (#95).

The operator hits a button in the PWA, the screen they were looking at is
captured, and this route puts the words plus the picture somewhere durable and
says so. It runs NO model: no triage, no classification, no intake
conversation. Everything here is I/O and refusals.

That is deliberate, and it is the v1 seam. Honeydew's port of this feature grew
a conversational intake agent that asks a clarifying question and synthesizes
prose before submitting; that half is DEFERRED here by ruling. The shape it
would slot into is the ``description`` field — an intake would rewrite that
string before it ever reached this route, and nothing below would change. So a
v2 lands entirely on the far side of this handler, which is why this one can
afford to be dumb.

WHERE THE FILES LAND, AND WHY THE SCREENSHOT IS NOT BESIDE THE MARKDOWN.
The report body goes to ``<vault>/inbox/bugreport-<id>.md`` because ``inbox/``
is what the curator watches — that is the whole point of filing it there. The
screenshot goes one level down, into ``<vault>/inbox/.bugreport-attachments/``,
and the leading dot is load-bearing:

  * ``watcher.full_scan`` skips any entry that is not a file and any name
    starting with ``.``, so the directory is invisible to the scan twice over.
  * ``is_ingestable`` (#88) admits only ``.md`` / ``.markdown`` / ``.txt`` by
    default, so even a PNG dropped directly in ``inbox/`` would be refused.

Either guard alone would do. Both are used because #88 is exactly this defect
having already happened once — 39 chat screenshots pushed through ``claude -p``
a second time — and the cost of being wrong is paid in vision-model billing,
which is a bad way to discover a filing mistake. The markdown names the
attachment by a vault-relative path so a human (and the curator agent) can find
it without knowing this rule.

Auth: Layer 1 (peer token + ``X-Alfred-Client``) is enforced by the transport
``auth_middleware`` for every non-``/health`` route. This handler ALSO peer-pins
``transport_peer == "web_bugreport"`` (:data:`BUGREPORT_PEER_NAME`). The chat
``web`` token, ``web_ingest``, ``web_batch`` and this one can all legitimately
carry ``allowed_clients: [web]``, so ``allowed_clients`` cannot tell them apart,
and without the pin a chat token would drive vault writes. Same requirement as
``/vault/ingest`` and ``/vault/batch``; see CLAUDE.md "Relay / asserted-identity
routes — peer-pin requirement". The asserted reporter
(``X-Alfred-BugReport-User``) is PROVENANCE ONLY, never an authz principal —
possession of the ``web_bugreport`` token IS the authority.

Opt-in inertness: :func:`register_bugreport_routes` mounts NOTHING unless
``transport.bugreport.enabled`` is true, so every un-opted-in instance's
transport server stays byte-unchanged.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import (
    DEFAULT_BUGREPORT_MAX_DESCRIPTION_CHARS,
    DEFAULT_BUGREPORT_MAX_SCREENSHOT_BYTES,
)
from .peer_handlers import _KEY_WEB_NOTIFY_SINK, _get_vault_path
from .utils import get_logger

log = get_logger(__name__)

#: The transport peer NAME (``auth.tokens`` key) whose token authorises filing a
#: bug report. PINNED explicitly rather than trusting ``allowed_clients``: the
#: chat ``web`` token and this one both legitimately carry
#: ``allowed_clients: [web]``, so without the pin a full-chat token would clear
#: Layer 1 and write into the vault. See the module docstring.
BUGREPORT_PEER_NAME = "web_bugreport"

#: Screenshot types accepted. Deliberately narrower than the chat composer's
#: set: the capture path produces PNG, and the only reason to accept anything
#: else is a future "attach a file" affordance, which JPEG and WebP cover. A
#: type outside this set is REFUSED by name rather than stored with a guessed
#: extension — a file whose bytes disagree with its extension is worse than no
#: file, because nothing downstream can tell.
ALLOWED_SHOT_MEDIA_TYPES: frozenset[str] = frozenset({
    "image/png", "image/jpeg", "image/webp",
})

#: media type → on-disk extension. The stored name is ``<report-id>.<ext>``.
_EXT_BY_MEDIA_TYPE: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

#: The hidden directory under ``inbox/`` that holds screenshots. The leading dot
#: is what keeps the curator's scan away from it — see the module docstring.
ATTACHMENT_DIRNAME = ".bugreport-attachments"

#: Provenance field ceilings (defense-in-depth; the BFF's zod validates first).
#: These bound what a peer can write into frontmatter, not what a human would
#: plausibly send.
_MAX_ROUTE_CHARS = 512
_MAX_USER_AGENT_CHARS = 512
_MAX_APP_VERSION_CHARS = 120
_MAX_REPORTER_CHARS = 200
_MAX_INSTANCE_CHARS = 120

# Application-storage keys, stashed by ``register_bugreport_routes`` so the
# handler reaches its config without globals (same shape as ingest / batch).
_KEY_BUGREPORT_INSTANCE = "transport.bugreport_instance"
_KEY_BUGREPORT_MAX_DESCRIPTION = "transport.bugreport_max_description_chars"
_KEY_BUGREPORT_MAX_SCREENSHOT = "transport.bugreport_max_screenshot_bytes"


def _json_error(status: int, error: str, **extra: Any) -> web.Response:
    """Consistent error shape — ``{"error": <code>, ...}``, as ingest uses."""
    payload: dict[str, Any] = {"error": error}
    payload.update(extra)
    return web.json_response(payload, status=status)


def mint_report_id(*, now: datetime | None = None) -> str:
    """A fresh report id: ``<YYYYMMDD-HHMMSS>-<6 hex>``.

    Minted SERVER-SIDE and never taken from the request, which is what makes it
    safe to use as a path segment: there is no input to traverse with. The
    timestamp prefix sorts an ``inbox/`` listing chronologically; the random
    suffix means two reports in the same second cannot collide.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def _clean(value: Any, limit: int) -> str:
    """A trimmed, length-capped string from an untrusted field.

    Control characters are stripped rather than escaped. Every one of these
    values is interpolated into YAML frontmatter, and a stray newline in a
    user-agent string would end the value and start what YAML reads as a new
    key — a parse failure that would present as "the curator ignores my bug
    reports" rather than as anything about a user-agent.
    """
    if not isinstance(value, str):
        return ""
    flattened = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    return flattened.strip()[:limit]


def _yaml_quote(value: str) -> str:
    """Quote a scalar for YAML frontmatter.

    Single-quoted style, doubling any embedded single quote. Chosen over bare
    scalars because these are untrusted values and a bare ``route: /chat: 1``
    is not the string it looks like; chosen over double-quoted style because
    single-quoted YAML has no escape sequences at all, so a backslash in a
    Windows-ish user agent cannot mean anything.
    """
    return "'" + value.replace("'", "''") + "'"


def _render_report(
    *,
    report_id: str,
    description: str,
    reporter: str,
    instance: str,
    route: str,
    user_agent: str,
    app_version: str,
    viewport: str,
    reported_at: str,
    screenshot_rel: str,
) -> str:
    """The inbox markdown: frontmatter provenance + the operator's own words.

    The description is written VERBATIM into the body and never into
    frontmatter. Two reasons, and the second is the load-bearing one: prose
    belongs in a body, and a multi-line description in a YAML scalar is the
    single most likely thing to break the frontmatter parse that the curator
    depends on to admit the file at all.
    """
    fm = [
        "---",
        "source: bug-report",
        f"bugreport_id: {_yaml_quote(report_id)}",
        f"reported_by: {_yaml_quote(reporter)}",
        f"reported_at: {_yaml_quote(reported_at)}",
        f"instance: {_yaml_quote(instance)}",
        f"route: {_yaml_quote(route)}",
        f"app_version: {_yaml_quote(app_version)}",
        f"user_agent: {_yaml_quote(user_agent)}",
        f"viewport: {_yaml_quote(viewport)}",
    ]
    # Absent rather than empty when there is no picture: a reader scanning
    # frontmatter should see the field only when it points at something.
    if screenshot_rel:
        fm.append(f"screenshot: {_yaml_quote(screenshot_rel)}")
    fm.append("---")

    lines = ["\n".join(fm), "", "# Bug report", "", description, ""]
    lines += [
        "## Capture context",
        "",
        f"- Reported by: {reporter or '(unset)'}",
        f"- Instance: {instance or '(unset)'}",
        f"- Route: {route or '(unknown)'}",
        f"- App version: {app_version or '(unknown)'}",
        f"- Viewport: {viewport or '(unknown)'}",
        f"- User agent: {user_agent or '(unknown)'}",
        f"- Reported at: {reported_at}",
    ]
    if screenshot_rel:
        # THE PLAIN PATH IS THE LOAD-BEARING HALF. Obsidian does not index
        # dot-folders, so the embed below will usually render UNRESOLVED —
        # it is kept because it costs nothing and would resolve for anyone
        # who does index the attachment directory, but it is NOT what makes
        # the picture findable. The path is, and the record says so rather
        # than leaving a reader to conclude the screenshot went missing when
        # an embed fails to render.
        lines += ["", "## Screenshot", "", f"![[{screenshot_rel}]]", "",
                  f"Saved at `{screenshot_rel}` (relative to the vault root). "
                  "Open it by that path — the embed above may not render, "
                  "because Obsidian does not index dotted folders."]
    else:
        # Intentionally-left-blank: "no screenshot" is a real outcome (capture
        # can fail, and the reporter can remove it), and saying so is what
        # keeps it distinguishable from a screenshot that went missing.
        lines += ["", "## Screenshot", "",
                  "No screenshot was attached to this report."]
    return "\n".join(lines) + "\n"


def _decode_screenshot(
    body: dict[str, Any], *, peer: str, max_bytes: int,
) -> "web.Response | tuple[bytes, str] | None":
    """Decode the optional screenshot, or return the refusal that explains why.

    Returns ``None`` when no screenshot was sent (the common, legitimate case —
    a report is never blocked on a picture), ``(bytes, media_type)`` on success,
    and a ready :class:`web.Response` on every refusal.

    The BASE64 LENGTH is checked before decoding. Decoding first would allocate
    the full payload in order to discover it is too big, which is the thing the
    cap exists to prevent.
    """
    raw_b64 = body.get("screenshot_b64")
    if raw_b64 is None or (isinstance(raw_b64, str) and not raw_b64.strip()):
        return None
    if not isinstance(raw_b64, str):
        log.warning("transport.bugreport.rejected",
                    reason="invalid_screenshot", peer=peer)
        return _json_error(400, "invalid_screenshot")

    media_type = _clean(body.get("screenshot_media_type"), 100).lower() or "image/png"
    if media_type not in ALLOWED_SHOT_MEDIA_TYPES:
        log.warning(
            "transport.bugreport.rejected", reason="unsupported_media_type",
            peer=peer, media_type=media_type,
        )
        return _json_error(
            415, "unsupported_media_type", media_type=media_type,
            allowed=sorted(ALLOWED_SHOT_MEDIA_TYPES),
        )

    # base64 is 4 chars per 3 bytes; this over-estimates the decoded size by at
    # most two bytes of padding, which is the safe direction to be wrong in.
    approx_bytes = (len(raw_b64) * 3) // 4
    if approx_bytes > max_bytes:
        log.warning(
            "transport.bugreport.rejected", reason="screenshot_too_large",
            peer=peer, approx_bytes=approx_bytes, max_bytes=max_bytes,
        )
        return _json_error(413, "screenshot_too_large", max_bytes=max_bytes)

    try:
        # ``validate=True`` so stray non-alphabet bytes are an error rather than
        # silently skipped — a truncated upload must not decode into a
        # plausible-looking shorter image.
        shot = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        log.warning("transport.bugreport.rejected", reason="invalid_base64",
                    peer=peer, error=str(exc)[:120])
        return _json_error(400, "invalid_base64")

    if not shot:
        log.warning("transport.bugreport.rejected", reason="empty_screenshot",
                    peer=peer)
        return _json_error(400, "empty_screenshot")
    # Re-checked against the TRUE size: the estimate above bounds the
    # allocation, this bounds what actually lands on disk.
    if len(shot) > max_bytes:
        log.warning(
            "transport.bugreport.rejected", reason="screenshot_too_large",
            peer=peer, screenshot_bytes=len(shot), max_bytes=max_bytes,
        )
        return _json_error(413, "screenshot_too_large", max_bytes=max_bytes)
    return shot, media_type


def _notify(request: web.Request, *, report_id: str, route: str,
            instance: str, screenshot: bool) -> bool:
    """Best-effort in-app notification that a report was filed. Never raises.

    Returns whether a notification was enqueued, which the response reports
    honestly — the operator should not be told their report is visible in the
    tray when this instance has no notification surface wired.

    A failure here must NOT fail the request: the report is already durable on
    disk by the time this runs, and answering 502 for a missing tray entry
    would invite a retry that files a SECOND copy of the same bug.
    """
    sink = request.app.get(_KEY_WEB_NOTIFY_SINK)
    if sink is None:
        # Intentionally-left-blank: an instance with no web surface has no
        # tray. Observable skip, not silence.
        log.info(
            "transport.bugreport.notify_skipped",
            report_id=report_id,
            reason="no web notify sink registered on this instance — the "
                   "report is filed but nothing will surface it in-app",
        )
        return False
    try:
        sink(
            payload={
                "text": f"Bug report filed{f' from {route}' if route else ''}"
                        f"{' (with screenshot)' if screenshot else ''}",
                "precedence": "R",
                "source": "bug-report",
            },
            from_peer=BUGREPORT_PEER_NAME,
            correlation_id=report_id,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the report is already durable
        log.warning(
            "transport.bugreport.notify_failed",
            report_id=report_id,
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return False


async def _handle_vault_bugreport(request: web.Request) -> web.StreamResponse:
    """POST /vault/bugreport — file one report into ``inbox/``.

    Error taxonomy (JSON ``{"error": <code>}``)::

        wrong_peer (401)              vault_not_configured (503)
        invalid_json (400)            empty_description (400)
        description_too_long (413)    invalid_screenshot (400)
        unsupported_media_type (415)  invalid_base64 (400)
        empty_screenshot (400)        screenshot_too_large (413)
        bugreport_failed (502)
    """
    peer = request.get("transport_peer", "")

    # Peer-pin. Fail-closed, and FIRST — before any config read or disk touch.
    if peer != BUGREPORT_PEER_NAME:
        log.warning(
            "transport.bugreport.rejected",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=BUGREPORT_PEER_NAME,
            detail="filing a bug report requires the dedicated "
                   "'web_bugreport' peer token — refusing a write from "
                   "another peer (e.g. web)",
        )
        return _json_error(401, "wrong_peer")

    vault_path = _get_vault_path(request)
    if vault_path is None:
        log.warning("transport.bugreport.rejected",
                    reason="vault_not_configured", peer=peer)
        return _json_error(503, "vault_not_configured")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → 400
        return _json_error(400, "invalid_json")
    if not isinstance(body, dict):
        return _json_error(400, "invalid_json")

    max_description = int(request.app.get(
        _KEY_BUGREPORT_MAX_DESCRIPTION, DEFAULT_BUGREPORT_MAX_DESCRIPTION_CHARS,
    ))
    max_screenshot = int(request.app.get(
        _KEY_BUGREPORT_MAX_SCREENSHOT, DEFAULT_BUGREPORT_MAX_SCREENSHOT_BYTES,
    ))

    # --- description: the one thing a report cannot do without --------------
    raw_description = body.get("description")
    description = raw_description.strip() if isinstance(raw_description, str) else ""
    if not description:
        # Refused honestly rather than filed as a blank record. A bug report
        # with no words is not a report; accepting one would put an empty file
        # in front of the curator and cost a model call to conclude nothing.
        log.warning("transport.bugreport.rejected",
                    reason="empty_description", peer=peer)
        return _json_error(400, "empty_description")
    if len(description) > max_description:
        log.warning(
            "transport.bugreport.rejected", reason="description_too_long",
            peer=peer, description_chars=len(description),
            max_chars=max_description,
        )
        return _json_error(413, "description_too_long", max_chars=max_description)

    # --- optional screenshot ------------------------------------------------
    decoded = _decode_screenshot(body, peer=peer, max_bytes=max_screenshot)
    if isinstance(decoded, web.Response):
        return decoded
    shot_bytes, shot_media_type = decoded if decoded else (b"", "")

    # --- provenance (metadata only, never authz) ----------------------------
    context = body.get("context")
    context = context if isinstance(context, dict) else {}
    instance = request.app.get(_KEY_BUGREPORT_INSTANCE, "") or ""
    # The header assertion wins over the body field (the BFF sets the header
    # from the verified identity cookie); both are provenance-only.
    reporter = (
        _clean(request.headers.get("X-Alfred-BugReport-User"), _MAX_REPORTER_CHARS)
        or _clean(body.get("reported_by"), _MAX_REPORTER_CHARS)
    )
    route = _clean(context.get("route"), _MAX_ROUTE_CHARS)
    user_agent = _clean(context.get("user_agent"), _MAX_USER_AGENT_CHARS)
    app_version = _clean(context.get("app_version"), _MAX_APP_VERSION_CHARS)
    # The instance the REPORTER was looking at, when the BFF names one; this
    # instance otherwise. Both are recorded on the instance being written to,
    # so a cross-instance report still says which screen it came from.
    viewed_instance = _clean(context.get("instance"), _MAX_INSTANCE_CHARS)
    viewport = _viewport(context)
    reported_at = _clean(context.get("ts"), 64) or datetime.now(
        timezone.utc).isoformat()

    report_id = mint_report_id()
    inbox = Path(vault_path) / "inbox"
    md_path = inbox / f"bugreport-{report_id}.md"

    screenshot_rel = ""
    try:
        inbox.mkdir(parents=True, exist_ok=True)
        if shot_bytes:
            attach_dir = inbox / ATTACHMENT_DIRNAME
            attach_dir.mkdir(parents=True, exist_ok=True)
            shot_name = f"{report_id}.{_EXT_BY_MEDIA_TYPE[shot_media_type]}"
            # Written and renamed into place, so the curator can never observe
            # a half-written image through the embed in the markdown.
            tmp = attach_dir / f".incoming-{secrets.token_hex(8)}.tmp"
            try:
                tmp.write_bytes(shot_bytes)
                os.replace(tmp, attach_dir / shot_name)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            screenshot_rel = f"inbox/{ATTACHMENT_DIRNAME}/{shot_name}"

        rendered = _render_report(
            report_id=report_id,
            description=description,
            reporter=reporter,
            instance=viewed_instance or instance,
            route=route,
            user_agent=user_agent,
            app_version=app_version,
            viewport=viewport,
            reported_at=reported_at,
            screenshot_rel=screenshot_rel,
        )
        # The MARKDOWN is written last and atomically. The curator admits the
        # file the moment it appears, so it must appear complete and it must
        # appear only once its screenshot is already in place — otherwise the
        # record can be processed while pointing at a picture that is not
        # there yet.
        tmp_md = inbox / f".incoming-{report_id}.tmp"
        try:
            tmp_md.write_text(rendered, encoding="utf-8")
            os.replace(tmp_md, md_path)
        except BaseException:
            tmp_md.unlink(missing_ok=True)
            raise
    except OSError as exc:
        log.warning(
            "transport.bugreport.failed",
            reason="write_failed", peer=peer, report_id=report_id,
            error_type=type(exc).__name__, detail=str(exc)[:200],
        )
        # Remove a screenshot whose report never landed — an orphan in a hidden
        # directory is invisible bytes that nothing will ever report.
        if screenshot_rel:
            (Path(vault_path) / screenshot_rel).unlink(missing_ok=True)
        return _json_error(502, "bugreport_failed", detail=str(exc)[:200])

    notified = _notify(
        request, report_id=report_id, route=route, instance=instance,
        screenshot=bool(screenshot_rel),
    )

    log.info(
        "transport.bugreport.filed",
        peer=peer,
        report_id=report_id,
        user=reporter or "(unset)",
        instance=instance,
        route=route or "(unknown)",
        path=str(md_path),
        description_chars=len(description),
        screenshot_bytes=len(shot_bytes) or None,
        screenshot_path=screenshot_rel or None,
        notified=notified,
    )
    return web.json_response({
        "status": "filed",
        "report_id": report_id,
        "path": f"inbox/{md_path.name}",
        "instance": instance,
        "screenshot": bool(screenshot_rel),
        # Reported rather than assumed: the UI says "filed to <instance>"
        # either way, but an operator who was told it would appear in their
        # tray and finds nothing there has been misled.
        "notified": notified,
    })


def _viewport(context: dict[str, Any]) -> str:
    """``<w>x<h>`` from the context block, or ``""`` when unusable.

    Non-numeric or negative values yield an empty string rather than a
    guessed zero — "we did not capture this" and "the viewport was 0x0" are
    different facts, and only one of them is ever true.
    """
    try:
        w = int(context.get("viewport_w"))
        h = int(context.get("viewport_h"))
    except (TypeError, ValueError):
        return ""
    if w <= 0 or h <= 0:
        return ""
    return f"{w}x{h}"


def register_bugreport_routes(
    app: web.Application,
    *,
    enabled: bool,
    instance_name: str,
    max_description_chars: int = DEFAULT_BUGREPORT_MAX_DESCRIPTION_CHARS,
    max_screenshot_bytes: int = DEFAULT_BUGREPORT_MAX_SCREENSHOT_BYTES,
) -> bool:
    """Mount ``POST /vault/bugreport`` — IFF bug reporting is enabled.

    Returns ``True`` when mounted, ``False`` when disabled (opt-in inertness:
    nothing is registered and the transport server stays byte-unchanged). Must
    be called BEFORE the app is started, like every other ``register_*`` helper.
    """
    if not enabled:
        # Intentionally-left-blank: disabled is a deliberate state, logged so
        # "no bugreport route" is distinguishable from "wiring silently
        # skipped" in an operator audit.
        log.info(
            "transport.bugreport.disabled",
            reason="transport.bugreport.enabled is false / absent",
        )
        return False

    app[_KEY_BUGREPORT_INSTANCE] = instance_name
    app[_KEY_BUGREPORT_MAX_DESCRIPTION] = int(max_description_chars)
    app[_KEY_BUGREPORT_MAX_SCREENSHOT] = int(max_screenshot_bytes)
    app.router.add_post("/vault/bugreport", _handle_vault_bugreport)
    log.info(
        "transport.bugreport.registered",
        instance=instance_name,
        max_description_chars=int(max_description_chars),
        max_screenshot_bytes=int(max_screenshot_bytes),
    )
    return True


__all__ = [
    "ALLOWED_SHOT_MEDIA_TYPES",
    "ATTACHMENT_DIRNAME",
    "BUGREPORT_PEER_NAME",
    "mint_report_id",
    "register_bugreport_routes",
]
