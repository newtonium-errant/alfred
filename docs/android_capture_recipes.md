# Android capture recipes

How to get text and voice into a vault from an Android phone. Written for the
~Nov iPhone → Android switch (task #29, ratified 2026-08-04); the readiness
analysis is `~/reports/android-migration-readiness-2026-08-04.md`.

Nothing here is iOS-specific in the other direction either — the bearer door
works from any client that can send an HTTP POST.

## The two doors, and why there are two

Android gives us two capture entry points with **two different auth models**.
Picking the wrong one is the main way this goes wrong, so:

| Affordance | Runs where | Auth | Route |
|---|---|---|---|
| **Share sheet** (Algernon in every app's Share menu) | Inside the installed PWA | The existing **session cookie** — no token | `/api/ingest/submit` (session-authed) |
| **Tasker / Quick-Settings tile / Quick Tap** | No browser, no session | **Bearer device token** | `/api/ingest/shortcut` (unchanged) |

The bearer route needed **zero changes** for Android: a Tasker HTTP POST is
indistinguishable from the iOS Shortcut it was built for. The share sheet is the
new capability, and it is Android-only — `share_target` requires an installed
WebAPK and is not available on iOS/iPadOS, which is why it was never built.

---

## Door 1 — Share sheet (shipped, no device config)

Highlight text in any app → **Share** → **Algernon**. The share lands on `/share`
inside the installed PWA, prefilled into the normal ingest form: pick the target
instance, fix the title if you like, file it.

Setup is just **installing the PWA** (Chrome → ⋮ → *Install app*). Android
registers the share target from the manifest at install time; there is nothing to
configure and no token on the device.

Three things worth knowing:

- **It does not auto-file.** A share opens the review form. The target instance is
  a real choice and the derived title is a guess, so a shared selection gets the
  same review a typed one does — and a failure has somewhere to show itself, which
  it would not once the share sheet has closed.
- **A signed-out share is not lost.** The text is parked in `sessionStorage` the
  moment it arrives; `/share` shows it and offers a sign-in link, and it is still
  there afterwards. (It cannot ride the sign-in redirect: the open-redirect guard
  rejects any character ≤ 0x20, and shared text almost always contains a space.)
- **Titles carry a timestamp.** Sharing two selections from the same article would
  otherwise derive the same title and bounce off `409 title_collision`.

**Known limit:** the share target is `method: GET`, so the payload rides the URL.
Very large selections (a whole long article) can exceed practical URL limits.
Normal selections and links are far below it. Moving to `method: POST` needs
service-worker request interception — deliberately not built yet, and worth doing
only if a real capture actually gets truncated.

---

## Door 2 — Bearer POST (Tasker, Quick-Settings tile, Quick Tap)

This is the eyes-free voice path that replaces the iPhone Action Button. **Server
work: none.** It is the same route, same token model, same rate limit.

### Server env (once)

| Variable | Purpose |
|---|---|
| `SHORTCUT_INGEST_TOKEN` | The device secret. **Required** — until it is set the route answers `503 not_configured` and there is no cookie fallback, ever. Use a long random string. |
| `SHORTCUT_INGEST_SOURCE_LABEL` | Provenance stamped into the record's `source:`. **Set this on Android** — it defaults to `iOS Shortcut`, which would be a false origin. e.g. `Android (Tasker)`. |
| `SHORTCUT_INGEST_USER` | Who the capture is attributed to. Defaults to `Andrew (shortcut)`. |
| `SHORTCUT_INGEST_DEFAULT_TARGET` | Instance used when the request names none. Defaults to `salem`. |
| `SHORTCUT_INGEST_RATE_MAX` | Captures per hour per token. Defaults to `30`. |
| `SHORTCUT_DIRECTIVE_ALIASES` | JSON map of instance → spoken aliases, for speech-to-text mangling. Optional; has built-in defaults. |

Set `SHORTCUT_INGEST_SOURCE_LABEL` **at the same time** as you point a device at
this route. It is the difference between a record that says where it came from and
one that quietly lies.

### The request

```
POST https://<your-host>/api/ingest/shortcut
Authorization: Bearer <SHORTCUT_INGEST_TOKEN>
Content-Type: application/json

{ "text": "buy milk tomorrow" }
```

| Field | Required | Notes |
|---|---|---|
| `text` | yes | The record **body**, written verbatim. Max 8000 characters; blank-after-trim is a 400. |
| `title` | no | Max 300 chars. Omit it and the route derives one from the first ~8 words plus a timestamp. |
| `record_type` | no | Only `note` is accepted today. Omit it. |
| `target` | no | Instance name, max 64 chars, matched case-insensitively against the configured targets. Omit it to use the default. |

Responses: `201` created · `400` bad body or unknown target · `401` bad/missing
token · `429` over the hourly ceiling · `503` token not configured on the server.

### Tasker recipe

1. **Task → new → `Algernon Capture`**
2. **Action: Input → Voice Command** (or *Get Voice*), store into `%vtext`.
   Set the language explicitly if your STT is picking the wrong one.
3. **Action: Net → HTTP Request**
   - Method: `POST`
   - URL: `https://<your-host>/api/ingest/shortcut`
   - Headers:
     ```
     Authorization: Bearer <SHORTCUT_INGEST_TOKEN>
     Content-Type: application/json
     ```
   - Body: `{"text": "%vtext"}`
   - Timeout: 30
4. **Action: Alert → Flash**, text `%http_response_code` — so a failure is visible
   instead of silent. A capture you think succeeded and didn't is the bad outcome.
5. Bind the task to whichever trigger you want:
   - **Quick-Settings tile** — Tasker's *QS Tile* event, closest to the Action Button
   - **Quick Tap** (double-tap the back of the phone) → Settings → System → Gestures
   - A home-screen shortcut, or a Bluetooth-connect trigger for in-car capture

**Keep the token off anything shared.** It is a device secret. Its blast radius is
deliberately small — the route relays through the deterministic-create-only
`web_ingest` peer, so even a stolen token can only create `note` records; it cannot
chat, read, edit, or delete. That is the design, not a reason to be careless.

### Voice routing to another instance

The directive parser works from Android exactly as it does from iOS. Open the
capture with an instance name and it is re-homed and the prefix stripped:

- `message for Hypatia, add to my story idea`
- `for Hypatia: buy milk`
- `Hypatia: buy milk`

Fail-safe by construction: an unknown name — or one that is recognised but not
configured — keeps the words **fully intact** in the default inbox rather than
guessing. A misheard name never costs you the capture. Add STT aliases via
`SHORTCUT_DIRECTIVE_ALIASES` when you find one your phone reliably mangles.

---

## Not done here

- **Login copy still says "best on iPhone"** (`pages/login.tsx`). Deliberately left
  alone: it is device-gated on test T1 confirming how the magic link behaves in the
  installed Android PWA. Do not re-steer that copy until the hardware says which
  path is actually better.
- **Nothing below is device-verified.** No Android hardware exists yet at time of
  writing. Test card T5 in the readiness report is the check that a bearer POST
  from Tasker lands with correct provenance; T1 is the login/cookie-jar question.
  Treat the recipes here as ready-to-try, not as confirmed-working.
