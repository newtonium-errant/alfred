# Algernon web backend — dev handshake (Frontend M1)

Backend-authored contract doc for the **m1-frontend** track. Describes the
exact HTTP handshake the Next BFF performs against a running Algernon
instance's transport server: **login → verify → chat**, with the precise
headers, plus **how to get a working session token in LOCAL dev without the
Resend email round-trip** (Resend won't deliver in local dev).

> This documents the *backend* surface (`src/alfred/web/`). It is frozen for
> M1 — build the FE against these shapes. Non-streaming (streaming is M2).

---

## Base URL

The web routes mount on the **existing transport server** (no separate
port). Default bind is `http://127.0.0.1:8891` (`transport.server.host` /
`transport.server.port` in the instance config). Examples below use:

```bash
export BASE="http://127.0.0.1:8891"
```

---

## Two-layer auth (both required on `/chat/*`)

| Layer | Header(s) | Means | Who holds it |
|---|---|---|---|
| **1 — peer** | `Authorization: Bearer <peer-token>` + `X-Alfred-Client: web` | "this front-end may talk to me" | the **BFF** (server-side only; the browser never sees it) |
| **2 — session** | `X-Alfred-Session: <session-token>` | "this verified named user is driving" | issued by `/auth/verify`; BFF stores it (httpOnly cookie to browser) and relays it as this header |

- **Layer 1 is required on EVERY non-`/health` route** — including
  `/auth/login` and `/auth/verify`. The transport `auth_middleware`
  enforces it; a bad/missing peer token → `401`.
- **Layer 2 is required on `/chat/*` only.** `/auth/login` + `/auth/verify`
  establish it, so they do NOT carry `X-Alfred-Session`.
- The peer token comes from the instance's `transport.auth.tokens.web`
  entry. Set it in the BFF env as e.g. `ALFRED_WEB_PEER_TOKEN`.

```bash
export PEER="$ALFRED_WEB_PEER_TOKEN"   # the transport.auth.tokens.web token
_PEER=(-H "Authorization: Bearer $PEER" -H "X-Alfred-Client: web")
```

---

## LOCAL DEV: get a session token WITHOUT email

Resend won't deliver in local dev, so don't drive `/auth/login`. Instead
**mint a session token directly** with the same codec `/auth/verify` uses.
A session token is just an HMAC of `{user, role, exp}` signed with the
instance's `web.auth.session_secret` — minting one bypasses only the
magic-link delivery, nothing in the chat auth path (`require_web_session`
still verifies the signature AND re-resolves the user against the live
`web.users` allowlist).

Run from the **alfred repo root** (the backend), using the SAME secret the
running daemon loaded:

```bash
# Must match the daemon's web.auth.session_secret (ALFRED_WEB_SESSION_SECRET)
export ALFRED_WEB_SESSION_SECRET="<your-dev-secret>"

PYTHONPATH=src python3 - <<'PY'
import os
from alfred.web.auth import make_session_token
print(make_session_token(
    "andrew", "owner",
    secret=os.environ["ALFRED_WEB_SESSION_SECRET"],
    ttl_hours=168,
))
PY
# → paste the printed token as X-Alfred-Session
```

Notes:
- The user (`"andrew"`) MUST be in the instance's `web.users` allowlist, or
  `/chat/*` returns `401 invalid_session` (the token is re-resolved against
  live config).
- `role` in the mint is cosmetic — `require_web_session` re-resolves the
  role from `web.users`, so live config wins. Use the real role anyway.

```bash
export SESSION="<token printed above>"
_SESSION=(-H "X-Alfred-Session: $SESSION")
```

---

## Production handshake (for reference — what the BFF does)

```bash
# 1. Request a magic link. Uniform {status:"sent"} whether or not the email
#    matches (no enumeration). 503 {error:"email_not_configured"} if Resend
#    creds / base_url are unset.
curl -sS -X POST "$BASE/auth/login" "${_PEER[@]}" \
  -H "Content-Type: application/json" \
  -d '{"email":"andrew@example.com"}'
# → {"status":"sent"}
#   The email contains:  <web.auth.base_url>/auth/callback?token=<magic-token>

# 2. The user clicks the link → lands on the FE /auth/callback page → the BFF
#    POSTs the token here to exchange it for a session token (single-use:
#    a replayed link → 401).
curl -sS -X POST "$BASE/auth/verify" "${_PEER[@]}" \
  -H "Content-Type: application/json" \
  -d '{"token":"<magic-token-from-the-link>"}'
# → {"session_token":"<sess>","name":"andrew","role":"owner","exp":<unix>}
#   401 {"error":"invalid_or_expired"} on bad/expired/forged/replayed token
#   or a user removed from the allowlist.
```

The BFF then stores `session_token` (httpOnly cookie) and relays it as
`X-Alfred-Session` on every `/chat/*` call.

---

## Chat (Layer 1 + Layer 2)

```bash
# Open a session (closes + archives any prior active one, like Telegram).
curl -sS -X POST "$BASE/chat/open" "${_PEER[@]}" "${_SESSION[@]}" \
  -H "Content-Type: application/json" -d '{}'
# → {"session_key":"<uuid>"}

# One turn. kind is "text" | "voice" (voice only tags the transcript).
curl -sS -X POST "$BASE/chat/turn" "${_PEER[@]}" "${_SESSION[@]}" \
  -H "Content-Type: application/json" \
  -d '{"session_key":"<uuid>","message":"hello Salem","kind":"text"}'
# → {"reply":"<assistant text>","session_key":"<uuid>"}

# History of the CURRENT active session (tool-use plumbing flattened out).
curl -sS "$BASE/chat/history/<uuid>" "${_PEER[@]}" "${_SESSION[@]}"
# → {"turns":[{"role":"user"|"assistant","text":"...","ts":"<iso8601>"}]}
```

---

## Error reference

| Route | Status | Body | When |
|---|---|---|---|
| any non-`/health` | `401` | `{"error":"missing_bearer"}` / `{"error":"invalid_token"}` / `{"error":"client_not_allowed"}` | Layer-1 peer token bad/missing |
| `/chat/*` | `401` | `{"error":"invalid_session"}` | Layer-2 session token missing/bad/expired, or user not in allowlist |
| `/chat/turn` | `400` | `{"error":"message_required"}` | empty/blank `message` |
| `/chat/turn`, `/chat/history` | `404` | `{"error":"no_such_session"}` | `session_key` ≠ caller's active session |
| `/chat/turn` | `502` | `{"error":"engine_error","detail":"..."}` | model/engine error |
| `/auth/login` | `400` | `{"error":"email_required"}` | missing `email` |
| `/auth/login` | `503` | `{"error":"email_not_configured"}` | Resend creds / `base_url` unset/unresolved |
| `/auth/verify` | `401` | `{"error":"invalid_or_expired"}` | bad/expired/forged/replayed token or removed user |

---

## M1 caveats (so the FE doesn't over-promise)

- **Non-streaming.** `/chat/turn` returns the full reply; show a typing
  indicator. Token streaming is M2.
- **History = current active session only.** Closed-session / vault-record
  history is a later milestone.
- **No calibration / pushback parity.** Web turns do NOT carry the operator
  voice-calibration or session-type challenge-tuning the Telegram path
  injects (deferred — keyed to machinery web users don't have). Don't claim
  it in UI copy.
- **One active session per user.** A second `/chat/open` archives the prior
  session and starts fresh (mirrors Telegram).
```
