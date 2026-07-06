# Voice V0 — WebRTC echo transport (runbook)

V0 = **"audio transport up"**: browser mic → WebRTC → aiortc server → echo
back → browser playback. No STT / TTS / chat coupling. Salem-first,
**default-OFF everywhere**, merged inert (the `/voice/*` routes don't mount
unless `web.voice.enabled: true`).

Signaling rides the existing relay chain (browser → Next BFF holding the
`web` peer token → this aiohttp transport). **Media flows DIRECT
browser↔server UDP** — never through the BFF or the cloudflared tunnel
(cloudflared cannot proxy UDP). ICE is **vanilla (non-trickle)**: one
offer/answer round-trip with all candidates embedded, no `/candidate`
endpoint, no renegotiation. Reconnect = a fresh `POST /voice/offer` (new
`voice_session_id`).

Install the server extra:

```bash
pip install 'alfred-vault[webrtc]'   # aiortc (pulls av/ffmpeg + aioice + pylibsrtp)
```

If `web.voice.enabled: true` but aiortc is NOT installed, the daemon still
boots and the routes still mount, but in **503 mode**: `POST /voice/offer` →
`503 {"error":"voice_unavailable","reason":"aiortc_missing"}`,
`GET /voice/config` → `{"available":false,"reason":"aiortc_missing",...}`,
and a loud `web.voice.unavailable reason=aiortc_missing` at mount. That is by
design (a probeable 503 beats an ambiguous 404) — but it means **voice is not
actually working** until the extra is installed.

---

## The WSL2 dev trap (read this before "it's broken")

A **Windows-side browser** pointed at aiortc-running-in-WSL2 under the
default **NAT networking mode** gets **signaling OK, then ICE stuck forever**.
The WSL2 localhost-forwarding relay is TCP-only; UDP is not forwarded
(microsoft/WSL#8783). This masquerades as an app bug — it is NOT. Symptom:
`POST /voice/offer` returns a valid answer SDP, `connectionState` never
leaves `connecting`, no media.

Sanctioned dev paths (pick one):

1. **In-WSL2 Playwright** (the automated acceptance gate, facet 3). Browser
   and server share the WSL2 network namespace, so there's no UDP boundary.
   Chromium flags: `--use-fake-device-for-media-stream
   --use-fake-ui-for-media-stream --use-file-for-fake-audio-capture=tone.wav`.
   Primary assert = `getStats()` inbound-rtp `bytesReceived` rising.
2. **aiortc in-process loopback** — `tests/integration/test_voice_echo_aiortc.py`
   (skipif-gated on aiortc). Same namespace, real negotiation, proves echo
   frames flow back. Run with `pip install aiortc` first.
3. **Mirrored networking mode** for the ears-on human demo — set
   `networkingMode=mirrored` in `.wslconfig` (Win11 22H2+, machine-global,
   operator-gated one-liner), then Windows Chrome at `http://localhost:3000`
   works end-to-end including UDP. `localhost` is a secure context, so
   `getUserMedia` needs no HTTPS. **Validate before promising ears-on** — the
   mirrored-mode path is a caveat, not a guarantee, on every host.

---

## Deploy (OVH box, direct UDP)

The recommendation is **DIRECT UDP** to the public IP. The box carries its
public IP on-interface, so host candidates are directly reachable and the
NAT'd browser's outbound checks open its own binding (server learns prflx) —
**no STUN/TURN needed on either side**. Signaling keeps riding the cloudflared
HTTPS chain unchanged.

Operator-gated infra actions at deploy (**zero code changes**):

1. **Open the ephemeral UDP range on the firewall.** aiortc/aioice cannot pin
   UDP ports (aiortc#487), so it binds OS-random ephemeral ports:

   ```bash
   sudo ufw allow proto udp to any port 32768:60999 comment 'webrtc media'
   ```

   Do **NOT** scope this to IPv4 only — `ufw` applies to v4+v6 by default and
   ICE may pick either family. If you scope to v4 you'll get one-way or dead
   media on v6-preferring clients.

2. **Audit what actually bound** (security W8) after the first session:

   ```bash
   sudo ss -ulpn | grep -E 'python|aiortc'   # confirm only the expected proc listens
   ```

   The open range is defended by ICE itself — non-STUN / wrong-ufrag packets
   are dropped by aioice before DTLS. Optional hardening: narrow
   `net.ipv4.ip_local_port_range` (box-global sysctl — document the tradeoff,
   don't default it) and/or a `ufw` rate-limit on the range.

3. **Flip the config** and restart the talker:

   ```yaml
   web:
     voice:
       enabled: true
       # advertised_ip: ""   # leave EMPTY — OVH puts the public IP on-interface
   ```

   Set `ice.advertised_ip: <public-ip>` **only** if `ip addr` shows the box
   carries a private address behind 1:1 NAT (then the answer SDP's host
   candidates would otherwise advertise the unreachable private addr). When
   the public IP is already on the NIC, leave it empty — the rewrite is
   unnecessary and setting it wrong breaks media.

### BFF timeout floor (security W4 — load-bearing invariant)

The BFF's buffered transport timeout MUST exceed the server's negotiation
timeout, or the BFF 504s the browser before the transport can answer:

```
ALFRED_WEB_TRANSPORT_TIMEOUT_MS  >  (offer_timeout_seconds + 2s)
```

With the default `offer_timeout_seconds: 10`, the floor is **~12s**. If you
raise `offer_timeout_seconds`, raise the BFF timeout in lockstep.

---

## iOS on-device QA checklist (open until validated)

`getUserMedia` is fixed since iOS 13.4, but standalone-PWA quirks are
unverified on current iOS. Before promising iOS voice:

- [ ] Mic permission prompt fires and re-prompts sanely across app relaunch.
- [ ] Playback starts inside the mic-tap gesture (autoplay policy) — never
      rely on the srcObject-autoplay exemption (Chrome docs it as subject to
      change; Safari is stricter).
- [ ] `RTCPeerConnection` reaches `connected` on cellular AND Wi-Fi.
- [ ] Audio routes to the earpiece/speaker as expected (no silent output).
- [ ] Backgrounding / lock-screen does not wedge the session (the reaper's
      connect-deadline / idle timers free the slot server-side regardless).

---

## Contingencies + envelope

- **WS-audio fallback** (if UDP-blocking client networks become real): the
  signaling routes + `VoiceSessionManager` (cap / timeout / shutdown /
  observability) are transport-agnostic. A WS fallback would add one route +
  a `WsSession` sibling class riding the same auth chain over cloudflared —
  documented, NOT built.
- **CPU envelope**: `max_sessions: 2` until smoke-measured. Per echo session
  Opus decode+encode is 50-300× realtime native; the dominant cost is Python
  asyncio handling ~50 frames/s. Budget ≤10% of one core + ~30-60 MB RSS per
  session (av/ffmpeg ~100 MB RSS loads once via the lazy import on the first
  offer — expect a ~1s first-negotiation penalty, logged
  `web.voice.aiortc_imported`). This is a **budget, not a measurement** — the
  Playwright smoke test should record the actual talker CPU/RSS delta before
  raising `max_sessions`.

---

## Observability (grep these)

All structured under `web.voice.*`, **no SDP bodies ever logged** (byte-size
+ m-line count only):

| Event | Meaning |
|---|---|
| `web.voice.registered` | routes mounted, full mode |
| `web.voice.disabled` | not mounted (`reason=not_enabled\|relay_mode\|unknown_pipeline`) |
| `web.voice.unavailable` | mounted in 503 mode (`reason=aiortc_missing`) |
| `web.voice.ice_option_unapplied` | reserved ICE knob set but not applied (`udp_port_range`) |
| `web.voice.wrong_peer` | a non-`web` peer token hit a voice route (escalation block) |
| `web.voice.session.open` / `.replaced` / `.connected` / `.state` / `.close` / `.fail` | lifecycle |
| `web.voice.reject reason=too_many_sessions` | cap hit (incl. in-flight negotiations) |
| `web.voice.track_received` / `.track_ignored` / `.track_ended` | media |
| `web.voice.reaper_started` / `.reaper_error` | reaper task |

---

## V1 — assistant pipeline (streaming STT → text reply)

`pipeline: assistant` turns the live WebRTC session into a walkie-talkie
dictation chat: the mic is tapped for **streaming STT** (a second
`relay.subscribe` off the inbound track feeds a `VoiceSttWorker` → Deepgram
`nova-3` over a WebSocket), silence is sent outbound (the V2 TTS source-swap
seam is kept alive), end-of-utterance text drives the SAME
`run_turn_streaming` engine the chat UI uses, and the reply streams back over
a **`voice` datachannel**. Voice turns land in the exact `/chat/history` the
browser renders. NO TTS yet (that's V2).

**Default-OFF and merged inert**: `pipeline: echo` stays the default; flip to
`assistant` per-instance at deploy. Unknown `pipeline`, absent/unknown STT
provider, or `deepgram` with an unresolved `${DEEPGRAM_API_KEY}` all
fail-closed (no mount, loud `web.voice.disabled`).

### Egress posture (per-instance decision — read before enabling)

`pipeline: assistant` streams **CONTINUOUS mic audio — all ambient speech in
the room — to the cloud STT provider** for the whole time the session is
connected. This is a per-instance privacy call:

- **Salem** = accepted residual (single-operator, owner's own mic).
- **VERA / sovereign voice** = a SEPARATE arc; do **NOT** enable cloud STT
  there.

There is **no push-to-talk or wake-word** in V1 — any speech becomes a
persisted chat turn. **Mute is the only gate.** Say this plainly in any UI
copy.

### Cost honesty

Deepgram `nova-3` streaming is ~$0.0077/min PAYG ≈ **$0.46/hr while the mic is
open**, billed **through silence** (STT runs whenever media flows). Bounded
by `no_speech_close_s` (default 600 s — a connected session with zero speech
closes with `reason=no_speech`) and `max_session_seconds` (1800 s absolute).
Verify the box's Deepgram project concurrency/billing in the console at ship.

### iOS caveats

- **Screen lock kills the session.** An in-flight turn still **completes
  server-side** and appears in history on return — that's the expected flow,
  not a bug. Wake Lock (frontend) mitigates while the screen is unlocked.
- Same getUserMedia / autoplay caveats as V0 apply.

### Config + deploy

Set `web.voice.pipeline: assistant` + the `web.voice.stt` block (see
`config.yaml.example`); `DEEPGRAM_API_KEY` must be in the box `.env`. The
media-plane deploy (ufw UDP range, direct-UDP, BFF timeout floor) is
UNCHANGED from V0 above — only the signaling adds a datachannel m-line (noise
in the SDP). Dev smoke is keyless via `provider: fake` (a finite 3-utterance
script; then idle + `web.voice.stt.script_exhausted`).

### V1 observability (grep these — NO transcript text, chars/counts only)

| Event | Meaning |
|---|---|
| `web.voice.registered pipeline= stt_provider=` | assistant mount |
| `web.voice.disabled reason=stt_unconfigured\|unknown_stt_provider\|stt_key_missing` | assistant fail-closed no-mount |
| `web.voice.stt.config_clamped` | an STT setting was clamped at mount |
| `web.voice.assistant_tap` | mic tapped for STT on a session |
| `web.voice.stt.worker_started` / `.connected` / `.worker_closed` | STT worker lifecycle (close summary carries utterances/finals/dropped counts) |
| `web.voice.stt.utterance_end` / `.utterance_empty` | EOU fired / below-floor noise |
| `web.voice.stt.backpressure_drop` | audio queue drop-oldest (first + every 50th) |
| `web.voice.stt.reconnect` / `.error fatal=true` | Deepgram reconnect / fatal (→ session closes `reason=stt_failed`) |
| `web.voice.session_bound bind_mode=explicit\|reused\|opened` | chat-session binding at offer |
| `web.voice.bad_session_key` | offer session_key didn't match the active chat session (400) |
| `web.voice.session.close reason=no_speech` | silent-connected assistant session reaped |
| `web.voice.dc_drop` / `.dc_backpressure_drop` / `.dc_event_truncated` | datachannel send drops / oversize turn_final |
| `web.voice.utterance_superseded` | latest-wins queue replaced a pending utterance |
| `web.voice.turn_complete` / `.engine_error` / `.turn_session_gone` / `.turn_slot_timeout` | turn outcomes |
| `web.voice.driver_closed` | turn driver teardown (carries aggregate drop counts) |
