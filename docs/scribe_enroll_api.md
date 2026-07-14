# Scribe voice-enrollment API — FROZEN route + payload contract (P4-5a backend)

**Status:** frozen as of the P4-5 backend merge. **This document is the contract the
PWA slice (Slice B) builds against** — not the design memo, whose route table predates
these decisions. Where they differ, THIS file is authoritative and the differences are
called out explicitly below.

All routes ride the existing **#49 loopback `ingest_web` server** (no new server, port, or
CSP surface). Every route inherits the #49 middleware: Host-pin (421 on a DNS-rebind),
loopback peername (403), never-CORS, constant-time bearer compare, PHI-safe opaque error
bodies (`{"error": "<code>"}` — never a label, path, or transcript).

---

## Two-token capability split

| Token | Capability | Where it lives |
|---|---|---|
| `ingest_web.token` | **encounter** — chunks, close, status, preset *selection* | **EMBEDDED in the served page** (the JS needs it) |
| `ingest_web.enroll_token` | **biometric custody** — enroll, re-record, rename, revoke | **NEVER in the page.** Pasted once by the clinician (memory-only) |

* A token valid for the **other** class → **401** + a `wrong_token_class` audit row (a
  privilege-boundary probe — the durable trail records it).
* `enroll_token` unset/empty ⇒ the **entire enrollment face is INERT: 404** (`enroll_inert`)
  on every enroll-face path, **regardless of the token presented** (404 takes precedence
  over 401). The ingest routes are unaffected.
* `enroll_token == token` ⇒ **refused at config load** (fail-closed to inert + loud error):
  an equal token collapses the split, because page possession already yields the ingest token.

---

## Routes

Auth column: **E** = enroll token · **I** = ingest token · **E|I** = either.

| Method + path | Auth | Query | Body | 2xx response |
|---|---|---|---|---|
| `POST /scribe/enroll/start` | E | `user` (required), `preset` (optional — re-record) | — | `{session, state:"recording"}` |
| `POST /scribe/enroll/chunk` | E | `session`, `seq` (optional but **recommended**) | raw audio bytes | `{windows:<int>}` or `{windows, duplicate:true}` |
| `POST /scribe/enroll/finalize` | E | `session` | `{name}` | `{state:"processing"}` |
| `GET  /scribe/enroll/result` | E | `session` | — | `{state, ...}` — see below |
| `POST /scribe/enroll/abandon` | E | `session` | — | `{state:"abandoned"}` |
| `GET  /scribe/presets` | **E\|I** | `user` (required) | — | `{user, state, presets:[...]}` |
| `POST /scribe/presets/rename` | E | `user`, `preset` | `{name}` | `{preset_id, name}` |
| `POST /scribe/presets/delete` | E | `user`, `preset` | — | `{preset_id, state:"revoked"}` |
| `POST /scribe/encounter/preset` | **I** | `label`, `preset` | — | `{preset_id, state:"bound"}` |
| `GET  /scribe/status` | I | `label` | — | `{..., preset_fit}` |

### ⚠ Divergences from the design memo's route table (reconciled HERE)

1. **`?user` is REQUIRED** on `/scribe/presets`, `/presets/rename`, `/presets/delete`.
   The memo's table omits it. The store is user-keyed (`<enrollment_dir>/<user>/<preset_id>.json`),
   so the server cannot resolve a preset without it. **The PWA must send `?user=`** on all
   three. (Requests without a valid `user` → `400 invalid_request` / `invalid_user`.)
2. **`?seq` on `/enroll/chunk`** is honoured for **idempotency** (a retried window is a
   no-op, answering `duplicate:true`). It is optional — omitting it restores
   append-every-POST, which double-counts on a retry. **The PWA should always send it.**
   `?ext` is accepted but ignored (the container is sniffed from the bytes).

---

## Enrollment state machine (client view)

```
POST /enroll/start            -> {session, state:"recording"}
POST /enroll/chunk  (xN)      -> {windows}                       # ~15 s windows, serial
POST /enroll/finalize         -> {state:"processing"}            # returns IMMEDIATELY
GET  /enroll/result  (poll)   -> {state:"processing"} ... then {state:"done", verdict, stats, preset_id?}
                              -> {state:"unknown_session"}       # daemon restarted / abandoned / TTL
POST /enroll/abandon          -> {state:"abandoned"}             # TERMINAL: bytes dropped, no preset written
```

**`/enroll/result` states:** `processing` · `done` · `unknown_session`.
On `unknown_session` the client offers **Record again** (the session is gone; nothing was kept).

**Verdicts** (`done` only) — the closed set:

| Verdict | Meaning | Preset written? |
|---|---|---|
| `ok` | all four advisory gates passed | ✅ |
| `ok_marginal` | passed the HARD gates; ≥1 advisory gate failed → show the **△ marginal quality** badge | ✅ |
| `too_short` | HARD gate: < 10 s net speech | ❌ |
| `no_speech` | HARD gate: no usable audio | ❌ |
| `decode_failed` | HARD gate: the container could not be decoded | ❌ |
| `engine_error` | the embedder failed | ❌ |
| `abandoned` | internal only — the session was discarded mid-finalize (the client sees `unknown_session`) | ❌ |

**`stats`** (always present, even on a hard-fail): `n_windows`, `duration_s`,
`net_speech_s`, `snr_db_est`, `spread`, `self_sim_mean`, `self_sim_p10`.

**Advisory gates** (fail ⇒ `ok_marginal`, never a refusal): 30 s target duration ·
SNR ≥ 10 dB · self-similarity ≥ 0.80 (mean) · **self-match headroom** (`self_sim_p10 ≥ tau + 0.05`).
All four are *advisory until the first on-box `--calibrate`*, which ratifies the cut-lines.

---

## Refusal codes (opaque `{"error": <code>}`)

| Code | HTTP | Route(s) | Meaning |
|---|---|---|---|
| `enroll_inert` | 404 | any enroll-face | `enroll_token` unset — the face is absent |
| `unauthorized` | 401 | any | bad token, or the **wrong token class** |
| `enrollment_dormant` | **503** | `/enroll/start` | `enrollment_dir` unset, or `diarize.provider: off` — **refused BEFORE recording** |
| `user_not_clinician` | 403 | `/enroll/start` | `user` not in `scribe.clinicians` (verbatim, case-sensitive) |
| `unknown_preset` | 404 | `/enroll/start`, rename, delete | re-record target does not exist for this user |
| `preset_revoked` | 409 | `/enroll/start` | re-record target is a tombstone — **never resurrected** |
| `preset_bound_open_encounter` | 409 | `/enroll/start`, `/enroll/finalize` | the preset is bound to a live (un-`_CLOSED`) encounter |
| `too_many_sessions` | 429 | `/enroll/start` | 2 concurrent **live** sessions (finished ones don't count) |
| `too_many_windows` / `window_too_large` / `session_too_large` | 429 | `/enroll/chunk` | RAM-custody caps |
| `unknown_session` | 404 | chunk / finalize | no such session, or it is no longer `recording` |
| `preset_unusable` | 409 | `/encounter/preset` | the preset is revoked / corrupt / engine-incompatible |
| `preset_locked` | 409 | `/encounter/preset` | a **different** preset is already bound, **or** the encounter already started recording |
| `preset_cap` / `tombstone_cap` | — | finalize | 32 **active** presets/user · 256 stored ids/user |

**The 409s live ONLY on `/scribe/encounter/preset`.** Chunk POSTs carry **zero** preset
semantics — a `?preset=` on an ingest chunk is ignored and never re-binds. (So the shipped
client's "409 ⇒ advance" discipline is safe: no audio is ever lost to a binding refusal.)

---

## `/scribe/presets` response

```json
{"user": "np_jamie", "state": "ok", "mru_preset_id": "pst-…",
 "presets": [{"preset_id": "pst-…", "name": "Clinic Room A", "status": "active",
              "classification": "usable", "centroid_version": 1,
              "quality": {"verdict": "ok_marginal", "advisory": {...}},
              "device_hint": {}, "created_at": "…", "updated_at": "…", "revoked": null}]}
```

* `mru_preset_id` — the user's **most recently BOUND** preset that is still `usable`, or
  `null`. **This is the picker's default.** It is server-derived (from the `_ENROLLMENT.json`
  binding sidecars) *because it has to be*: R5 is absolute, so the client may not remember
  the last choice in storage. An unusable (revoked / engine-incompatible) preset is never
  offered as the default — it would strand the clinician on a preset that cannot attribute.
* `state`: `empty` · `all_incompatible` · `ok` (empty-registry and all-incompatible are
  **distinct** explicit states — render them differently).
* `classification`: `usable` · `incompatible_model` · `incompatible_engine` ·
  `unsupported_schema` · `revoked` · `corrupt`. Anything ≠ `usable` must show the
  **⟳ needs re-record** / **⚠ unreadable** treatment and must not be offered as a selectable preset.
* **No route EVER returns a centroid or audio** (R2 extended to biometrics). Do not add one.

## `/scribe/status` → `preset_fit`

Values **frozen now**: `unarmed` · `warming` · `weak` · `none` · `ok`.
**5a emits only `unarmed` and `ok`.** `warming` / `weak` / `none` activate with the 5b
latch. **The client MUST tolerate all five values today** (treat an unrecognised value as
`unarmed`) so 5b can ship without a client change.

---

## Binding rules (the recording chip / picker)

* Selection happens via `POST /scribe/encounter/preset` **before Start**.
* The binding **locks at the first chunk**: a first bind after recording has begun is
  **409 `preset_locked`** (a late bind would leave the note's `diarize_provenance`
  permanently absent). **Select the preset before the first chunk POST.**
* An identical re-bind (same preset) is **idempotent 200**, even mid-recording (safe retry).
* A binding failure must **never block Start** — "No preset — attribution off" is a
  first-class choice, and the encounter simply runs un-attributed.

---

---

## The served page (what the client is given, and what it is NOT)

`GET /` embeds two `data-` attributes on `<body>` for the same-origin JS (never an inline
script — CSP forbids it):

| Attribute | Contents | Why |
|---|---|---|
| `data-ingest-token` | the **INGEST** token | the JS needs it for chunks / close / status / **binding** |
| `data-clinicians` | JSON array of `scribe.clinicians` slugs | the enrol view OFFERS the identity instead of making it hand-typed — the server matches it VERBATIM, so a typo would fail-close a consented recording with 403. Staff slugs, never PHI. |

**The ENROLL token is NEVER embedded.** Page possession must not grant biometric mutation
— the clinician pastes it once per page-load, memory-only (a reload asks again). There is
no `data-enroll-*` attribute, and there never should be.

## Device containers (the phone is an iPhone — operator ruling)

Do **not** hardcode `webm`. The recorder negotiates a supported type and the client sends
what the device actually produced:

* **Enrolment** — the server **sniffs** the container (webm EBML / mp4 `ftyp`) and
  **ignores `?ext`**, so iOS `audio/mp4` works as-is.
* **Encounter** — `/scribe/ingest-chunk` validates `?ext` against a frozen allowlist
  (`wav ogg mp3 m4a flac webm`) that has **no `mp4`**. iOS emits `audio/mp4`, which is
  **AAC-in-MP4 — whose conventional extension is `m4a`**, and `m4a` *is* on the allowlist,
  *is* swept, and *is* decoded (ffmpeg/whisper sniff by content, not by filename).
  **So the client maps `audio/mp4` → `ext=m4a`.** This honours the iPhone ruling without
  reopening the frozen #49 ext contract. `audio/webm` → `webm`; `audio/ogg` → `ogg`.

⚠ On-box smoke must still confirm a **real iPhone `m4a` chunk decodes end-to-end** — the
mapping is correct by construction but has only been exercised against a shim.

---

## Activation order (on-box runbook — do NOT skip a step)

Enrollment presets are stamped with the **engine fingerprint** and are invalidated when it
changes. Enrolling before the real engine is resolved will mass-invalidate those presets
(`incompatible_engine`) the moment it lands. Correct order:

1. Install the CPU torch + `[scribe-diarize]` extra; stage the models; materialize the
   repo-id-free pipeline config (`scripts.stage_diarize_models`).
2. Land the **real embedder + the real engine-fingerprint accessor** (`embed_voice`
   `pyannote` currently RAISES; `engine_fingerprint` is a placeholder stamp).
3. Close P4-1 NOTE-1 (`diarize.enabled` wired) and set `provider: pyannote, enabled: true`.
4. Set `enrollment_dir`, then arm `enroll_token` (distinct from the ingest token).
5. **Only now** perform the first real enrollment.

Config states that REFUSE enrollment up-front (503 `enrollment_dormant`, before any
recording): `enrollment_dir` empty, or `provider: off`. A `pyannote` provider whose
embedder is not yet implemented will fail at finalize with `engine_error` — hence step 2
before step 5.
