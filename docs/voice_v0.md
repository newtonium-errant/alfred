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

---

## V2 — TTS talk-back (streaming spoken reply)

`web.voice.tts.enabled: true` (on top of `pipeline: assistant`) makes Alfred
**speak** its reply: as `run_turn_streaming` yields sentence chunks, they feed a
streaming TTS provider (ElevenLabs `eleven_flash_v2_5` over a per-turn
WebSocket); the synthesized PCM streams back over the SAME outbound WebRTC track
the V1 silence source used. **Half-duplex** — Alfred listens OR speaks, not both.

**Default-OFF and merged inert**: `tts.enabled: false` is the default and keeps
V1 byte-identical (stock 8 kHz silence outbound, no TTS worker). TTS is an
*enhancement* — an absent / disabled / misconfigured / keyless tts block
**degrades voice to text-only** (loud `web.voice.disabled_tts reason=...`); it
NEVER unmounts `/voice/*` (unlike STT, which is the product). A fatal TTS error
mid-session degrades to text-only + one `tts_unavailable` DC event — the session
LIVES (asymmetry vs STT's fail-honest close: the text reply plane is fully
functional).

### The outbound track is the playout source (no runtime swap)

When tts is enabled the outbound track is a `TTSPlayoutSource` for the whole
session — one constant frame spec (`s16 / mono / 48000 Hz / 960-sample`),
silence-fill when idle, speech when queued. pts is a running sample counter
assigned at emission (`+960` every frame, silence and speech alike); `flush()`
drops queued audio but never touches the counter → monotonic by construction, so
the V1→V2 pts/format-continuity hazard is satisfied without any mid-stream swap.

### Egress posture (per-instance decision — read before enabling)

Enabling tts sends **every assistant voice reply** — and ONLY the reply sentence
chunks, never the system prompt / vault context / user text — to
`api.elevenlabs.io` for that instance.

- **Salem** = accepted residual (single-operator, the owner's own replies).
- **VERA / sovereign voice** = do **NOT** enable tts (keep the reply plane local).

`zero_retention: true` requests `enable_logging=false` (plan-gated at
ElevenLabs; default `false` inherits their logging default — ratify at deploy).

### Cost honesty

`eleven_flash_v2_5` ≈ **0.5 credits/char** ≈ **$40-99/mo at 30-60 min/day of
spoken replies** — ElevenLabs dominates the voice bill (far above Deepgram STT).
**CHECK YOUR KEY TIER before activation** — the free tier lasts ≈ days of real
use. Cost is bounded per turn by `max_tts_chars_per_turn` (default 4000 chars ≈
4 min of speech) and per session by `max_session_seconds`.

### Self-hearing / AEC caveats

Half-duplex relies on the browser's acoustic echo canceller (AEC) as line one +
a **server-side gate** as the backstop: while Alfred is speaking, an utterance
final is DISCARDED (`utterance_discarded` DC event; the transcript still shows
what was heard). Notes for the runbook:

- **First-exchange echo leak is EXPECTED.** Safari AEC needs ~2-5 s to adapt;
  early replies may briefly transcribe Alfred's own voice as a discarded final.
  Not a bug — the server gate prevents a self-conversation loop.
- **Speakerphone / Bluetooth / non-default output routing** can escape AEC →
  more `utterance_discarded` events. Prefer the PWA's PC-fed audio element (the
  well-cancelled path). On iOS check the output route + volume if replies are
  inaudible.

### Cartesia seam (not built)

The `TTSStreamProvider` ABC maps 1:1 onto Cartesia Sonic (begin_turn→context_id,
feed_text→transcript+continue, end_of_reply→flush, `interrupt_speech`→cancel,
native `pcm_s16le`). Adding it = a new `tts_cartesia.py` + one
`_KNOWN_TTS_PROVIDERS` entry + one factory case; zero changes to the
worker/playout/driver/config shape.

### V2 observability (grep these — NO reply text, chars/bytes/ms/status only)

| Event | Meaning |
|---|---|
| `web.voice.registered tts_provider=` | tts mounted alongside the assistant pipeline |
| `web.voice.disabled_tts reason=not_enabled\|tts_unconfigured\|unknown_tts_provider\|tts_key_missing` | tts off / misconfigured → voice mounts text-only |
| `web.voice.tts.config_clamped` | a tts setting was clamped at mount |
| `web.voice.tts.worker_started` / `.worker_closed` | TTS worker lifecycle (close summary carries turns/chars/audio counts) |
| `web.voice.tts.connected` / `.turn_done` | per-turn ws connect / turn synthesized |
| `web.voice.tts.turn_capped` | per-turn spoken cap hit (fed prefix still speaks) |
| `web.voice.tts.turn_degraded` | a transient (network/rate-limit) per-turn failure (retry next turn) |
| `web.voice.tts.latched_off` / `.degraded_text_only` | auth/bad-request or 3 consecutive failures → TTS off for the session (session LIVES) |
| `web.voice.tts.interrupted` / `.flush` | audio-plane cancel (client cancel / new-turn / engine-error / teardown) |
| `web.voice.tts.feed_overflow` / `.backpressure`/`.underrun` | text-queue drop-newest / playout buffer pressure |
| `web.voice.utterance_discarded_speaking` | half-duplex gate dropped a final while speaking |

### Config + deploy

Set `web.voice.tts.enabled: true` + the `web.voice.tts` block (see
`config.yaml.example`); `ELEVENLABS_API_KEY` must be in the box `.env`. The
media-plane deploy is UNCHANGED from V0/V1 (the outbound track already existed).
Dev smoke is keyless via `provider: fake` (a 440 Hz tone proportional to the
reply, audible through the real WebRTC/Opus path). Real ElevenLabs is validated
by a one-turn live gate test (`ELEVENLABS_API_KEY` present) and at box
activation.

---

## V3 — barge-in (speak-to-interrupt)

`web.voice.tts.barge_in.enabled: true` (on top of `tts.enabled`) lets you
**interrupt** Alfred mid-reply: start talking while he's speaking and playback
stops, your utterance becomes the next turn. Default-OFF; disabled = V2
half-duplex (finals while speaking are discarded) byte-identical.

### Two-stage commit (why it's not trigger-happy)

- **Stage A** — a qualifying STT *partial* while speaking interrupts the AUDIO
  only (silent on the wire). It does NOT commit a turn.
- **Stage B** — the utterance's *final* re-runs the SAME gates; only on pass
  does the barge commit (cancel any running turn + submit the new one).

Suppression pipeline (in order): `too_early` (first 700 ms) → interrupt-phrase
bypass (`stop` / `wait` / **the instance name** — "Salem!" — + config extras) →
backchannel (`yeah` / `uh huh` / …) → `min_words`/`min_chars` floor → **echo
gate**. The echo gate compares your words against what Alfred actually spoke
(`echo_score ≥ echo_threshold` ⇒ suppress) so his own voice bleeding into the
mic (Bluetooth / imperfect AEC) can't self-interrupt — including a 2 s
post-drain grace window for the echo tail. The score is garble-resistant (a
single ASR substitution / dropped / split word still matches).

### Honesty

- **Latency envelope: ~0.7-1.6 s** perceived stop-after-you-start-talking
  (interrupt phrases faster than generic speech). Measured at smoke, reported.
- **Truncated audio is accepted:** a barge flush is destructive (there's no
  duck/hold), so the interrupted reply's audio is cut — its text still completes
  and persists in history.
- **Storm breaker:** 3 consecutive confirmed barges each landing <2 s into
  playback auto-disable barge for the session (`web.voice.barge.storm_disabled`).
- No Deepgram / VAD change — the partial transcript IS the trigger (SpeechStarted
  stays dark; zero added cost/latency).

### Observability (grep `web.voice.barge.*` — ids / ms / scores only, no text)

| Event | Meaning |
|---|---|
| `web.voice.barge.enabled` / `.disabled reason=not_enabled\|tts_unavailable` | mount state |
| `web.voice.barge.config_clamped` | a setting / list entry was clamped or dropped |
| `web.voice.barge.triggered` | Stage-A audio interrupt fired |
| `web.voice.barge.suppressed reason=too_early\|backchannel\|too_short\|echo` | a partial/final was gated out |
| `web.voice.barge.confirmed` | Stage-B committed the barge |
| `web.voice.barge.late_suppressed reason=echo` | echo caught at Stage B / in the post-drain grace window |
| `web.voice.barge.outcome outcome=completed\|empty\|cancelled` | the barge's turn resolved (learner-ready: `empty` = false barge) |
| `web.voice.barge.storm_disabled` | storm breaker tripped |

Barge events carry `utterance_id`, `turn_id`, `ms_into_speaking`, `score`
(echo), `reason` — a V3.1 self-correcting arc joins these (+ the client cancel
logs, which carry ISO timestamps) to tune the per-user thresholds.

### Config + deploy

Set `web.voice.tts.barge_in.enabled: true` + the block (see
`config.yaml.example`). No new env. Merges INERT; activation of the whole voice
stack (V1 dictation + V2 talk-back + V3 barge) is one operator-gated config flip.
