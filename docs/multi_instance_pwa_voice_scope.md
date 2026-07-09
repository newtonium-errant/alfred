All load-bearing claims verified against source. Synthesis follows.

---

# MULTI-INSTANCE PWA VOICE — DECISION-GRADE SCOPE + BUILD PLAN

## 1. RECOMMENDATION + HONEST EFFORT

**GO — as an incremental, single-instance-first arc, gated on a prerequisite "Increment 0."** This is a legitimate, bounded **MEDIUM** arc, not a rebuild and not "config-only + a small FE tweak."

The "70% scaffolded" headline is **true for CHAT and misleading for the whole job.** Split the effort read cleanly in two:

- **The instance SWITCHER is ~80–90% done and ships today — for chat.** The switcher UI (`ChatTargetPicker.tsx`, self-hides at `<=1` target), per-instance thread isolation (`useChat.ts:34-36` per-instance `localStorage` sessionKey + re-bootstrap-on-switch `useChat.ts:262-299`), the server-side `name → {url,token}` map (`transport.ts:400-414` `resolveChatTarget`, read per-request from env, never build-baked), and the owner/known-target gate (`chatRouting.ts` `gateCrossInstance`) all exist and are security-reviewed. **This half is a reference model, not work.** If the ask were only "let me TEXT-chat KAL-LE/Hypatia," it is roughly a day of config + BFF env.

- **VOICE is 0% cross-instance and is the real arc.** It is gated by exactly **one deliberate backend security decision**, which I verified: `register_voice_handlers` Gate 2 (`routes_voice.py:349-360`) **hard-refuses to mount `/voice/*` when `auth.mode == "relay"`** ("V0 pins the session-mode 'web' peer only; fail-closed, security W5"). Cross-instance chat **requires** relay mode. So home-instance (Salem, session-mode) mounts voice; every switch target (relay-mode) will never mount it. That single decision is why the FE, the BFF, **and** the config are all home-only for voice — it is not incidental drift.

**Honest unknowns are all backend/ops, not UI:** (a) does relaxing W5 cleanly admit a relay identity; (b) do the KAL-LE/Hypatia daemons even mount a web app once a `web:` block is added (today they register web/voice routes **x0** — they have `transport:` but no `web:` block); (c) is the WebRTC **media** path reachable per-instance. The FE voice-threading itself is mechanical — it mirrors `useChat`, which already exists as the template.

**Bottom line:** price this as a backend security-surface change + BFF routing + mechanical FE threading + first-time config/token provisioning. The switcher being genuinely 80% done is *why this is feasible in a bounded way* — do not re-scope a switcher build.

---

## 2. THE BUILD PLAN BY LAYER

### Layer 1 — Backend (`src/alfred/web/routes_voice.py`) — THE CORE CHANGE (builder)

This is the single load-bearing code change. Two edits, in the same file:

1. **Relax Gate 2** (`routes_voice.py:349-360`) to permit `mode == "relay"` for voice mounting.
2. **Add a relay branch to `_require_voice_identity`** (`routes_voice.py:92-107`). Today it does: pin `transport_peer == WEB_CHAT_PEER` (line 96), then `require_web_session` (session-mode, `X-Alfred-Session`). New shape: **keep the peer-pin unconditionally**, then branch on `web_config.auth.mode` — session → existing `require_web_session`; relay → resolve the asserted identity via the relay path, mirroring `auth.py:_resolve_relay_identity` (265-360). The `X-Alfred-User` name is re-resolved against **this instance's own** `web.users` roster.

Everything downstream is already instance-agnostic and needs **no change**: `_resolve_chat_binding` (`routes_voice.py:829-866`) is identity-driven (binds against `state_mgr.get_active(identity.synthetic_chat_id)`); `synthetic_chat_id` (`identity.py:62-70`) gives per-instance session/vault isolation for free; the STT/TTS/pipeline builders read `voice.*` + `talker_config.instance.name` dynamically. **Salem proves the entire pipeline is instance-agnostic — the relay-mode gate is the ONLY instance-specific blocker.**

**Reconciled security decision (Facet B is correct):** the relay-voice resolver must pin **only** `WEB_CHAT_PEER`, **NOT** also `RRTS_RELAY_PEER`. I confirmed `auth.py:309` accepts both (`WEB_CHAT_PEER`, `RRTS_RELAY_PEER`) for *chat* intake — but `rrts_relay` is a text bug-report lane, never voice. `routes_voice.py:96` already pins only `web`; preserve that single-peer pin. Fail-closed 401 + logged `web.voice.wrong_peer` on any other peer stays exactly as-is.

### Layer 2 — BFF per-instance voice routing (`web/pages/api/voice/*`) (voice-frontend)

Three routes are single-home today (`offer.ts`, `config.ts`, `close.ts` — all `callTransport` + session token, explicit "home-only" comments at `offer.ts:11`, `config.ts:11`). Convert each to the **same home-vs-cross branch as `chat/stream.ts:74-108`**:

- **home** (`isHomeInstance(instance)`, incl. absent selector) → keep the existing `callTransport(...)` call, unchanged. Salem keeps its session-token path.
- **cross** → `gateCrossInstance(req, instance)` (reused **verbatim**: owner-only 403 → known-target 400), then relay.

**Reconciled disagreement (Facet A is correct over recon/Facet C on the helper):** do **NOT** add a `callVoiceTo` that mirrors `callChatStream`. `callChatStream` is the **SSE-streaming** chat path; voice `offer`/`config`/`close` are all **buffered JSON** (`offer.ts:32`, `config.ts:24`, `close.ts:34` — none stream). The correct drop-in is the existing **buffered** `callChatTo(targetName, method, path, {body, userName})` (`transport.ts:429-453`), which already sends `Authorization: Bearer <target web token>` + `X-Alfred-Client` + `X-Alfred-User` + verbatim JSON round-trip. **Zero new transport helper.** A thin `callVoiceTo` alias delegating to `callChatTo` is optional readability sugar only.

- `/voice/config` is a **GET** → carry instance as `?instance=<name>` (mirror `chatApi.history`'s `?instance=` query, `client.ts:48-52`), read via `req.query`.
- Schemas (`schemas.ts`): add `instance: chatInstanceSchema.optional()` to `voiceOfferBodySchema` (131-135) and `voiceCloseBodySchema` (141-143), **BFF-only, stripped before relay** (mirror `chatTurnBodySchema.instance` `schemas.ts:30-31`). `chatInstanceSchema` already exists (line 14) and constrains the name to `[A-Za-z0-9_]` (`transport.ts:179-181`) so it can't read arbitrary `process.env`.

**Env reuse — no new family:** cross-instance voice reuses `ALFRED_WEB_CHAT_<NAME>_URL/TOKEN` (the target's `web` peer token is exactly the authority voice needs). Do **NOT** invent `ALFRED_WEB_VOICE_*` — that would fork the token model and risk a mis-scoped token crossing instances.

### Layer 3 — FE instance-threading (`web/lib/algernon/`, `VoicePanel.tsx`) (voice-frontend)

- **`VoicePanel.tsx`:** delete the `homeOk = isHomeInstance(instance)` gate (line 37) and the disabled-button + "Voice is available with {HOME} only" branch (98-106). Drive `enabled` off a **per-instance `/voice/config` probe** (`voiceApi.config(instance)`): `null` → explicit "checking…" (never a dead button — intentionally-left-blank), `false` → chat-only hint, `true` → the Voice button. This makes VERA and any no-`web.voice` instance show `available:false` and hide the affordance **naturally, with no name special-case.**
- **`useVoice.ts`:** add `instance` to opts (211-218) + a ref; thread into `voiceApi.config(instance)` (683), `voiceApi.offer(sdp, instance, sessionKey)` (891), and the close paths.
- **THE ONE GENUINELY-NEW CORRECTNESS MECHANISM — close-beacon routing.** Capture the offer-time instance into a `sessionInstanceRef` at offer success (alongside `sessionIdRef`, `useVoice.ts:898`). Add a **dedicated instance-change teardown effect** that fires `closeAndReset('idle')` when `instance` changes while a call is live. Route the close beacon (`useVoice.ts:371`, unmount beacon `:976`) through `sessionInstanceRef.current`, **not the currently-selected instance** — otherwise switching Salem→KAL-LE sends Salem's close to :8892 and strands/leaks the Salem session. **Critical subtlety, verified in the recon:** the existing `enabled`-flag auto-teardown (`useVoice.ts:926-930`) *appears* to handle switch-hangup today only because voice is home-only (any switch flips `enabled` false). Once voice is multi-instance, switching **between two voice instances keeps `enabled` true**, so that path no longer fires — the dedicated instance-change effect is **REQUIRED, not redundant.** This must be explicitly tested with a two-voice-instance switch.
- `sessionKey` binding is already correct: `VoicePanel` gets the per-instance sessionKey from `useChat` (`index.tsx:204`); `start()` is gated on `sessionKey != null` (`VoicePanel.tsx:67`), so the user can't start voice on a newly-selected instance until its chat session has booted. No new ordering work.

### Layer 4 — Per-instance config (config files) (builder)

**Verified ground truth: NO committed config has any `web:` block** (`config.yaml`, `config.kalle.yaml`, `config.hypatia.yaml`, `config.vera.yaml` — zero matches). Salem's live `web.voice` + `web` peer run entirely from **uncommitted/out-of-band config.** So this is *authoring the first committed web/voice blocks*, plus reconciling Salem's live block into the committed source of truth — not editing an existing block.

Each switch-target instance gets (per Facet B's verified field names, `config.py:181-296`):
- A top-level `web:` block: `enabled: true`, `auth.mode: relay`, `users: [{name: andrew, role: owner}]` (email omitted — no magic-link in relay mode).
- A full `web.voice` block mirroring Salem: `pipeline: assistant`, Deepgram `nova-3` STT, ElevenLabs `eleven_flash_v2_5` TTS, barge_in + endpoint_hold enabled.
- **The custom voice_id goes in `web.voice.tts.voice`** (`WebVoiceTtsConfig.voice`, `config.py:253`) — a raw ElevenLabs id passes through `resolve_voice_id` unchanged (`telegram/tts.py:79-92`). **KAL-LE `3Pibk0EXQgBeP7LBvpdV`, Hypatia `vkjIYUm559s4pFsYI5HC`.**
- `shadow_capture.enabled: false` on **both** — Salem-only measurement infra for KAL-LE; **load-bearing PHI exclusion** for Hypatia (Groq raw-PCM egress; `config.yaml.example` explicitly forbids it on PHI/sovereign instances). `endpoint_hold` is safe everywhere (features-only telemetry, no raw-transcript egress).

**CORRECTION to the brief — "/brief voices ride along free" is FALSE.** I verified `web.voice.tts.voice` and `talker.tts.voice_id` are **independent config surfaces** driving independent paths (/voice vs /brief + capture-mode):
- Salem's committed `talker.tts.voice_id` is stock **`"Rachel"`** (`config.yaml:325`), NOT the custom `hvJ50RCIU03khPQwTD66` — even Salem's brief doesn't use its custom voice today.
- KAL-LE has **no `tts` block at all, by deliberate decision** (`config.kalle.yaml:146-149`: "No tts on KAL-LE — no /brief... we don't want audio replies for coding work").
- Hypatia **has** a `talker.tts` block but `voice_id` is **commented out / TBD** (`config.hypatia.yaml:170-171`: "Voice ID TBD — pick a scholar-toned voice").

Setting `web.voice.tts.voice` does **not** populate `/brief`'s voice. Per-instance brief voice is a **separate small decision each** — not a free rider. (For Hypatia: one-line uncomment + fill `voice_id: vkjIYUm559s4pFsYI5HC`, model stays `eleven_turbo_v2_5`. For KAL-LE: adding any `talker.tts` reverses an explicit decision — operator ruling required. For Salem: reconcile `"Rachel"` → `hvJ50RCIU03khPQwTD66` if brief should match the live voice.)

### Layer 5 — Per-instance `web` peer TOKEN + peer-pin (builder — the load-bearing security item)

Each switch-target instance needs a **dedicated peer in `transport.auth.tokens` keyed to the literal NAME `web`** (I confirmed `WEB_CHAT_PEER = "web"`, `auth.py:74`) with `allowed_clients: [web]` and a **per-instance secret**:

```yaml
      web:
        token: "${ALFRED_KALLE_WEB_TOKEN}"   # Hypatia: ${ALFRED_HYPATIA_WEB_TOKEN}
        allowed_clients: [web]
```

Provisioning (all **absent** today):
- **Backend `.env`:** `DEEPGRAM_API_KEY` (verified absent — only `ELEVENLABS_API_KEY` present at `.env:30`; **hard prerequisite** — without it `_build_assistant_stt` fails closed → no voice mount), `ALFRED_KALLE_WEB_TOKEN`, `ALFRED_HYPATIA_WEB_TOKEN`, `ALFRED_SALEM_WEB_TOKEN`.
- **BFF `web/.env.local`:** `ALFRED_WEB_CHAT_KALLE_TOKEN` / `ALFRED_WEB_CHAT_HYPATIA_TOKEN` holding the **same secret value** as the matching backend var (two env files, two processes, one secret each). **Tokens are server-side only — never `NEXT_PUBLIC_`** (the client sends only the instance NAME; `transport.ts` carries a SERVER-ONLY banner).

**Why the peer-pin is per-instance-correct with zero per-instance code** (verified two-layer defense): (1) **Authentication layer** — `auth_middleware` resolves `(client, token)` against *this instance's* `config.auth.tokens` and sets `transport_peer = matched_key` (`server.py:236`); each instance's `web` secret differs, so a KAL-LE token presented to Salem :8891 finds no match → **401 `invalid_token` before any handler**. (2) **Peer-pin layer** — `routes_voice.py:96` pins `transport_peer == WEB_CHAT_PEER`; a `web_ingest` token (which *also* carries `allowed_clients:[web]` — the exact shared-client escalation the CLAUDE.md rule names) resolves `transport_peer="web_ingest"` → pin fails → fail-closed 401 + `web.voice.wrong_peer`. Shared NAME + per-instance SECRET = the pin is automatically correct everywhere.

---

## 3. INCREMENTS — SMALLEST-FIRST SHIPPABLE

**Strongly recommend incremental (prove-the-vertical-on-ONE-instance), NOT all-at-once.** The W5 relaxation is a security-surface change and the single load-bearing unknown — validate it end-to-end on one instance before multiplying token/config surface across three.

**INCREMENT 0 — PREREQUISITE GATE (no user-visible change, do FIRST):**
- Reconcile the committed source of truth: commit Salem's live `web` + `web.voice` block + `transport.auth.tokens.web` (currently out-of-band). Every later increment builds on sand otherwise.
- Provision per-instance `web` peer tokens (backend `.env` + BFF `.env.local`).
- Verify `DEEPGRAM_API_KEY` + `ELEVENLABS_API_KEY` resolvable in each daemon's **runtime** env (box-side check — `DEEPGRAM_API_KEY` is not in committed `.env`).
- **Confirm the daemons serve a web app at all:** kalle/hypatia have `transport:` but no `web:` block, so web/voice routes register **x0** today. Confirm that adding a `web:` block causes the transport daemon to mount the web app and that voice routes register once `web.voice` is present. **This is the true "is it even standable" gate — a NO-GO if it fails, and no FE/BFF work routes around it.**

**INCREMENT 1 — MVP: ONE instance gets voice. Target = HYPATIA.** Backend relay-voice path (relax W5 + relay identity resolver, peer-pinned) → BFF voice routing (reuse `callChatTo` + `ALFRED_WEB_CHAT_HYPATIA_*` + `X-Alfred-User` + `gateCrossInstance`) → FE thread `instance` into `useVoice`/`voiceClient`/`voiceOfferBodySchema` + delete `homeOk` gate + close-beacon `sessionInstanceRef` → Hypatia `web` + `web.voice` config (voice_id `vkjIYUm559s4pFsYI5HC`). **Ship = talk live to Hypatia in her own voice.** Proves the entire vertical.

**INCREMENT 2 — GENERALIZE to N.** With the vertical proven, KAL-LE becomes config+token only (voice_id `3Pibk0EXQgBeP7LBvpdV`) — **BUT this is an explicit product decision, not automatic** (`config.kalle.yaml:146-149` deliberately declines audio for coding work). Requires operator ruling.

**INCREMENT 3 — POLISH.** Per-instance `/voice/config` probe fully drives the affordance; switcher optionally annotates voice-capable vs chat-only; **prompt-tuner capability pass** so KAL-LE/Hypatia SKILLs stop saying "voice not available" (CLAUDE.md feature-enabling → same-cycle capability audit — mandatory, not optional).

**Reconciled MVP disagreement (Facet C is correct over the recon's implied both-at-once):** MVP = **Hypatia, not KAL-LE.** Hypatia *wants* voice (talker.tts scaffold present, scholar-voice intent); KAL-LE's config *actively declines* audio. Shipping KAL-LE voice first would reverse an explicit product decision.

---

## 4. THE SECURITY GATE — EXPLICIT + NON-NEGOTIABLE

The load-bearing CLAUDE.md asserted-identity rule applies to every new cross-instance voice route. Non-negotiable requirements:

1. **Peer-pin per instance.** The relaxed relay-voice path in `_require_voice_identity` MUST keep `transport_peer == WEB_CHAT_PEER` (`routes_voice.py:96`), pinning **only `web`** — NOT also `RRTS_RELAY_PEER`. Fail-closed **401 + logged `web.voice.wrong_peer` (`reason="wrong_peer"`)** on any other peer. A `web_ingest` token shares `allowed_clients:[web]` and would otherwise escalate to full voice scope.
2. **Tokens server-side only.** All `web` peer secrets live in backend `.env` and BFF `.env.local` — **never `NEXT_PUBLIC_`.** The client sends only the instance NAME; the BFF holds the token and asserts `X-Alfred-User`.
3. **No cross-instance drive.** Reuse the CHAT env family (`ALFRED_WEB_CHAT_<NAME>_*`), never the ingest family. Reusing `resolveChatTarget` + `callChatTo` guarantees this. A KAL-LE token cannot drive Salem (401 at authentication, `server.py:199-210`); a `web_ingest` token cannot drive voice (401 at pin).
4. **BFF defence-in-depth.** Run `gateCrossInstance` in every voice route so a non-owner cookie is 403'd BFF-side before any relay (the peer token is the real authority; this is belt-and-suspenders).
5. **Test fixtures pin the production peer NAME** `web` — not any name that merely clears `allowed_clients` (the exact trap the CLAUDE.md rule calls out).

Because W5 relaxation *removes a deliberate fail-closed*, this must be validated on ONE instance before generalizing.

---

## 5. KAL-LE AUDIO-SURFACE NOTE + VERA EXCLUSION

- **KAL-LE deliberately has no audio today.** `config.kalle.yaml:146-149`: "No tts on KAL-LE — no /brief, and we don't want audio replies for coding work." Live voice does **not** need `talker.tts` (it uses `web.voice.tts`), so standing up KAL-LE *voice* is technically just config+token — but **it reverses an explicit product decision and requires an operator ruling.** Do not auto-enable it in Increment 2 without confirmation. `/brief` on KAL-LE is a *separate* decision again (would need a fresh `talker.tts` block).
- **VERA is permanently excluded from voice** — sovereign/PHI, never cloud STT/TTS. VERA may remain a **chat-only target.** The exclusion is enforced at **VERA's config** (no `web.voice` block ever) — the FE only *reflects* the backend's answer via the `/voice/config` probe returning `available:false`. Do not rely on an FE name special-case; if someone mistakenly added `web.voice` to VERA, the FE would happily show the affordance. **Config is the enforcement point.** Also keep `shadow_capture` OFF on Hypatia for the same PHI-egress reason.

---

## 6. RISKS + OPEN QUESTIONS NEEDING OPERATOR RULINGS

**Risks (load-bearing):**
- **Media reachability (hard precondition, out of BFF/FE scope).** cloudflared is HTTP/WS-only; WebRTC **media + DataChannel flow direct browser↔box UDP** after SDP exchange — they never touch the BFF. Each instance's `web.voice.ice` (`advertised_ip`/`stun`) must be set and inbound ephemeral UDP allowed. All three share the box public IP (only ephemeral ports differ). BFF signaling can be perfect and the call still fail at ICE. **Validate on the real deploy, not assumed from the working Salem path.**
- **Live-vs-committed config drift** is an active hazard until Salem's out-of-band `web` block is reconciled into committed config (Increment 0).
- **W5 relaxation removes a deliberate fail-closed** — the peer-pin (§4) is the mitigation; prove on one instance first.
- **Cost/egress:** assistant-pipeline voice streams continuous mic to Deepgram (~$0.46/hr open) + every reply to ElevenLabs ($40–99/mo/instance). Each live voice instance is an **independent cloud-egress decision.** `max_sessions` defaults to 2 **per instance** (`config.py`) — three instances = up to 6 concurrent, independent budgets, even for a single-user operator.
- **Capability-audit rule (CLAUDE.md):** enabling voice is a feature-enabling commit — bundle a prompt-tuner SKILL pass in the same cycle so those instances stop saying "voice not available."

**Open questions needing operator rulings:**
1. **Ratify the auth model:** extend `web.voice` to relay mode (recommended — one code change, keeps single-login) vs per-instance session-mode logins (rejected — multiplies magic-link/session-secret surface). This shapes whether the config blocks are relay-mode or session-mode.
2. **Confirm MVP target = Hypatia** (recommended) and whether **KAL-LE voice is actually wanted** at all (its config declines audio).
3. **Per-instance `/brief` voice** is a *separate* decision: does KAL-LE want a `talker.tts` block (reverses `148-149`)? Which brief voice_id for Hypatia and Salem (reconcile Salem's "Rachel")?
4. **Where does Salem's live `web.voice` + `web` peer config actually live** (uncommitted `config.yaml`? a `.local` override?), and confirm reconciling committed config as source of truth.
5. **Box-side confirm:** `DEEPGRAM_API_KEY` present in each daemon's runtime env (absent from committed `.env`).
6. **Switcher UX polish:** silent auto-hangup on switch-away-from-a-live-call (recommended, parity with chat's silent thread-swap) vs a confirm prompt? Explicit "Voice isn't available for {label}" hint (recommended) vs render-nothing for chat-only instances?

---

## 7. SPECIALIST ASSIGNMENTS + CROSS-AGENT CONTRACT

- **builder** owns: Layer 1 (backend relay-voice path in `routes_voice.py` — relax Gate 2 + relay branch in `_require_voice_identity`, keep the `WEB_CHAT_PEER` pin), Layer 4 (per-instance `web`/`web.voice` config blocks + Salem reconciliation), Layer 5 (`web` peer tokens + `.env` provisioning + `DEEPGRAM_API_KEY`). Backend + config + auth are one coherent builder workstream.
- **voice-frontend** owns: Layer 2 (BFF per-instance voice routing in `web/pages/api/voice/*` + `schemas.ts`) and Layer 3 (`useVoice.ts` / `voiceClient.ts` / `VoicePanel.tsx` instance-threading, `homeOk` gate deletion, `sessionInstanceRef` close-beacon routing, `/voice/config` probe).
- **prompt-tuner** owns: Increment 3 SKILL capability pass (KAL-LE/Hypatia "you can do voice now").
- **code-reviewer** (independent, before every fast-forward to master, per the QA-review-standard — no carve-outs): reviews the W5 relaxation + peer-pin especially.

**The cross-agent contract (agree BEFORE implementing — this crosses domains):**
1. **The backend defines the wire contract.** Cross-instance `/voice/{offer,config,close}` accepts the relay identity via `X-Alfred-User: andrew` + `Authorization: Bearer <target web token>` + `X-Alfred-Client: web`, resolves it against the instance's own `web.users`, and returns the **same response shape as home** (verbatim answer SDP + minted `voice_session_id`). The BFF passes these through byte-identically — proxying to :8892/:8893 is mechanically identical to the working :8891 path.
2. **The `web` peer NAME is the shared contract point.** Backend `transport.auth.tokens.web` (secret) ↔ BFF `ALFRED_WEB_CHAT_<NAME>_TOKEN` (same secret) ↔ pin `WEB_CHAT_PEER == "web"`. Both agents must agree the NAME is literally `web` on every instance before either side wires a token.
3. **FE-can-merge-behind-backend:** the FE `/voice/config` probe returns `available:false` (fails safe, no crash) until the backend relay-voice path lands — so Layer 2+3 can merge FE/BFF-first behind a still-home-only backend without breaking anything. The BFF cross branch should mirror chat's headers exactly (the safe default) pending §Open-Question-1's ratification.

---

## CONCRETE NEXT STEP

**Get the operator ruling on Open Question #1 (ratify relay-mode voice extension — Option A) and #2 (confirm MVP = Hypatia), then execute Increment 0 as a spike:** on the box, add a minimal `web:` block to `config.hypatia.yaml`, restart the Hypatia daemon, and confirm from the logs that (a) the transport daemon mounts the web app and (b) voice routes attempt to register (they'll still fail-closed on relay mode until the code change — that failure log *confirms the mount path is alive*). That one spike resolves the two hardest unknowns — "do the daemons even serve web" and "does `web.voice` reach the relay gate" — before any builder/voice-frontend code is written. If it passes, spawn builder on Layer 1+4+5 for Hypatia and voice-frontend on Layer 2+3 in parallel against the wire contract above. If the daemon can't mount a web app, that is the NO-GO and the arc stops there.

**Relevant files:** `/home/andrew/alfred/src/alfred/web/routes_voice.py` (Gate 2 `349-360`, pin `92-107`), `/home/andrew/alfred/src/alfred/web/auth.py` (`WEB_CHAT_PEER:74`, `_resolve_relay_identity:265-360`), `/home/andrew/alfred/config.hypatia.yaml`, `/home/andrew/alfred/config.kalle.yaml`, `/home/andrew/alfred/config.yaml`, `/home/andrew/alfred/.env`, `/home/andrew/alfred/web/pages/api/voice/{offer,config,close}.ts`, `/home/andrew/alfred/web/lib/algernon/{useVoice.ts,voiceClient.ts,transport.ts,schemas.ts}`, `/home/andrew/alfred/web/components/chat/VoicePanel.tsx`, `/home/andrew/alfred/web/.env.local`.