"""The device side of the peer link (task #81, stage 2).

Where a ROUTE capture actually goes. The service holds a sink-shaped
callable and knows nothing about HTTP; this is the one implementation of it,
and it posts to the receiving instance's ``POST /vault/jeeves/capture``.

**A TRANSCRIPT AND NOTHING ELSE.** The payload is built from NAMED FIELDS,
never from a dict the caller hands over, so there is no path by which audio
could ride along even if something upstream started carrying it. The
receiving route independently refuses audio-shaped keys; belt and braces,
because this is the fence the whole design rests on and it is worth checking
at both ends.

**Never raises.** A capture device must keep listening through a peer that
is down, a token that has expired, or a network that has gone away. Every
failure returns ``False``, which the service reads as "keep it local" — the
operator's words survive on the device and he can move them by hand. Losing
a capture to a transport error would be the worst outcome available, because
the audio behind it is already gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from .config import JEEVES_MODE_LIVE, JeevesConfig, JeevesRouteSinkConfig
from .service import RoutedCapture

log = structlog.get_logger(__name__)

#: The route this sink targets. Kept beside the constant the server side
#: mounts (``transport.routes_jeeves``) — a drift pin asserts they agree, so
#: a rename on one side cannot silently leave the device posting into a 404.
CAPTURE_PATH = "/vault/jeeves/capture"


@dataclass
class JeevesTransportSink:
    """Posts a routed capture to the receiving instance.

    Constructed only when both ``base_url`` and ``token`` are configured
    (see :func:`build_route_sink`) — a half-configured sink would fail every
    send, and the service's local fallback is the better shape for that.
    """

    base_url: str
    token: str
    client_name: str = "jeeves"
    timeout_s: float = 15.0
    #: Provenance for the RECEIVER's fail-closed mode gate. A synthetic-mode
    #: device tags its captures ``synthetic: true`` so a synthetic-mode
    #: receiver accepts them — which is what makes an end-to-end dev path
    #: possible without either side being flipped to live.
    provenance: dict[str, Any] | None = None

    @property
    def url(self) -> str:
        return f"{self.base_url.rstrip('/')}{CAPTURE_PATH}"

    async def send(self, capture: RoutedCapture) -> bool:
        """Send one capture. True iff the receiving instance has the record."""
        payload: dict[str, Any] = {
            "transcript": capture.transcript,
            "verb": capture.verb,
            "captured_at": capture.captured_at,
            "capture_facts": dict(capture.capture_facts),
        }
        if self.provenance is not None:
            payload["provenance"] = self.provenance

        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Alfred-Client": self.client_name,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            log.warning(
                "jeeves.sink.send_failed",
                reason="network",
                url=self.url,
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
                stdout_tail="",
            )
            return False

        if resp.status_code == 409:
            # The record already exists — the device retried, or two cues
            # landed on the same title. Idempotent from here: the transcript
            # IS in the vault, which is what the caller asked about.
            log.info(
                "jeeves.sink.already_present",
                status=resp.status_code,
                detail="the receiving instance already holds this capture "
                       "(title collision) — treating the send as complete",
            )
            return True

        if resp.status_code >= 400:
            body_tail = (resp.text or "")[:300]
            log.warning(
                "jeeves.sink.send_failed",
                reason="http_error",
                status=resp.status_code,
                url=self.url,
                # The refusal codes are the actionable part: wrong_peer means
                # the token is not the dedicated jeeves one, capture_refused
                # means the RECEIVER is still in synthetic mode.
                detail=body_tail or "(no body)",
                stdout_tail="",
            )
            return False

        log.info(
            "jeeves.sink.sent",
            status=resp.status_code,
            # A LENGTH, never the words.
            transcript_chars=len(capture.transcript),
            target=capture.target,
        )
        return True

    async def __call__(self, capture: RoutedCapture) -> bool:
        """So the sink can be passed straight in as the service's callable."""
        return await self.send(capture)


def build_route_sink(config: JeevesConfig) -> JeevesTransportSink | None:
    """Construct the sink this config describes, or ``None`` if inert.

    ``None`` is a first-class outcome, not a failure: an unconfigured link
    means ROUTE captures stay in the local log, which is the design's
    deliberate fallback. It is logged either way so a device that is quietly
    keeping everything local is distinguishable from one that is sending.
    """
    route: JeevesRouteSinkConfig = config.route
    if not route.base_url or not route.token:
        # Intentionally-left-blank: "the peer link is not configured" is a
        # state the operator needs to be able to see from a log, because its
        # symptom in the garage is indistinguishable from success.
        log.info(
            "jeeves.sink.inert",
            has_base_url=bool(route.base_url),
            has_token=bool(route.token),
            detail="jeeves.route.base_url and/or jeeves.route.token are "
                   "unset, so no capture sink was built. ROUTE cues will be "
                   "written to the LOCAL mark log instead of being sent — "
                   "nothing is lost, but nothing leaves either.",
        )
        return None

    # A synthetic-mode DEVICE tags what it sends, so a synthetic-mode
    # RECEIVER accepts it. A live device sends no synthetic tag at all: if
    # the receiver is still synthetic it will refuse, loudly, which is the
    # correct outcome — an un-flipped receiver should not silently accept
    # real garage audio because the device asserted something about itself.
    provenance = (
        None if config.mode == JEEVES_MODE_LIVE else {"synthetic": True}
    )

    log.info(
        "jeeves.sink.armed",
        url=f"{route.base_url.rstrip('/')}{CAPTURE_PATH}",
        client=route.client_name,
        mode=config.mode,
        tags_synthetic=provenance is not None,
    )
    return JeevesTransportSink(
        base_url=route.base_url,
        token=route.token,
        client_name=route.client_name,
        timeout_s=route.timeout_seconds,
        provenance=provenance,
    )
