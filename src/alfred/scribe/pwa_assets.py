"""The STAY-C loopback PWA client assets (#49 Slice B + P4-5 enrollment UI).

A MINIMAL, single-operator, loopback-only PWA the clinician loads on-box (or via an
SSH/WG tunnel to 127.0.0.1). TWO hash-routed views in ONE page — no new server, port,
or CSP surface:

  * ``#/record``  (default) — encounter capture + the voice-PRESET PICKER + the
    ``preset_fit`` chip.
  * ``#/presets``           — the presets list (rename / re-record / delete) and the
    guided ENROLLMENT wizard.

Contract-bound to ``docs/scribe_enroll_api.md`` (the frozen wire contract).

R4 (zero external resources) — the page loads NOTHING off-box: inline CSS only, and the
JS is a SAME-ORIGIN file (``/scribe/app.js``), not a CDN. ``script-src`` inherits
``default-src 'self'`` (NO ``'unsafe-inline'``), so an INLINE ``<script>`` would be
CSP-blocked — the PWA logic MUST be the external same-origin file, and the ingest token
is handed to it via a DOM ``data-`` attribute, never an inline script.

R5 (no PHI / no secrets in browser storage) — MEMORY-ONLY. NO localStorage /
sessionStorage / IndexedDB / Cache-API / service-worker. The ENROLL token is pasted
once per page-load and lives ONLY in a closure variable; a reload asks again. This is
also why the preset picker's default (MRU) is SERVER-derived (``mru_preset_id``) — the
client is not allowed to remember it.

STANDALONE INSTALL (Task #1) — the page ships a Web App Manifest + icons so Chrome ≥108
installs it as a standalone app (no URL bar) from the MANIFEST ALONE. This adds NO
service worker, NO Cache-API, NO storage of any kind: it is chrome (the install shell),
not persistence. Offline support is DELIBERATELY absent — you cannot record to a server
you cannot reach, and audio must never buffer on-device. The manifest + icons + favicon
are STATIC and SECRET-FREE (unlike the index page, which embeds the ingest token); the
no-residue posture above is preserved intact.

R6 (no patient identifiers) — the encounter label is a MACHINE token minted client-side
(``enc-<13-digit ms>-<16 hex nonce>``); NO DOM value feeds it. The record view has NO
free-text field at all. The only free-text inputs in the page are (a) the enroll-token
paste (``type=password``, memory-only) and (b) the voiceprint NAME, which is enrollment
metadata — never the label, never logged (the backend pins names out of logs + audit) —
and which carries the memo's "name the place and mic, not a patient" guidance.

THE TWO-TOKEN SPLIT — the INGEST token is embedded in the page (its JS needs it for
chunks/close/status/binding). The ENROLL token is NEVER embedded: page possession must
not grant biometric mutation. The clinician pastes it once per session.

B2 (the client trap) — a FRESH ``MediaRecorder`` PER WINDOW: each window is
start→stop→one complete ``ondataavailable`` blob that is INDEPENDENTLY decodable. NEVER
``MediaRecorder.start(timeslice)`` (blobs 2..N would be headerless clusters).

MUTUAL EXCLUSION (a CONSENT property, not a mic-contention nicety) — an enrolment window
that captures a live patient encounter is folded into a PERMANENT biometric centroid, on a
surface whose entire consent basis is "the enrolling clinician's own voice". Three
mechanisms enforce it, and all are needed:
  * the MIC CLAIM (``micOwner``) is taken SYNCHRONOUSLY at each point the microphone is
    actually ACQUIRED — ``start()`` and ``captureEnroll()`` — because ``recording`` and
    ``enrollSession`` are only set AFTER an await, so a guard reading them alone is a
    check-then-act across a world-changing await.
  * the GENERATION TOKEN (``enrollGen``) makes the claim ENFORCEABLE across ``captureEnroll``'s
    own awaits. The claim is synchronous, but the teardown HANDLE (``enrollHalt``) is not
    registered until after two awaits; a ``route()`` in that window would otherwise let the
    continuation resume behind a hidden view (a live capture, bytes never abandoned, ``micOwner``
    stuck → the encounter path DoS'd). ``teardownEnroll`` bumps the token; ``captureEnroll``
    and ``finalizeEnroll`` bail after each await if their generation is stale.
  * ``route()`` TEARS DOWN the enrolment wizard on every view change. A wizard that is
    merely STAGED (intro rendered, its [Start recording] listener live) otherwise survives a
    navigation, and an encounter started in the meantime composes — by ordinary navigation,
    no race — into exactly the violation above.
The general rule: anything that can be STAGED and then FIRED must re-check where the
resource is ACQUIRED, never only where the user expressed the intention — and a claim taken
before an await needs its RELEASE reachable from every path that await can be interrupted by.

DEVICE CONTAINERS (operator ruling: the phone is an iPhone) — we do NOT hardcode webm.
The recorder negotiates a supported type and we send what the device actually produced:
  * ENROLLMENT — the server SNIFFS the container (webm EBML / mp4 ``ftyp``); ``ext`` is
    ignored, so iOS ``audio/mp4`` works as-is.
  * ENCOUNTER — the ingest route validates ``ext`` against a frozen allowlist that has
    no ``mp4``. iOS emits ``audio/mp4``, which is AAC-in-MP4 — whose CONVENTIONAL
    extension is ``m4a``, and ``m4a`` IS on the allowlist, IS swept, and IS decoded
    (ffmpeg/whisper sniff by content, not by name). So ``audio/mp4`` → ``ext=m4a``. This
    honours the iPhone ruling WITHOUT reopening the frozen #49 ext contract.
"""

from __future__ import annotations

import html
import json
import struct
import zlib

# The theme/splash colours — the SINGLE source of truth for both the manifest and the
# page's ``<meta name="theme-color">`` (baked into the HTML by ``render_index``), so the
# two can never drift. ``THEME_COLOR`` is the app's primary green (matches the record-view
# primary button); ``BACKGROUND_COLOR`` is the standalone splash background.
THEME_COLOR = "#1a7f37"
BACKGROUND_COLOR = "#ffffff"

# The strict CSP the server sends on the page (and the JS). ``connect-src 'self'`` makes
# the BROWSER itself refuse any off-box fetch even if the page were tampered — a second
# belt under the sovereign no-egress boundary. ``script-src`` inherits ``default-src
# 'self'`` (no inline scripts). ``base-uri 'none'`` + ``form-action 'none'`` kill
# base-tag hijack + form exfil. ``frame-ancestors 'none'`` refuses framing.
CSP_VALUE = (
    "default-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)

_TOKEN_PLACEHOLDER = "__INGEST_TOKEN__"
_CLINICIANS_PLACEHOLDER = "__CLINICIANS_JSON__"
_THEME_COLOR_PLACEHOLDER = "__THEME_COLOR__"
# The install-asset link hrefs are BAKED from the route constants (below) via render_index,
# so a route rename propagates to the served page — the browser-facing literal can never
# drift from the registered route (QA fix round, findings 4/9).
_MANIFEST_ROUTE_PLACEHOLDER = "__MANIFEST_ROUTE__"
_FAVICON_ROUTE_PLACEHOLDER = "__FAVICON_ROUTE__"
_APPLE_TOUCH_ICON_ROUTE_PLACEHOLDER = "__APPLE_TOUCH_ICON_ROUTE__"
_BUG_MAX_PLACEHOLDER = "__BUG_MAX_PER_SESSION__"

_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="__THEME_COLOR__">
<link rel="manifest" href="__MANIFEST_ROUTE__">
<link rel="icon" href="__FAVICON_ROUTE__" sizes="any">
<link rel="apple-touch-icon" href="__APPLE_TOUCH_ICON_ROUTE__">
<title>STAY-C Scribe</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 34rem; margin: 1.5rem auto; padding: 0 1rem 4rem; }
  h1 { font-size: 1.25rem; }
  h2 { font-size: 1.05rem; margin-top: 1.4rem; }
  nav { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
  nav a { flex: 1; text-align: center; padding: 0.55rem; border: 1px solid #8886; border-radius: 0.5rem; text-decoration: none; color: inherit; }
  nav a.on { background: #8882; font-weight: 600; }
  button { font-size: 1.05rem; padding: 0.7rem 1.2rem; margin: 0.3rem 0.3rem 0.3rem 0; border-radius: 0.5rem; border: 1px solid #8886; background: #8881; color: inherit; }
  button.primary { background: #1a7f37; color: #fff; border: 0; }
  button.danger { background: #b32424; color: #fff; border: 0; }
  button:disabled { opacity: 0.45; }
  select, input, textarea { font-size: 1.05rem; padding: 0.6rem; border-radius: 0.5rem; border: 1px solid #8886; width: 100%; box-sizing: border-box; background: transparent; color: inherit; font-family: inherit; }
  label { display: block; margin: 0.8rem 0 0.3rem; font-size: 0.9rem; color: #888; }
  .bug-link { background: transparent; border: 0; color: #888; text-decoration: underline; font-size: 0.85rem; padding: 0.4rem 0; margin-top: 1.2rem; }
  #status { margin-top: 1rem; padding: 0.8rem; border: 1px solid #8888; border-radius: 0.5rem; min-height: 3rem; white-space: pre-wrap; font-variant-numeric: tabular-nums; }
  .note { color: #888; font-size: 0.85rem; margin-top: 1rem; }
  .chip { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 1rem; font-size: 0.8rem; border: 1px solid #8886; }
  .banner { padding: 0.7rem; border-radius: 0.5rem; border: 1px solid #c90; background: #fd06; margin: 0.7rem 0; font-size: 0.9rem; }
  .badge { font-size: 0.75rem; padding: 0.1rem 0.45rem; border-radius: 0.4rem; border: 1px solid #8886; margin-left: 0.3rem; }
  .row { border: 1px solid #8886; border-radius: 0.5rem; padding: 0.7rem; margin: 0.5rem 0; }
  .ring { font-size: 2rem; font-variant-numeric: tabular-nums; margin: 0.6rem 0; }
  .hide { display: none; }
</style>
</head>
<body data-ingest-token="__INGEST_TOKEN__" data-clinicians="__CLINICIANS_JSON__" data-bug-max="__BUG_MAX_PER_SESSION__">
<h1>STAY-C Scribe &mdash; loopback</h1>
<nav>
  <a id="nav-record" href="#/record">Record</a>
  <a id="nav-presets" href="#/presets">Voice presets</a>
</nav>

<section id="view-record">
  <p class="note">Synthetic-only. Loopback single-operator. No patient identifiers &mdash; the encounter id is a machine token.</p>
  <label for="who">Clinician</label>
  <select id="who"></select>
  <label for="picker">Voice preset (who is speaking)</label>
  <select id="picker"></select>
  <div id="preset-msg"></div>
  <p><span id="chip" class="chip">attribution: unarmed</span></p>
  <button id="start" class="primary">Start encounter</button>
  <button id="stop" disabled>Stop &amp; finish</button>
  <div id="status" aria-live="polite">Idle. Press &ldquo;Start encounter&rdquo; to begin.</div>
  <p class="note">Audio is captured in short self-contained windows and pushed to this on-box server. Nothing is stored in the browser; nothing leaves 127.0.0.1.</p>
  <p><button id="bug-open-record" class="bug-link">Report a problem</button></p>
</section>

<section id="view-presets" class="hide">
  <label for="who2">Clinician</label>
  <select id="who2"></select>
  <div id="presets-list"></div>
  <button id="new-preset" class="primary">Create a voiceprint (~1 min)</button>

  <div id="enroll" class="hide">
    <h2 id="enroll-title">Create a voiceprint</h2>
    <div id="enroll-body"></div>
  </div>
  <p class="note">A voiceprint lets the scribe tell who is speaking. Audio is deleted the moment the voiceprint is made &mdash; only the numbers are kept, and they never leave this machine. Engine updates will ask you to re-record.</p>
  <p><button id="bug-open-presets" class="bug-link">Report a problem</button></p>
</section>

<!-- Bug report — a SHARED panel (outside both view sections so the record view keeps NO
     free-text field). Opened from either view's "Report a problem" affordance. R5-clean: the
     form is memory-only; nothing is stored, and the free-text carries the PHI-caution banner. -->
<section id="bug" class="hide">
  <h2>Report a problem</h2>
  <div class="banner">Please do <b>not</b> include patient details &mdash; no names, dates of birth, or health information. This goes to the technical team only.</div>
  <label for="bug-summary">What went wrong? (short)</label>
  <input id="bug-summary" maxlength="200" autocomplete="off">
  <label for="bug-detail">Any more detail? (optional)</label>
  <textarea id="bug-detail" rows="4" maxlength="4000"></textarea>
  <p class="note">A short technical snapshot (which screen, whether a voiceprint exists, recent taps &mdash; never any recording or patient content) is attached to help debugging.</p>
  <button id="bug-send" class="primary">Send report</button>
  <button id="bug-cancel">Cancel</button>
  <div id="bug-msg" aria-live="polite"></div>
</section>

<script src="/scribe/app.js"></script>
</body>
</html>
"""

# The PWA logic — a SAME-ORIGIN external file (CSP-required; see module docstring).
APP_JS = r"""'use strict';
(function () {
  // ── config from the page (the ENROLL token is deliberately NOT here) ──────────
  const TOKEN = (document.body && document.body.dataset && document.body.dataset.ingestToken) || '';
  let CLINICIANS = [];
  try { CLINICIANS = JSON.parse(document.body.dataset.clinicians || '[]') || []; } catch (e) { CLINICIANS = []; }
  // client-side per-session bug-report cap (from config, embedded in the page). A stuck client
  // must not fill the disk; the server ALSO backstops with max_open_reports (429).
  let BUG_MAX_PER_SESSION = 10;
  try { BUG_MAX_PER_SESSION = parseInt(document.body.dataset.bugMax, 10) || 10; } catch (e) { BUG_MAX_PER_SESSION = 10; }
  let bugSubmitCount = 0;            // MEMORY-ONLY (dies on reload — same no-storage posture)

  const WINDOW_MS = 20000;           // encounter window (~20s) — B2 boundary amortized
  const ENROLL_WINDOW_MS = 15000;    // enrollment window (~15s) — memo B2 discipline
  const ENROLL_TARGET_MS = 45000;    // ~45s of speech clears the 10s HARD gate + nears 30s advisory
  const MAX_ATTEMPTS = 6;

  // ── R5: MEMORY-ONLY. No localStorage/sessionStorage/IndexedDB/SW anywhere. ─────
  let enrollToken = '';              // pasted once per page-load; a reload asks again
  let user = '';                     // selected clinician (a scribe.clinicians slug)
  let sessionToken = '';             // #12 12b: server-issued identity session (MEMORY-ONLY, R5)
  let selectedPreset = '';           // '' == "No preset — attribution off" (first-class)
  let presetsCache = [];
  let mruPresetId = null;
  let serverState = '';              // 'empty' | 'all_incompatible' | 'ok' | 'inert' (see loadPresets)
  let enrollSession = null;          // non-null while an enrolment session holds RAM bytes
  let enrollHalt = null;             // non-null while an enrolment CAPTURE holds the mic
  let enrollGen = 0;                  // bumped by captureEnroll entry AND by teardownEnroll
  let pendingToken = null;           // resolve() of an OPEN token-paste prompt
  let micLabel = '';                 // the device label, for the name prefill

  // THE MIC CLAIM — '' | 'encounter' | 'enroll'. Claimed SYNCHRONOUSLY, before the
  // getUserMedia await, by whichever path is about to ACQUIRE the microphone.
  //
  // `recording` / `enrollSession` are both set AFTER an await (getUserMedia, /enroll/start),
  // so a check on them is a check-then-act across a world-changing await: both paths could
  // pass their own guard while the other is mid-acquisition. The claim closes that window,
  // and — with the action-moment guards below — is what actually enforces the memo's
  // "enrolment and encounter recording are mutually exclusive".
  //
  // OWNERSHIP DISCIPLINE (load-bearing for the generation token below): a flow may clear
  // micOwner ONLY while it is still the CURRENT owner. captureEnroll claims it synchronously
  // but registers its teardown handle (enrollHalt) only after two awaits; if a teardown lands
  // in that window it becomes the owner. So a stale captureEnroll continuation stops its OWN
  // mic stream but must NEVER write micOwner — teardown (or a newer enrolment) owns it now.
  let micOwner = '';

  // encounter state
  let stream = null, recording = false, label = null, seq = 0, recorder = null;
  let windowTimer = null, chain = Promise.resolve(), stopped = false;

  const $ = (id) => document.getElementById(id);
  const statusEl = $('status');
  function show(msg) { statusEl.textContent = msg; }            // NON-PHI text only
  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  // ── R5 diagnostic ring buffer — MEMORY-ONLY, last ~20 UI breadcrumbs + JS errors ──────
  // Dies on reload (no storage — same posture as the rest of the client). PHI-FREE by
  // construction: code-path traces + HTTP status codes, NEVER transcript/audio/patient
  // content. Attached to a bug report so a dead-button-class bug is diagnosable from the
  // trace ("tap create-voiceprint -> runEnroll -> blocked: no clinician") instead of free
  // text alone (the 2026-07-16 incident). Capped so it can never grow unbounded.
  const BUG_RING_MAX = 20;
  const bugRing = [];
  function logEvent(msg) {
    try {
      const t = new Date().toISOString().slice(11, 19);         // HH:MM:SS — no date, no PHI
      bugRing.push(t + ' ' + String(msg).slice(0, 200));
      while (bugRing.length > BUG_RING_MAX) { bugRing.shift(); }
    } catch (e) { /* the ring must NEVER break the app */ }
  }
  window.addEventListener('error', (e) => {
    logEvent('jserror ' + ((e && e.message) ? String(e.message).slice(0, 120) : 'unknown'));
  });
  // ATTRIBUTE-SAFE escaping. The old version used the textContent->innerHTML trick, which
  // escapes & < > but NOT quotes — and every use site here interpolates into an ATTRIBUTE
  // (<option value="...">, data-id="..."). A value containing a double quote would break
  // out of the attribute. Escape quotes explicitly.
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // R6 — machine token label: enc-<13-digit ms epoch>-<16 hex crypto nonce>.
  // NO DOM value feeds this. Must fullmatch ^enc-[0-9]{13}-[0-9a-f]{16}$.
  function newLabel() {
    const ts = Date.now().toString();                           // 13 digits
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);                              // 8 bytes -> 16 hex
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return 'enc-' + ts + '-' + hex;
  }

  // ── device containers — do NOT hardcode webm (the phone is an iPhone) ──────────
  function pickMime() {
    const cands = ['audio/webm', 'audio/mp4', 'audio/ogg'];
    for (let i = 0; i < cands.length; i++) {
      if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(cands[i])) {
        return cands[i];
      }
    }
    return '';                                                  // let the browser choose
  }
  // Map the ACTUAL negotiated mimeType -> an ext the ingest allowlist accepts.
  // iOS Safari emits audio/mp4 (AAC). 'mp4' is NOT on the frozen allowlist, but 'm4a'
  // IS — and m4a IS AAC-in-MP4 (decoders sniff by content, not by name).
  function extFor(mime) {
    const m = String(mime || '').toLowerCase();
    if (m.indexOf('webm') >= 0) { return 'webm'; }
    if (m.indexOf('mp4') >= 0 || m.indexOf('m4a') >= 0 || m.indexOf('aac') >= 0) { return 'm4a'; }
    if (m.indexOf('ogg') >= 0) { return 'ogg'; }
    return 'webm';
  }
  function newRecorder(s) {
    const mime = pickMime();
    return mime ? new MediaRecorder(s, { mimeType: mime }) : new MediaRecorder(s);
  }

  async function api(path, opts) {
    const o = opts || {};
    const headers = o.headers || {};
    headers['Authorization'] = 'Bearer ' + (o.enroll ? enrollToken : TOKEN);
    // #12 12b: carry the identity session on every ingest-class call so the sliding TTL stays
    // warm (design 2.2). An EXPLICIT X-Scribe-Session (session/close passes the old token) wins
    // over the closure var, so a teardown can send the token it is dropping.
    if (sessionToken && !headers['X-Scribe-Session']) { headers['X-Scribe-Session'] = sessionToken; }
    const resp = await fetch(path, { method: o.method || 'GET', headers: headers, body: o.body, cache: 'no-store' });
    // Ring only the FAILURES (status >= 400) — logging every poll would flood the 20-slot ring
    // and evict the useful breadcrumbs. The path is stripped of its query (no label/PHI).
    if (resp.status >= 400) { logEvent('api ' + String(path).split('?')[0] + ' ' + resp.status); }
    return resp;
  }

  // ── #12 12b: per-clinician identity session (server-issued, MEMORY-ONLY) ────────────────
  // Binds the loopback page to a config.clinicians slug so consent capture (slice 12c) can
  // attribute captured_by server-side. The token lives ONLY in the sessionToken closure var
  // above  no localStorage/sessionStorage/cookie/IndexedDB (R5, identical to enrollToken):
  // a reload drops it and the page re-opens a session.
  async function openSession(clin) {
    if (!clin) { sessionToken = ''; return false; }
    try {
      const r = await api('/scribe/session/open?user=' + encodeURIComponent(clin), { method: 'POST' });
      if (!r.ok) { sessionToken = ''; logEvent('session open ' + r.status); return false; }
      const j = await r.json();
      sessionToken = (j && j.session) ? j.session : '';
      return !!sessionToken;
    } catch (e) { sessionToken = ''; logEvent('session open err'); return false; }
  }
  async function closeSession() {
    const t = sessionToken;
    sessionToken = '';                 // clear FIRST so a concurrent re-bind cannot reuse it
    if (!t) { return; }
    try { await api('/scribe/session/close', { method: 'POST', headers: { 'X-Scribe-Session': t } }); }
    catch (e) { /* best-effort teardown  a failed close just lets the server TTL reap it */ }
  }
  // Re-bind identity atomically on a clinician switch: close the old session, open the new
  // (design 2.4). Fire-and-forget from the change handlers  the session lands before an
  // encounter starts; slice 12c's consent gate 401s + re-opens if it somehow has not.
  async function bindSession(clin) {
    await closeSession();
    if (clin) { await openSession(clin); }
  }

  // ══ RECORD VIEW ═══════════════════════════════════════════════════════════════

  async function loadPresets() {
    presetsCache = []; mruPresetId = null; serverState = '';
    if (!user) { return; }
    try {
      const r = await api('/scribe/presets?user=' + encodeURIComponent(user));  // EITHER token
      // 404 = the whole enrolment FACE is inert (enroll_token unset — the DEFAULT ship
      // posture). 'inert' is a CLIENT-side state, not a server enum value: the server
      // cannot answer `state` at all when the route 404s. Every surface that offers
      // enrolment reads it (renderPresetMsg / renderPresets / runEnroll) — an un-armed box
      // must not walk the clinician through a token paste and a mic prompt to die on a 404.
      if (r.status === 404) { serverState = 'inert'; return; }
      if (!r.ok) { return; }
      const j = await r.json();
      presetsCache = j.presets || [];
      mruPresetId = j.mru_preset_id || null;                    // SERVER-side MRU (R5)
      serverState = j.state || '';                              // empty | all_incompatible | ok
    } catch (e) { /* offline-ish: the picker just shows the no-preset choice */ }
  }

  function usablePresets() { return presetsCache.filter((p) => p.classification === 'usable'); }

  function renderPicker() {
    const sel = $('picker');
    const usable = usablePresets();
    let html = '<option value="">No preset &mdash; attribution off</option>';
    for (let i = 0; i < usable.length; i++) {
      const p = usable[i];
      html += '<option value="' + esc(p.preset_id) + '">' + esc(p.name || p.preset_id) + '</option>';
    }
    sel.innerHTML = html;
    // MRU pre-select (server-derived). Falls back to the no-preset choice.
    if (mruPresetId && usable.some((p) => p.preset_id === mruPresetId)) {
      sel.value = mruPresetId;
    } else {
      sel.value = '';
    }
    selectedPreset = sel.value;
    renderPresetMsg();
  }

  // INTENTIONALLY-LEFT-BLANK: the record view must NEVER go silent about attribution.
  // Every registry state renders an explicit signal. The rule is simply: if NOTHING is
  // usable, say so — whatever the cause. (The original code only caught the
  // engine-incompatible cause, so an all-REVOKED or all-CORRUPT registry fell through
  // BOTH branches to an empty message: the WORST state emitted LESS signal than the
  // empty one, and Jamie would record with no indication attribution was off.)
  function renderPresetMsg() {
    const el = $('preset-msg');
    const usable = usablePresets();
    if (usable.length > 0) { el.innerHTML = ''; return; }        // a usable preset exists

    // THE INERT BOX — the DEFAULT ship posture (enroll_token unset ⇒ the enrolment face
    // 404s). Offering "Create one" here is an invitation that CANNOT be honoured: it walks
    // the clinician through a token paste AND a mic-permission prompt before dying on a
    // 404. Say what is true; offer nothing. (Attribution is off, so the banner still fires
    // — intentionally-left-blank — but Start stays enabled: recording is unaffected.)
    if (serverState === 'inert') {
      el.innerHTML = '<div class="banner">Voice enrolment is not set up on this machine ' +
        '&mdash; the scribe will not label who is speaking. Ask your operator.</div>';
      return;
    }

    if (presetsCache.length === 0) {
      // ZERO presets — a NON-BLOCKING badge. Start STAYS enabled (no-preset is valid).
      el.innerHTML = '<div class="banner">No voiceprint yet &mdash; the scribe will not label who is ' +
        'speaking. <button id="go-create">Create one (~1 min)</button></div>';
      const go = $('go-create');
      if (go) { go.addEventListener('click', () => { location.hash = '#/presets'; }); }
      return;
    }

    // PRESETS EXIST BUT NONE ARE USABLE. Name the cause when we can; ALWAYS say
    // attribution is off. The server also flags this as state:"all_incompatible" —
    // empty vs all-unusable are DISTINCT states (docs/scribe_enroll_api.md).
    const stale = presetsCache.some((p) => p.classification === 'incompatible_engine' ||
                                           p.classification === 'incompatible_model');
    const why = stale
      ? 'Your voiceprint was made with an older engine, so attribution is off.'
      : 'None of your voiceprints can be used right now, so attribution is off.';
    el.innerHTML = '<div class="banner">' + why +
      ' <button id="go-rerecord">Re-record (~1 min)</button>' +
      ' <button id="continue-off">Continue &mdash; attribution off</button></div>';
    const go = $('go-rerecord');
    if (go) { go.addEventListener('click', () => { location.hash = '#/presets'; }); }
    const off = $('continue-off');
    if (off) { off.addEventListener('click', () => { el.innerHTML = ''; }); }
  }

  const CHIP_COPY = { unarmed: 'attribution: unarmed', ok: 'attribution: on',
                      warming: 'attribution: warming up', weak: 'attribution: weak',
                      none: 'attribution: off' };

  function setChip(fit) {
    // preset_fit is a 5-value enum (unarmed|warming|weak|none|ok); 5a emits only
    // unarmed|ok. FORWARD-TOLERATE: an UNKNOWN value must not break the chip (5b adds
    // warming/weak/none) — anything unrecognised reads as 'unarmed'. hasOwnProperty, not
    // `CHIP_COPY[fit] ||`: a value like "constructor" or "toString" would otherwise
    // resolve off the PROTOTYPE and render a function body into the chip.
    const ok = fit && Object.prototype.hasOwnProperty.call(CHIP_COPY, fit);
    $('chip').textContent = ok ? CHIP_COPY[fit] : CHIP_COPY.unarmed;
  }

  // Bind the chosen preset BEFORE the first chunk — the server LOCKS the binding at the
  // first chunk (a late bind is a 409 and would leave the note's provenance absent).
  // A binding failure must NEVER block Start: the encounter simply runs un-anchored.
  // TIMEOUT-BOUNDED: startWindow() sits behind this await, so a HUNG bind would stall the
  // whole encounter (audio lost). Losing attribution is survivable; losing the recording
  // is not — so the bind races a deadline and we record regardless.
  const BIND_TIMEOUT_MS = 4000;
  async function bindPreset() {
    if (!selectedPreset) { return; }
    const offMsg = 'Recording. (Voice preset could not be applied — attribution off.)';
    const q = '?label=' + encodeURIComponent(label) + '&preset=' + encodeURIComponent(selectedPreset);
    try {
      const timeout = new Promise((res) => setTimeout(() => res(null), BIND_TIMEOUT_MS));
      // INGEST token — the binding is an ENCOUNTER-class capability. Sending the ENROLL
      // token here is a 401 wrong_token_class and attribution silently never arms.
      const r = await Promise.race([api('/scribe/encounter/preset' + q, { method: 'POST' }), timeout]);
      if (!r || !r.ok) { show(offMsg); }
    } catch (e) {
      show(offMsg);
    }
  }

  async function postChunk(blob, chunkSeq, ext) {
    const params = new URLSearchParams({ label: label, seq: String(chunkSeq), ext: ext, synthetic: 'true' });
    const url = '/scribe/ingest-chunk?' + params.toString();
    for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
      try {
        const resp = await api(url, { method: 'POST', body: blob });
        if (resp.status === 200) { return true; }
        if (resp.status === 409) { return true; }               // already accepted -> advance
        if (resp.status >= 400 && resp.status < 500) { return false; }   // TERMINAL 4xx
      } catch (e) { /* network -> retry same seq */ }
      await sleep(400 * (attempt + 1));
    }
    return false;
  }

  // B2 — ONE fresh MediaRecorder per window. NO timeslice.
  function startWindow() {
    if (!recording || !stream) { return; }
    recorder = newRecorder(stream);
    const ext = extFor(recorder.mimeType);                      // the ACTUAL negotiated type
    let blob = null;
    recorder.ondataavailable = (e) => { blob = e.data; };
    recorder.onstop = () => {
      const captured = blob;
      blob = null;                                              // R5: drop the reference
      if (captured && captured.size > 0 && !stopped) {
        chain = chain.then(async () => {
          // seq is computed INSIDE the chain, AFTER the prior chunk advanced it.
          const chunkSeq = seq + 1;
          const ok = await postChunk(captured, chunkSeq, ext);
          if (ok) { seq = chunkSeq; }
          else { stopEncounter('Chunk ' + chunkSeq + ' rejected — recording stopped, encounter closed.'); }
        });
      }
      if (recording) { startWindow(); }
    };
    recorder.start();                                           // <-- NO timeslice (B2)
    windowTimer = setTimeout(() => {
      if (recorder && recorder.state === 'recording') { recorder.stop(); }
    }, WINDOW_MS);
  }

  async function pollStatus() {
    while (recording) {
      try {
        const resp = await api('/scribe/status?label=' + encodeURIComponent(label));
        if (resp.ok) {
          const s = await resp.json();                          // NON-PHI: {chunks, state, preset_fit}
          setChip(s.preset_fit);
          show('Recording. chunks=' + s.chunks + ' state=' + s.state);
        }
      } catch (e) { /* transient */ }
      await sleep(3000);
    }
  }

  async function start() {
    // MUTUAL EXCLUSION (memo: enrolment and encounter recording are mutually exclusive).
    // Two MediaRecorders on one mic, and — far worse — an enrolment buffer that captures
    // LIVE PATIENT SPEECH on a surface whose entire consent basis is "the enrolling
    // clinician's own voice". Refuse, loudly.
    logEvent('start encounter');
    if (enrollSession || micOwner) {
      logEvent('blocked: mic in use');
      show('Finish or cancel the voiceprint recording first — the microphone is in use.');
      return;
    }
    micOwner = 'encounter';               // claimed SYNCHRONOUSLY, BEFORE the await below
    $('start').disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      micOwner = '';
      show('Microphone unavailable.'); $('start').disabled = false; return;
    }
    label = newLabel();
    seq = 0; recording = true; stopped = false; chain = Promise.resolve();
    $('picker').disabled = true;                                // LOCKED while recording
    $('who').disabled = true;
    $('stop').disabled = false;
    show('Recording. chunks=0 state=recording');
    await bindPreset();                                         // BEFORE the first chunk
    startWindow();
    pollStatus();
  }

  function stopEncounter(msg) {
    if (stopped) { return; }
    stopped = true; recording = false;
    clearTimeout(windowTimer);
    try { if (recorder && recorder.state === 'recording') { recorder.stop(); } } catch (e) {}
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
    if (micOwner === 'encounter') { micOwner = ''; }             // release the mic claim
    $('stop').disabled = true;
    chain = chain.then(async () => {
      const finalSeq = seq;
      let u = '/scribe/close?label=' + encodeURIComponent(label);
      if (finalSeq >= 1) { u += '&final_seq=' + String(finalSeq); }
      try { await api(u, { method: 'POST' }); } catch (e) {}
      show(msg || 'Finished. Encounter closed.');
      $('start').disabled = false;
      $('picker').disabled = false;                             // unlock the picker
      $('who').disabled = false;
    });
  }

  async function stop() {
    recording = false;
    clearTimeout(windowTimer);
    if (recorder && recorder.state === 'recording') {
      await new Promise((res) => {
        const prev = recorder.onstop;
        recorder.onstop = () => { prev(); res(); };
        recorder.stop();
      });
    }
    stopEncounter('Finished. Encounter closed.');
    await chain;
  }

  // ══ PRESETS VIEW + ENROLLMENT WIZARD ══════════════════════════════════════════

  function badgeFor(p) {
    if (p.classification === 'usable') {
      const marginal = p.quality && p.quality.verdict === 'ok_marginal';
      return '<span class="badge">&check; active</span>' +
        (marginal ? '<span class="badge">&#9651; marginal quality</span>' : '');
    }
    if (p.classification === 'incompatible_engine' || p.classification === 'incompatible_model') {
      return '<span class="badge">&#8635; needs re-record &mdash; engine updated</span>';
    }
    if (p.classification === 'revoked') { return '<span class="badge">deleted</span>'; }
    return '<span class="badge">unreadable</span>';
  }

  async function renderPresets() {
    await loadPresets();
    const el = $('presets-list');
    // THE INERT BOX — hide the CREATE button outright. The face 404s; an un-armed box must
    // not invite an enrolment it cannot perform (see renderPresetMsg).
    const inert = serverState === 'inert';
    $('new-preset').classList.toggle('hide', inert);
    if (inert) {
      el.innerHTML = '<div class="banner">Voice enrolment is not set up on this machine. ' +
        'Ask your operator to arm it (an enrolment token).</div>';
      return;
    }
    if (!user) { el.innerHTML = '<p class="note">Select a clinician.</p>'; return; }
    if (presetsCache.length === 0) {
      el.innerHTML = '<p class="note">No voiceprints yet.</p>';   // intentionally-left-blank
      return;
    }
    let html = '';
    for (let i = 0; i < presetsCache.length; i++) {
      const p = presetsCache[i];
      const when = String(p.updated_at || p.created_at || '').slice(0, 10);
      html += '<div class="row"><b>' + esc(p.name || p.preset_id) + '</b> ' + badgeFor(p) +
        '<div class="note">' + esc(when) + '</div>' +
        '<button data-act="rename" data-id="' + esc(p.preset_id) + '">Rename</button>' +
        '<button data-act="rerecord" data-id="' + esc(p.preset_id) + '">Re-record</button>' +
        '<button class="danger" data-act="delete" data-id="' + esc(p.preset_id) + '">Delete</button>' +
        '</div>';
    }
    el.innerHTML = html;
    const btns = el.querySelectorAll('button[data-act]');
    for (let i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', (ev) => {
        const b = ev.currentTarget;
        onPresetAction(b.getAttribute('data-act'), b.getAttribute('data-id'));
      });
    }
  }

  async function needEnrollToken() {
    if (enrollToken) { return true; }
    // Paste-once, MEMORY-ONLY (R5). Never embedded in the page; never stored.
    $('enroll').classList.remove('hide');
    $('enroll-title').textContent = 'Enter the enrolment token';
    $('enroll-body').innerHTML =
      '<p class="note">Your operator gives you this once. It is kept in memory only &mdash; ' +
      'reloading this page will ask again.</p>' +
      '<label for="tok">Enrolment token</label>' +
      '<input id="tok" type="password" autocomplete="off" spellcheck="false">' +
      '<button id="tok-ok" class="primary">Continue</button>' +
      '<div id="tok-msg" class="note"></div>';
    return await new Promise((res) => {
      pendingToken = res;                                       // teardown can settle it
      $('tok-ok').addEventListener('click', () => {
        if (!pendingToken) { return; }                          // torn down by a route away
        const val = $('tok').value || '';
        // INTENTIONALLY-LEFT-BLANK — an EMPTY Continue must SAY so, not silently no-op.
        // Consuming pendingToken on empty would resolve false AND leave the form on-screen;
        // every later Continue click would then hit `!pendingToken` and do literally nothing
        // — a permanently dead button, the exact "button does nothing" this fix round kills.
        // So on empty: show a message, keep the prompt LIVE, and wait for a real paste.
        if (!val) {
          $('tok-msg').textContent = 'Enter the enrolment token to continue.';
          return;
        }
        pendingToken = null;
        enrollToken = val;
        $('tok').value = '';                                    // drop it from the DOM
        res(true);
      });
    });
  }

  async function onPresetAction(act, id) {
    if (!(await needEnrollToken())) { return; }
    if (act === 'rename') {
      $('enroll').classList.remove('hide');
      $('enroll-title').textContent = 'Rename voiceprint';
      $('enroll-body').innerHTML =
        '<label for="nm">Name &mdash; name the place and mic, not a patient</label>' +
        '<input id="nm" maxlength="64" autocomplete="off">' +
        '<button id="nm-ok" class="primary">Save</button>';
      $('nm-ok').addEventListener('click', async () => {
        const nm = $('nm').value || '';
        const q = '?user=' + encodeURIComponent(user) + '&preset=' + encodeURIComponent(id);
        await api('/scribe/presets/rename' + q, {
          method: 'POST', enroll: true, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: nm }),
        });
        $('enroll').classList.add('hide');
        renderPresets();
      });
      return;
    }
    if (act === 'delete') {
      const ok = window.confirm('Deletes this voiceprint from this machine. ' +
        'Notes already written are not affected.');
      if (!ok) { return; }
      const q = '?user=' + encodeURIComponent(user) + '&preset=' + encodeURIComponent(id);
      await api('/scribe/presets/delete' + q, { method: 'POST', enroll: true });
      renderPresets();
      return;
    }
    if (act === 'rerecord') { runEnroll(id); }
  }

  // TERMINAL DISCARD — drop the RAM-held voice bytes NOW rather than waiting out the
  // server's 10-minute TTL. RAM-only custody is the security centrepiece; leaving the
  // bytes resident is exactly what /enroll/abandon exists to prevent (and two abandoned
  // attempts would otherwise 429 the next start against the 2-session cap).
  async function abandonEnroll() {
    const s = enrollSession;
    enrollSession = null;
    if (!s) { return; }
    try { await api('/scribe/enroll/abandon?session=' + encodeURIComponent(s),
                    { method: 'POST', enroll: true }); } catch (e) { /* best-effort */ }
  }

  // MUTUAL EXCLUSION (memo) — never enrol while an encounter is recording: the enrolment
  // buffer would capture LIVE PATIENT SPEECH, and the whole consent basis of this surface
  // is "the enrolling clinician's own voice". Rendered from BOTH the intent moment
  // (runEnroll) and the ACTION moment (captureEnroll) — see captureEnroll's guard.
  function refuseEnrollDuringEncounter() {
    logEvent('blocked: encounter recording');
    $('enroll').classList.remove('hide');
    $('enroll-title').textContent = 'Not now';
    $('enroll-body').innerHTML = '<p>An encounter is recording. Stop it before making a ' +
      'voiceprint &mdash; the microphone can only do one at a time.</p>';
  }

  function refuseEnrollInert() {
    logEvent('blocked: enrollment inert');
    $('enroll').classList.remove('hide');
    $('enroll-title').textContent = 'Not available';
    $('enroll-body').innerHTML = '<p>Voice enrolment is not set up on this machine. ' +
      'Ask your operator.</p>';
  }

  // INTENTIONALLY-LEFT-BLANK — the "Create a voiceprint" button must NEVER be a silent
  // no-op. The old `if (!user) { return; }` did exactly that: with scribe.clinicians empty
  // (the live 2026-07-16 root cause), tapping Create did literally nothing. Say what is wrong
  // and who fixes it. This guard fires ONLY in the no-clinician-configured case: fillWho
  // auto-selects CLINICIANS[0] in EVERY non-empty case (sole AND multi-clinician), and no
  // picker <option> carries an empty value, so `user` is truthy from page-load onward. "A
  // clinician exists but none is selected" is therefore unreachable — there is no second
  // message on purpose (a dead branch would only invite the comment-lies trap).
  function refuseEnrollNoClinician() {
    logEvent('blocked: no clinician configured');
    $('enroll').classList.remove('hide');
    $('enroll-title').textContent = 'No clinician configured';
    $('enroll-body').innerHTML =
      '<p>No clinicians are configured on this machine &mdash; ask your operator.</p>';
  }

  // The guided enrolment: record-first, name-last (~45-60s).
  async function runEnroll(rerecordId) {
    logEvent('runEnroll rerecord=' + (rerecordId ? '1' : '0'));
    if (!user) { return refuseEnrollNoClinician(); }
    if (recording || micOwner) { return refuseEnrollDuringEncounter(); }
    // An INERT face 404s every enroll route. Refuse HERE — before the token paste and
    // before the mic prompt — rather than asking for both and then failing.
    if (serverState === 'inert') { return refuseEnrollInert(); }
    if (!(await needEnrollToken())) { return; }
    const box = $('enroll');
    box.classList.remove('hide');
    $('enroll-title').textContent = rerecordId ? 'Re-record voiceprint' : 'Create a voiceprint';
    $('enroll-body').innerHTML =
      '<p>Speak normally for about a minute &mdash; read anything, or just describe your day.</p>' +
      '<p class="note">Audio is deleted the moment the voiceprint is made &mdash; only the numbers ' +
      'are kept, and they never leave this machine. Engine updates will ask you to re-record.</p>' +
      '<button id="en-go" class="primary">Start recording</button>';
    $('en-go').addEventListener('click', () => captureEnroll(rerecordId));
  }

  async function captureEnroll(rerecordId) {
    // ── THE GUARD BELONGS AT THE ACTION MOMENT, NOT THE INTENT MOMENT ──────────────
    // runEnroll()'s check fires when the enrolment SCREEN opens. The microphone opens
    // HERE — arbitrarily later, and the staged "Start recording" listener stays live in
    // between. Ordinary navigation composes into a consent violation with no race at all:
    //   #/presets → "Create a voiceprint" (intro STAGED, en-go wired, `recording` false)
    //   → #/record → Start (encounter LIVE) → back to #/presets → tap the staged button.
    // Without a re-check here that opens a SECOND recorder on the live patient mic and
    // pushes those windows into a PERMANENT biometric centroid. The route() teardown below
    // destroys the staged DOM, and this guard is the belt that holds if it ever regresses.
    // GENERAL RULE: anything that can be STAGED and then FIRED must re-check where the
    // resource is ACQUIRED, not where the user expressed the intention.
    if (recording || micOwner) { return refuseEnrollDuringEncounter(); }
    micOwner = 'enroll';                  // claimed SYNCHRONOUSLY, BEFORE the await below
    // THE GENERATION TOKEN — the SECOND half of the action-moment discipline, and the reason
    // the mic claim above is actually enforceable. micOwner is claimed synchronously, but the
    // ONLY handle teardownEnroll can stop this flow through (enrollHalt) is not registered
    // until AFTER the two awaits below. A route() firing in that window (the OS mic-permission
    // prompt holds getUserMedia for SECONDS on first run — wide open) would otherwise let this
    // continuation resume behind a hidden view: a second recorder on the live patient mic, the
    // RAM bytes never abandoned, and micOwner stuck so the patient-recording path is DoS'd
    // ("cancel the voiceprint" — but there is none to cancel). teardownEnroll bumps enrollGen;
    // after EACH await we bail if our generation is stale.
    const myGen = ++enrollGen;
    let st;
    try {
      st = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      if (myGen !== enrollGen) { return; }   // torn down mid-prompt: not ours to release or render
      micOwner = '';
      $('enroll-body').innerHTML = '<p>Microphone unavailable.</p>' +
        '<button id="en-retry" class="primary">Record again</button>';
      $('en-retry').addEventListener('click', () => runEnroll(rerecordId));
      return;
    }
    // GEN CHECK #1 — teardown fired while getUserMedia held the await. Stop OUR stream; do NOT
    // touch micOwner (teardown released it, or a newer enrolment owns it — OWNERSHIP DISCIPLINE).
    if (myGen !== enrollGen) { st.getTracks().forEach((t) => t.stop()); return; }
    const dropMic = () => { st.getTracks().forEach((t) => t.stop()); micOwner = ''; };
    // /enroll/start — validates the clinician BEFORE any recording (never a wasted 45s).
    let q = '?user=' + encodeURIComponent(user);
    if (rerecordId) { q += '&preset=' + encodeURIComponent(rerecordId); }
    let session = null, startOk = false, startStatus = 0, startThrew = false;
    try {
      const r = await api('/scribe/enroll/start' + q, { method: 'POST', enroll: true });
      startOk = r.ok; startStatus = r.status;
      if (r.ok) { session = (await r.json()).session; }
    } catch (e) {
      startThrew = true;
    }
    // GEN CHECK #2 — teardown fired during /enroll/start. The server may already hold bytes for
    // `session`; ABANDON them (a RECORDING session is safe — the finalizing-session carve-out
    // does not apply here) and stop our stream. Same OWNERSHIP DISCIPLINE: never touch micOwner,
    // never render, never start a window.
    if (myGen !== enrollGen) {
      st.getTracks().forEach((t) => t.stop());
      if (session) {
        try { await api('/scribe/enroll/abandon?session=' + encodeURIComponent(session),
                        { method: 'POST', enroll: true }); } catch (e) { /* best-effort */ }
      }
      return;
    }
    if (startThrew) { dropMic(); return enrollFailed(0, rerecordId); }
    if (!startOk) { dropMic(); return enrollFailed(startStatus, rerecordId); }
    enrollSession = session;                 // RAM bytes are now held server-side (still current)
    // the mic's own label is the memo's name prefill ("name the place and mic").
    try {
      const tr = st.getAudioTracks ? st.getAudioTracks()[0] : null;
      micLabel = (tr && tr.label) ? String(tr.label) : '';
    } catch (e) { micLabel = ''; }

    $('enroll-body').innerHTML =
      '<p><b>Keep this screen open.</b></p><div class="ring" id="ring">0s</div>' +
      '<p class="note">Recording&hellip; speak normally.</p>' +
      '<button id="en-done" class="primary">Done</button>' +
      '<button id="en-cancel">Cancel</button>';

    let elapsed = 0, live = true, discarded = false, eseq = 0, curRec = null;
    let echain = Promise.resolve();
    const ring = setInterval(() => {
      elapsed += 1;
      const r = $('ring');
      if (r) { r.textContent = elapsed + 's' + (elapsed >= 45 ? ' ✓' : ''); }
    }, 1000);

    function windowOnce() {
      if (!live) { return; }
      const rec = newRecorder(st);
      curRec = rec;
      let blob = null;
      rec.ondataavailable = (e) => { blob = e.data; };
      rec.onstop = () => {
        const captured = blob; blob = null;
        if (captured && captured.size > 0 && !discarded) {
          echain = echain.then(async () => {
            eseq += 1;
            // ?seq ALWAYS — a retried window must be idempotent, not a second append.
            const p = '?session=' + encodeURIComponent(session) + '&seq=' + String(eseq);
            try { await api('/scribe/enroll/chunk' + p, { method: 'POST', enroll: true, body: captured }); }
            catch (e) { /* a dropped window just shortens the sample */ }
          });
        }
        if (live) { windowOnce(); }
      };
      rec.start();                                              // NO timeslice (B2)
      setTimeout(() => { if (rec.state === 'recording') { rec.stop(); } }, ENROLL_WINDOW_MS);
    }

    // halt(discard) — stop the loop and release the mic.
    //   discard=false (Done) — the in-flight window is left to its own stop timer and its
    //     blob is dropped by `live=false`, exactly as before: `await echain` then covers
    //     only the COMPLETED windows, so finalize can never race a late chunk POST.
    //   discard=true (Cancel / route-away) — additionally STOP the live recorder now and
    //     refuse its blob, so no window keeps recording (and POSTing) behind a dead screen.
    function halt(discard) {
      live = false;
      if (discard) {
        discarded = true;
        try { if (curRec && curRec.state === 'recording') { curRec.stop(); } } catch (e) {}
      }
      clearInterval(ring);
      st.getTracks().forEach((t) => t.stop());
      if (micOwner === 'enroll') { micOwner = ''; }
      if (enrollHalt === halt) { enrollHalt = null; }
    }
    enrollHalt = halt;                    // route() tears the capture down through this
    windowOnce();

    $('en-done').addEventListener('click', async () => {
      halt(false);
      await echain;
      finalizeEnroll(session, rerecordId, myGen);    // the same generation tokens the poll
    });
    // CANCEL — an explicit discard. Drops the RAM bytes NOW (server-side) instead of
    // leaving them resident for the 10-minute TTL.
    $('en-cancel').addEventListener('click', async () => {
      halt(true);
      await echain;
      await abandonEnroll();
      $('enroll').classList.add('hide');
      renderPresets();
    });
  }

  async function finalizeEnroll(session, rerecordId, gen) {
    $('enroll-body').innerHTML = '<p><b>Making the voiceprint&hellip;</b></p>' +
      '<p class="note">Keep this screen open.</p>';
    let finalizeOk = false, finalizeStatus = 0;
    try {
      const r = await api('/scribe/enroll/finalize?session=' + encodeURIComponent(session), {
        method: 'POST', enroll: true, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: 'unnamed' }),              // record-first, name-LAST
      });
      finalizeOk = r.ok; finalizeStatus = r.status;
    } catch (e) { finalizeStatus = 0; }
    // GEN CHECK — a route-away DURING finalize/poll must not render the naming form into a
    // torn-down body (residual #5), and must NOT abandon: the finalize is in flight, the worker
    // is writing the centroid (its pre-write live-check would silently lose a completed
    // voiceprint if we abandoned). Just stop. The preset lands under its placeholder name and
    // shows in the list with a Rename.
    if (gen !== enrollGen) { return; }
    // A finalize REFUSAL (e.g. 409 preset_bound_open_encounter) leaves the session in
    // state="recording" server-side, so /enroll/result would answer {state:"processing"}
    // FOREVER — the client used to poll 300x500ms and then show a generic error. Fail
    // FAST with the copy that already exists for this status.
    if (!finalizeOk) { return enrollFailed(finalizeStatus, rerecordId); }
    // poll /result
    for (let i = 0; i < 300; i++) {
      let j = null;
      try {
        const r = await api('/scribe/enroll/result?session=' + encodeURIComponent(session), { enroll: true });
        j = await r.json();
      } catch (e) { /* transient */ }
      if (gen !== enrollGen) { return; }        // torn down mid-poll — no render, no abandon
      if (j && j.state === 'done') { return enrollVerdict(j, session, rerecordId); }
      if (j && j.state === 'unknown_session') { return enrollFailed('unknown_session', rerecordId); }
      await sleep(500);
    }
    enrollFailed('timeout', rerecordId);
  }

  const VERDICT_COPY = {
    too_short: 'That was too short. Please speak for about a minute.',
    no_speech: 'No speech was detected. Check the microphone and try again.',
    decode_failed: 'The audio could not be read. Try again.',
    engine_error: 'The voice engine could not process that. Try again.',
  };

  function enrollVerdict(j, session, rerecordId) {
    enrollSession = null;                     // the server cleared the bytes at finalize
    if (j.verdict !== 'ok' && j.verdict !== 'ok_marginal') {
      return enrollFailed(j.verdict, rerecordId);
    }
    // PASSED -> name it LAST. Prefill the MIC LABEL + today's date (never a patient).
    const marginal = j.verdict === 'ok_marginal';
    const today = new Date().toISOString().slice(0, 10);
    const prefill = (micLabel ? micLabel : 'Room mic') + ' ' + today;
    $('enroll-body').innerHTML =
      '<p><b>Voiceprint made.</b>' + (marginal ? ' (Quality is marginal but usable.)' : '') + '</p>' +
      '<p class="note">The audio is already deleted &mdash; only the numbers were kept.</p>' +
      '<label for="nm2">Name &mdash; name the place and mic, not a patient</label>' +
      '<input id="nm2" maxlength="64" autocomplete="off">' +
      '<button id="nm2-ok" class="primary">Save</button>';
    $('nm2').value = prefill;                                   // prefill (mic label + date)
    $('nm2-ok').addEventListener('click', async () => {
      const nm = $('nm2').value || prefill;
      const pid = j.preset_id;
      const q = '?user=' + encodeURIComponent(user) + '&preset=' + encodeURIComponent(pid);
      try {
        await api('/scribe/presets/rename' + q, {
          method: 'POST', enroll: true, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: nm }),
        });
      } catch (e) { /* the preset exists either way */ }
      $('enroll').classList.add('hide');
      renderPresets();
    });
  }

  function enrollFailed(reason, rerecordId) {
    // DROP THE BYTES NOW on every failure path. Otherwise a failed attempt leaves its RAM
    // buffer resident until the 10-min TTL — and two of them exhaust the 2-session cap,
    // 429-ing the very [Record again] this screen offers.
    abandonEnroll();
    const known = Object.prototype.hasOwnProperty.call(VERDICT_COPY, String(reason))
      ? VERDICT_COPY[reason] : null;
    const copy = known ||
      (reason === 503 ? 'Voice enrolment is not configured on this machine. Ask your operator.' :
       reason === 403 ? 'This clinician is not allowed to enrol. Ask your operator.' :
       reason === 429 ? 'Too many voiceprint attempts at once. Wait a moment and try again.' :
       reason === 409 ? 'That voiceprint is in use by a recording, or was deleted. Try again later.' :
       reason === 404 ? 'Voice enrolment is not available on this machine.' :
       'Something went wrong. Please try again.');
    $('enroll').classList.remove('hide');
    $('enroll-body').innerHTML = '<p>' + esc(copy) + '</p>' +
      '<button id="en-retry" class="primary">Record again</button>';
    $('en-retry').addEventListener('click', () => runEnroll(rerecordId));
  }

  // ══ BUG REPORT — a shared, memory-only diagnostic surface (task #4) ════════════
  // PHI-cautious: the free-text carries the on-screen "no patient details" banner, and the
  // attached auto-context is a CLOSED PHI-free set (the server ALSO drops any key outside its
  // allowlist). NO storage — the form + the ring die on reload. Confirms + failures are BOTH
  // rendered visibly (intentionally-left-blank: a submit is never a silent no-op).
  function currentBugContext() {
    let chip = '';
    try { chip = $('chip').textContent || ''; } catch (e) { chip = ''; }
    let ua = '';
    try { ua = (navigator && navigator.userAgent) ? String(navigator.userAgent).slice(0, 200) : ''; } catch (e) { ua = ''; }
    return {
      view: location.hash || '#/record',
      server_state: serverState || '',
      clinicians_len: CLINICIANS.length,
      user: user || '',                       // a clinician SLUG (staff id) — never a patient
      attribution: chip,
      ua: ua,
      client_ts: new Date().toISOString(),
    };
  }
  function openBug() {
    logEvent('open bug report');
    $('bug').classList.remove('hide');
    $('bug-msg').textContent = '';
    try { $('bug').scrollIntoView(); } catch (e) { /* shim / non-browser: no-op */ }
  }
  function closeBug() {
    $('bug').classList.add('hide');
    $('bug-summary').value = ''; $('bug-detail').value = ''; $('bug-msg').textContent = '';
  }
  async function submitBug() {
    const summary = ($('bug-summary').value || '').trim();
    const detail = ($('bug-detail').value || '').trim();
    if (!summary && !detail) {                 // ILB — an empty report SAYS so, never a no-op
      $('bug-msg').textContent = 'Please describe the problem first.';
      return;
    }
    if (bugSubmitCount >= BUG_MAX_PER_SESSION) {   // per-session cap — visible, never a silent drop
      $('bug-msg').textContent = 'You have sent the maximum number of reports for now. ' +
        'Please tell your operator directly.';
      return;
    }
    const send = $('bug-send');
    send.disabled = true;
    $('bug-msg').textContent = 'Sending…';
    const body = JSON.stringify({ summary: summary, detail: detail,
      context: currentBugContext(), events: bugRing.slice() });   // SNAPSHOT the ring now
    try {
      const r = await api('/scribe/bug', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: body });
      if (r.ok) {
        bugSubmitCount += 1;                   // count SUCCESSES against the per-session cap
        $('bug-summary').value = ''; $('bug-detail').value = '';
        $('bug-msg').textContent = 'Thank you — your report was sent.';         // VISIBLE confirm
      } else if (r.status === 413) {
        $('bug-msg').textContent = 'That report is too long — please shorten it.';
      } else if (r.status === 429) {
        $('bug-msg').textContent = 'Too many reports right now — tell your operator directly.';
      } else {
        $('bug-msg').textContent = 'Could not send the report (error ' + r.status + '). Tell your operator.';
      }
    } catch (e) {
      $('bug-msg').textContent = 'Could not reach the server. Tell your operator.';   // VISIBLE failure
    }
    send.disabled = false;
  }

  // ══ shell: clinician pickers + hash routing ═══════════════════════════════════

  function fillWho(sel) {
    let html = '';
    for (let i = 0; i < CLINICIANS.length; i++) {
      html += '<option value="' + esc(CLINICIANS[i]) + '">' + esc(CLINICIANS[i]) + '</option>';
    }
    sel.innerHTML = html || '<option value="">(none configured)</option>';
    if (CLINICIANS.length > 0 && !user) { user = CLINICIANS[0]; }
    sel.value = user;
  }

  // A wizard that is STAGED (intro rendered, its listener live) or CAPTURING must never
  // survive the view it lives in. Routing away tears it down: the mic closes, the live
  // window stops (it would otherwise keep recording and POSTing behind a HIDDEN view), the
  // RAM-held bytes are dropped server-side NOW rather than at the 10-minute TTL, an open
  // token prompt is settled, and the staged DOM — with its live listeners — is destroyed.
  function teardownEnroll() {
    enrollGen += 1;                   // invalidate every in-flight captureEnroll/finalize continuation
    const capturing = !!enrollHalt;
    if (enrollHalt) { enrollHalt(true); }             // stop the mic + the window loop
    // THE LEAK-WINDOW BELT — captureEnroll claims micOwner='enroll' synchronously but registers
    // enrollHalt only AFTER its getUserMedia + /enroll/start awaits. If teardown fires in that
    // window, enrollHalt is still null, so nothing above released the claim — release it here.
    // (The stale-gen bail in captureEnroll deliberately does NOT touch micOwner, so this is the
    // ONLY release for that window.) Guarded to 'enroll' so a live ENCOUNTER's claim is untouched.
    if (micOwner === 'enroll') { micOwner = ''; }
    // Abandon ONLY a session still RECORDING. A session already FINALIZED is being consumed
    // server-side (audio destroyed, centroid being written) — abandoning it would race that
    // write and silently lose a completed voiceprint. We just stop owning it; the preset
    // lands in the list under its placeholder name.
    if (capturing && enrollSession) { abandonEnroll(); }   // drop the RAM bytes NOW
    enrollSession = null;
    if (pendingToken) { const res = pendingToken; pendingToken = null; res(false); }
    $('enroll').classList.add('hide');
    $('enroll-body').innerHTML = '';                  // destroys the staged DOM + listeners
  }

  function route() {
    teardownEnroll();
    logEvent('view ' + (location.hash || '#/record'));
    const presets = location.hash === '#/presets';
    $('view-record').classList.toggle('hide', presets);
    $('view-presets').classList.toggle('hide', !presets);
    $('nav-record').classList.toggle('on', !presets);
    $('nav-presets').classList.toggle('on', presets);
    if (presets) { renderPresets(); } else { loadPresets().then(renderPicker); }
  }

  $('picker').addEventListener('change', (e) => {
    selectedPreset = e.currentTarget.value;
  });
  $('who').addEventListener('change', (e) => {
    user = e.currentTarget.value; $('who2').value = user;
    bindSession(user);                 // #12 12b: explicit selection binds/re-binds identity
    loadPresets().then(renderPicker);
  });
  $('who2').addEventListener('change', (e) => {
    user = e.currentTarget.value; $('who').value = user;
    bindSession(user);                 // #12 12b: explicit selection binds/re-binds identity
    renderPresets();
  });
  $('new-preset').addEventListener('click', () => { logEvent('tap create-voiceprint'); runEnroll(null); });
  $('start').addEventListener('click', start);
  $('stop').addEventListener('click', stop);
  $('bug-open-record').addEventListener('click', openBug);
  $('bug-open-presets').addEventListener('click', openBug);
  $('bug-send').addEventListener('click', submitBug);
  $('bug-cancel').addEventListener('click', closeBug);
  window.addEventListener('hashchange', route);

  fillWho($('who'));
  fillWho($('who2'));
  // #12 12b (Q3): auto-bind the identity session when EXACTLY ONE clinician is configured
  // (frictionless, honest  fillWho has already selected it). With >1 clinicians, binding waits
  // for an EXPLICIT clinician selection (the who/who2 change handler) so a shared device gets
  // stronger attribution  no session is bound until the acting clinician picks themselves.
  if (CLINICIANS.length === 1) { bindSession(CLINICIANS[0]); }
  route();
})();
"""


# ── Standalone-install assets (Task #1): manifest + icons + favicon ───────────────────
# The routes these are served on. They live HERE (not ingest_web) because the manifest
# content below references the icon paths — keeping the paths beside the content is the
# single source of truth (no path drift between the manifest and its route registration).
# ingest_web imports these for ``app.router.add_get``.
MANIFEST_ROUTE = "/manifest.webmanifest"
ICON_192_ROUTE = "/scribe/icon-192.png"
ICON_512_ROUTE = "/scribe/icon-512.png"
FAVICON_ROUTE = "/favicon.ico"
# iOS/WebKit uses apple-touch-icon (NOT the manifest icons member) for the home-screen tile,
# and — with a <link rel="apple-touch-icon"> declared — fetches ONLY the declared href and
# does NOT probe the root prefix (per Apple docs), so the sized-variant probes
# (/apple-touch-icon-120x120.png etc.) never fire and need no route. We ALSO serve the
# ``-precomposed.png`` sibling: some older WebKit prefers it, and a direct/no-link probe of
# either canonical path must get a 200 (not the 401 warning-spam the favicon fix killed). The
# operator's own device is an iPhone (module docstring), so this is the ratified target.
APPLE_TOUCH_ICON_ROUTE = "/apple-touch-icon.png"
APPLE_TOUCH_ICON_PRECOMPOSED_ROUTE = "/apple-touch-icon-precomposed.png"

_THEME_RGB = (0x1A, 0x7F, 0x37)   # THEME_COLOR as RGB — the solid icon fill


def _solid_png(size: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal, valid, SECRET-FREE truecolor PNG: a solid ``rgb`` ``size``x``size`` square.

    GENERATED, not embedded — the bytes are provably data-only (no token, no PHI, no
    external reference), which is the install-icon hard constraint. A solid fill is a valid
    ``maskable`` icon: every pixel is brand colour, so any launcher mask crop still shows the
    brand (the safe-zone requirement is trivially met). zlib-compressed, so a 512px square is
    a few hundred bytes on the wire."""
    r, g, b = rgb
    row = b"\x00" + bytes((r, g, b)) * size          # PNG filter byte 0 + `size` RGB pixels
    raw = row * size

    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8-bit truecolor RGB
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


ICON_192_PNG = _solid_png(192, _THEME_RGB)
ICON_512_PNG = _solid_png(512, _THEME_RGB)
FAVICON_PNG = _solid_png(32, _THEME_RGB)
APPLE_TOUCH_ICON_PNG = _solid_png(180, _THEME_RGB)   # iOS home-screen tile (180px = @3x)

# The Web App Manifest — STATIC and SECRET-FREE (no token, unlike the index page). ``display:
# standalone`` is what drops the URL bar; the maskable icons are what Chrome ≥108 needs to offer
# "Add to Home screen" as an installed app. NO ``serviceWorker`` / ``related_applications`` /
# storage key — installability only (offline is deliberately absent; see the module docstring).
MANIFEST_JSON = json.dumps(
    {
        "name": "STAY-C",
        "short_name": "STAY-C",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "theme_color": THEME_COLOR,
        "background_color": BACKGROUND_COLOR,
        "icons": [
            {"src": ICON_192_ROUTE, "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": ICON_512_ROUTE, "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    },
    separators=(",", ":"),
)


def render_index(token: str, clinicians: list[str] | None = None,
                 bug_max_per_session: int = 10) -> str:
    """Return the PWA index HTML with the INGEST ``token`` embedded (HTML-attribute
    escaped) for the same-origin JS, plus the ``clinicians`` identity list.

    The ENROLL token is deliberately NOT embedded (page possession must not grant
    biometric mutation) — the clinician pastes it, memory-only.

    ``clinicians`` are ``scribe.clinicians`` slugs (staff ids, never patient data). They
    are embedded so the enrolment view can OFFER the identity rather than make the
    clinician hand-type it: the server matches the slug VERBATIM and fail-closes on a
    mismatch, so a typo would otherwise burn a consented recording on a 403. JSON is
    embedded in a ``data-`` attribute (HTML-attribute escaped, so ``"``/``<`` can never
    break out) and parsed by the external JS — never an inline script (CSP).

    The ``<meta name="theme-color">`` is baked from :data:`THEME_COLOR` — the same constant
    the manifest uses — so the tab/splash colour can never drift from the manifest's."""
    payload = json.dumps(list(clinicians or []))
    return (
        _INDEX_HTML
        .replace(_TOKEN_PLACEHOLDER, html.escape(token, quote=True))
        .replace(_CLINICIANS_PLACEHOLDER, html.escape(payload, quote=True))
        .replace(_THEME_COLOR_PLACEHOLDER, THEME_COLOR)
        # the install-link hrefs are baked from the route constants (single source of truth —
        # a route rename can never leave the page linking a now-un-exempt path). QA findings 4/9.
        .replace(_MANIFEST_ROUTE_PLACEHOLDER, MANIFEST_ROUTE)
        .replace(_FAVICON_ROUTE_PLACEHOLDER, FAVICON_ROUTE)
        .replace(_APPLE_TOUCH_ICON_ROUTE_PLACEHOLDER, APPLE_TOUCH_ICON_ROUTE)
        # the client-side per-session report cap — embedded so the number lives in ONE place
        # (config), read by the page's bug form. Coerced to a safe int (never a stray value).
        .replace(_BUG_MAX_PLACEHOLDER, str(max(1, int(bug_max_per_session))))
    )
