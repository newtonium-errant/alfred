"""The STAY-C loopback PWA client assets (#49 Slice B) — served by ingest_web.

A MINIMAL, single-operator, loopback-only PWA the clinician loads on-box (or via
an SSH/WG tunnel to 127.0.0.1) to push mic-audio chunks to the Slice-A ingest
routes. Contract-bound to the shipped backend (ingest-chunk / close / status).

R4 (zero external resources) — the page loads NOTHING off-box: inline CSS only,
and the JS is a SAME-ORIGIN file (``/scribe/app.js``), not a CDN. The server
sends a strict CSP (:data:`CSP_VALUE`). Note ``script-src`` inherits
``default-src 'self'`` (NO ``'unsafe-inline'`` for scripts), so an INLINE
``<script>`` would be CSP-blocked — the PWA logic MUST be the external
same-origin file, and the ingest token is handed to it via a DOM ``data-``
attribute (read by the external JS), never an inline script.

R5 (no PHI in browser storage) — the JS is memory-only: capture → POST → discard
the blob. NO localStorage / IndexedDB / Cache-API / service-worker. No offline
caching. (Statically pinned in the Slice-B tests.)

R6 (client label) — the JS mints ``enc-<13-digit Date.now()>-<16 hex crypto
nonce>`` per "Start encounter" (must fullmatch the backend's
``^enc-[0-9]{13}-[0-9a-f]{16}$``). There is NO patient-name/DOB/MRN field
anywhere in the UI.

B2 (the client trap) — a FRESH ``MediaRecorder`` PER WINDOW (~20s): each window
is start→stop→one complete ``ondataavailable`` blob that is INDEPENDENTLY
decodable. NEVER ``MediaRecorder.start(timeslice)`` (blobs 2..N would be
headerless clusters the chunk-by-chunk STT can't decode).

Serial-in-flight — chunks POST strictly serially per encounter (a promise chain;
each ``await``ed before the next); a network error retries the same seq; a 409 on
a retry (a possibly-lost 200) is treated as "already accepted → advance seq".

THE PAGE-LOAD AUTH SPLIT (option A, loopback single-operator) — the page GET is a
browser navigation that cannot carry a bearer, so the static routes are
Host-pinned + loopback-asserted but bearer-EXEMPT, and the ingest token is
EMBEDDED in the served page for its JS. Rebind-safe: a DNS-rebind request carries
the attacker domain as ``Host`` → the Host-pin refuses it (421); a cross-origin
``fetch`` to 127.0.0.1 gets an opaque response (no CORS) so the attacker JS can't
read the token-bearing HTML. The token never leaves the box; anyone who can load
the page is already on 127.0.0.1 — the same trust boundary the token protects.
"""

from __future__ import annotations

import html

# The strict CSP the server sends on the page (and the JS). ``connect-src 'self'``
# makes the BROWSER itself refuse any off-box fetch even if the page were tampered
# — a second belt under the sovereign no-egress boundary. ``script-src`` inherits
# ``default-src 'self'`` (no inline scripts). ``base-uri 'none'`` + ``form-action
# 'none'`` kill base-tag hijack + form exfil. ``frame-ancestors 'none'`` refuses
# framing (clickjacking hardening — a framed page can't be tricked into toggling
# an on-box recording; SOP already makes the frame's DOM/token unreadable).
CSP_VALUE = (
    "default-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

# The token placeholder in the HTML template — replaced (HTML-attribute-escaped)
# at serve time with the configured ingest token.
_TOKEN_PLACEHOLDER = "__INGEST_TOKEN__"

_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>STAY-C Scribe</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 34rem; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; }
  button { font-size: 1.1rem; padding: 0.7rem 1.4rem; margin: 0.3rem 0.3rem 0.3rem 0; border-radius: 0.5rem; }
  #start { background: #1a7f37; color: #fff; border: 0; }
  #stop { background: #b32424; color: #fff; border: 0; }
  button:disabled { opacity: 0.45; }
  #status { margin-top: 1rem; padding: 0.8rem; border: 1px solid #8888; border-radius: 0.5rem; min-height: 3rem; white-space: pre-wrap; font-variant-numeric: tabular-nums; }
  .note { color: #888; font-size: 0.85rem; margin-top: 1.2rem; }
</style>
</head>
<body data-ingest-token="__INGEST_TOKEN__">
<h1>STAY-C Scribe &mdash; loopback</h1>
<p class="note">Synthetic-only. Loopback single-operator. No patient identifiers &mdash; the encounter id is a machine token.</p>
<button id="start">Start encounter</button>
<button id="stop" disabled>Stop &amp; finish</button>
<div id="status" aria-live="polite">Idle. Press &ldquo;Start encounter&rdquo; to begin.</div>
<p class="note">Audio is captured in ~20s windows, each a self-contained file, and pushed to this on-box server. Nothing is stored in the browser; nothing leaves 127.0.0.1.</p>
<script src="/scribe/app.js"></script>
</body>
</html>
"""

# The PWA logic — a SAME-ORIGIN external file (CSP-required; see module docstring).
# Reads the ingest token from the page's data attribute. Browser-gated for full
# e2e (Playwright/#54); the Slice-B unit tests pin its structural contracts
# (label shape, per-window recorder, serial/409, no-storage).
APP_JS = r"""'use strict';
(function () {
  const TOKEN = (document.body && document.body.dataset && document.body.dataset.ingestToken) || '';
  const WINDOW_MS = 20000;          // ~20s window: B2 boundary word-clip amortized
  const EXT = 'webm';               // Chrome/Firefox opus-in-webm (backend ext set)
  const MIME = 'audio/webm';
  const MAX_ATTEMPTS = 6;

  let stream = null;
  let recording = false;
  let label = null;
  let seq = 0;                      // last successfully-accepted seq
  let recorder = null;
  let windowTimer = null;
  let chain = Promise.resolve();    // serial-in-flight: strict per-encounter chain
  let stopped = false;              // latched once the encounter is halted (manual or terminal)

  const startBtn = document.getElementById('start');
  const stopBtn = document.getElementById('stop');
  const statusEl = document.getElementById('status');

  function show(msg) { statusEl.textContent = msg; }        // NON-PHI text only
  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  // R6 — machine token label: enc-<13-digit ms epoch>-<16 hex crypto nonce>.
  // Must fullmatch ^enc-[0-9]{13}-[0-9a-f]{16}$. No patient identifier feeds it.
  function newLabel() {
    const ts = Date.now().toString();                       // 13 digits
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);                          // 8 bytes -> 16 hex
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return 'enc-' + ts + '-' + hex;
  }

  // Serial POST of one self-contained window blob. Retries the SAME seq on a
  // network/5xx error; treats a 409 (a retry after a possibly-lost 200) as
  // 'already accepted -> advance'. Returns true iff the seq is now accepted, or
  // false on a TERMINAL 4xx (413 cap / 403 / 401 / 400) or exhausted retries —
  // the CALLER then actually stops (N1: recording must not keep hammering the
  // same seq against a cap). The encounter is closed by the dedicated
  // /scribe/close (robust even if the final chunk's 200 was lost), never a
  // close-flag on a chunk that might itself be the lost one. /scribe/close ALSO
  // ASSERTS the final seq (#57) so the server finalizes READY only once all seqs
  // 1..final_seq have folded.
  async function postChunk(blob, chunkSeq) {
    const params = new URLSearchParams({ label: label, seq: String(chunkSeq), ext: EXT, synthetic: 'true' });
    const url = '/scribe/ingest-chunk?' + params.toString();
    for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
      try {
        const resp = await fetch(url, {
          method: 'POST',
          headers: { 'Authorization': 'Bearer ' + TOKEN },
          body: blob,
          cache: 'no-store',                                // R5: never cache
        });
        if (resp.status === 200) { return true; }
        if (resp.status === 409) { return true; }           // already accepted -> advance
        if (resp.status >= 400 && resp.status < 500) { return false; }  // TERMINAL 4xx -> caller stops
        // 5xx -> fall through to retry
      } catch (e) {
        // network error -> retry same seq (idempotent by content-hash server-side)
      }
      await sleep(400 * (attempt + 1));
    }
    return false;                                           // retries exhausted -> caller stops
  }

  // B2 — ONE fresh MediaRecorder per window. NO timeslice: start() with no arg
  // fires a single ondataavailable at stop() with the complete, decodable blob.
  function startWindow() {
    if (!recording || !stream) { return; }
    recorder = new MediaRecorder(stream, { mimeType: MIME });
    let blob = null;
    recorder.ondataavailable = (e) => { blob = e.data; };   // exactly one complete blob
    recorder.onstop = () => {
      const captured = blob;
      blob = null;                                          // R5: drop the reference (memory-only)
      if (captured && captured.size > 0 && !stopped) {      // don't enqueue after a stop (no stray post-close chunk)
        chain = chain.then(async () => {                    // serial: enqueue on the chain
          // seq is computed HERE (inside the chain), AFTER the prior chunk has
          // advanced it — NOT at onstop time. Recording windows are continuous,
          // so window N+1's onstop fires before window N's POST resolves; reading
          // seq at onstop time would collide both on seq (the backend seq check is
          // check-then-write across the await → duplicate seq = last-writer-wins).
          const chunkSeq = seq + 1;
          const ok = await postChunk(captured, chunkSeq);
          if (ok) { seq = chunkSeq; }
          else { stopEncounter('Chunk ' + chunkSeq + ' rejected — recording stopped, encounter closed.'); }
        });
      }
      if (recording) { startWindow(); }                     // next window = a NEW recorder
    };
    recorder.start();                                       // <-- NO timeslice argument (B2)
    windowTimer = setTimeout(() => {
      if (recorder && recorder.state === 'recording') { recorder.stop(); }
    }, WINDOW_MS);
  }

  async function pollStatus() {
    while (recording) {
      try {
        const resp = await fetch('/scribe/status?label=' + encodeURIComponent(label), {
          headers: { 'Authorization': 'Bearer ' + TOKEN }, cache: 'no-store',
        });
        if (resp.ok) {
          const s = await resp.json();                      // NON-PHI: {chunks, state, ...}
          show('Recording. chunks=' + s.chunks + ' state=' + s.state);
        }
      } catch (e) { /* transient — keep going */ }
      await sleep(3000);
    }
  }

  async function start() {
    startBtn.disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      show('Microphone unavailable.'); startBtn.disabled = false; return;
    }
    label = newLabel();
    seq = 0;
    recording = true;
    stopped = false;
    chain = Promise.resolve();
    stopBtn.disabled = false;
    show('Recording. chunks=0 state=recording');
    startWindow();
    pollStatus();
  }

  // N1 — halt recording + finalize. SHARED by the manual Stop button AND a
  // terminal chunk failure (a 4xx cap/reject or exhausted retries). Idempotent
  // (latched by `stopped`). Enqueues the /scribe/close on the chain but does NOT
  // await it here, so it is safe to call from INSIDE a chain link (awaiting the
  // chain from within a chain link would self-deadlock). The manual Stop handler
  // awaits the chain separately to reflect the final state.
  function stopEncounter(msg) {
    if (stopped) { return; }
    stopped = true;
    recording = false;
    clearTimeout(windowTimer);
    try { if (recorder && recorder.state === 'recording') { recorder.stop(); } } catch (e) {}
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    stopBtn.disabled = true;
    // Drain every queued chunk POST, THEN an explicit /scribe/close ASSERTING the
    // final seq (#57 "ready ⇒ complete"). This link runs AFTER every chunk link
    // (each does `if(ok){seq=chunkSeq;}`), so `seq` here == the last accepted seq ==
    // this client's asserted final (the read-seq-inside-the-chain discipline). The
    // `finalSeq >= 1` guard keeps a zero-chunk start→immediate-stop on the legacy
    // empty-close path (the server's >=1 check never 400s a legitimate empty close).
    chain = chain.then(async () => {
      const finalSeq = seq;
      let u = '/scribe/close?label=' + encodeURIComponent(label);
      if (finalSeq >= 1) { u += '&final_seq=' + String(finalSeq); }
      try {
        await fetch(u, {
          method: 'POST', headers: { 'Authorization': 'Bearer ' + TOKEN }, cache: 'no-store',
        });
      } catch (e) { /* operator can re-close from status if needed */ }
      show(msg || 'Finished. Encounter closed.');
      startBtn.disabled = false;
    });
  }

  async function stop() {
    // Stop spawning new windows FIRST (so the flushed window's onstop, which sees
    // `stopped` still false, enqueues its final chunk but does NOT start another).
    recording = false;
    clearTimeout(windowTimer);
    if (recorder && recorder.state === 'recording') {
      await new Promise((res) => {
        const prev = recorder.onstop;
        recorder.onstop = () => { prev(); res(); };
        recorder.stop();
      });
    }
    stopEncounter('Finished. Encounter closed.');           // latch + enqueue close
    await chain;                                            // drain queued POSTs + the close
  }

  startBtn.addEventListener('click', start);
  stopBtn.addEventListener('click', stop);
})();
"""


def render_index(token: str) -> str:
    """Return the PWA index HTML with the ingest ``token`` embedded (HTML-attribute
    escaped) in the ``data-ingest-token`` attribute for the same-origin JS to read.

    The token is operator config (trusted), but it is escaped defensively so a
    token containing a quote/angle-bracket can never break out of the attribute."""
    return _INDEX_HTML.replace(_TOKEN_PLACEHOLDER, html.escape(token, quote=True))
