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

_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
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
  select, input { font-size: 1.05rem; padding: 0.6rem; border-radius: 0.5rem; border: 1px solid #8886; width: 100%; box-sizing: border-box; background: transparent; color: inherit; }
  label { display: block; margin: 0.8rem 0 0.3rem; font-size: 0.9rem; color: #888; }
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
<body data-ingest-token="__INGEST_TOKEN__" data-clinicians="__CLINICIANS_JSON__">
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

  const WINDOW_MS = 20000;           // encounter window (~20s) — B2 boundary amortized
  const ENROLL_WINDOW_MS = 15000;    // enrollment window (~15s) — memo B2 discipline
  const ENROLL_TARGET_MS = 45000;    // ~45s of speech clears the 10s HARD gate + nears 30s advisory
  const MAX_ATTEMPTS = 6;

  // ── R5: MEMORY-ONLY. No localStorage/sessionStorage/IndexedDB/SW anywhere. ─────
  let enrollToken = '';              // pasted once per page-load; a reload asks again
  let user = '';                     // selected clinician (a scribe.clinicians slug)
  let selectedPreset = '';           // '' == "No preset — attribution off" (first-class)
  let presetsCache = [];
  let mruPresetId = null;
  let serverState = '';              // 'empty' | 'all_incompatible' | 'ok' (the API contract)
  let enrollSession = null;          // non-null while an enrolment session holds RAM bytes
  let micLabel = '';                 // the device label, for the name prefill

  // encounter state
  let stream = null, recording = false, label = null, seq = 0, recorder = null;
  let windowTimer = null, chain = Promise.resolve(), stopped = false;

  const $ = (id) => document.getElementById(id);
  const statusEl = $('status');
  function show(msg) { statusEl.textContent = msg; }            // NON-PHI text only
  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
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
    return fetch(path, { method: o.method || 'GET', headers: headers, body: o.body, cache: 'no-store' });
  }

  // ══ RECORD VIEW ═══════════════════════════════════════════════════════════════

  async function loadPresets() {
    presetsCache = []; mruPresetId = null; serverState = '';
    if (!user) { return; }
    try {
      const r = await api('/scribe/presets?user=' + encodeURIComponent(user));  // EITHER token
      if (r.status === 404) { serverState = 'inert'; return; }  // enrollment face inert
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
    if (enrollSession) {
      show('Finish or cancel the voiceprint recording first — the microphone is in use.');
      return;
    }
    $('start').disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
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
      '<button id="tok-ok" class="primary">Continue</button>';
    return await new Promise((res) => {
      $('tok-ok').addEventListener('click', () => {
        enrollToken = $('tok').value || '';
        $('tok').value = '';                                    // drop it from the DOM
        res(!!enrollToken);
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

  // The guided enrolment: record-first, name-last (~45-60s).
  async function runEnroll(rerecordId) {
    if (!user) { return; }
    // MUTUAL EXCLUSION (memo) — never enrol while an encounter is recording: the
    // enrolment buffer would capture LIVE PATIENT SPEECH, and the whole consent basis of
    // this surface is "the enrolling clinician's own voice".
    if (recording) {
      $('enroll').classList.remove('hide');
      $('enroll-title').textContent = 'Not now';
      $('enroll-body').innerHTML = '<p>An encounter is recording. Stop it before making a ' +
        'voiceprint &mdash; the microphone can only do one at a time.</p>';
      return;
    }
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
    let st;
    try {
      st = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      $('enroll-body').innerHTML = '<p>Microphone unavailable.</p>' +
        '<button id="en-retry" class="primary">Record again</button>';
      $('en-retry').addEventListener('click', () => runEnroll(rerecordId));
      return;
    }
    // /enroll/start — validates the clinician BEFORE any recording (never a wasted 45s).
    let q = '?user=' + encodeURIComponent(user);
    if (rerecordId) { q += '&preset=' + encodeURIComponent(rerecordId); }
    let session = null;
    try {
      const r = await api('/scribe/enroll/start' + q, { method: 'POST', enroll: true });
      if (!r.ok) { st.getTracks().forEach((t) => t.stop()); return enrollFailed(r.status, rerecordId); }
      session = (await r.json()).session;
      enrollSession = session;                 // RAM bytes are now held server-side
    } catch (e) {
      st.getTracks().forEach((t) => t.stop());
      return enrollFailed(0, rerecordId);
    }
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

    let elapsed = 0, live = true, eseq = 0;
    let echain = Promise.resolve();
    const ring = setInterval(() => {
      elapsed += 1;
      const r = $('ring');
      if (r) { r.textContent = elapsed + 's' + (elapsed >= 45 ? ' ✓' : ''); }
    }, 1000);

    function windowOnce() {
      if (!live) { return; }
      const rec = newRecorder(st);
      let blob = null;
      rec.ondataavailable = (e) => { blob = e.data; };
      rec.onstop = () => {
        const captured = blob; blob = null;
        if (captured && captured.size > 0) {
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
    windowOnce();

    function halt() {
      live = false;
      clearInterval(ring);
      st.getTracks().forEach((t) => t.stop());
    }
    $('en-done').addEventListener('click', async () => {
      halt();
      await echain;
      finalizeEnroll(session, rerecordId);
    });
    // CANCEL — an explicit discard. Drops the RAM bytes NOW (server-side) instead of
    // leaving them resident for the 10-minute TTL.
    $('en-cancel').addEventListener('click', async () => {
      halt();
      await echain;
      await abandonEnroll();
      $('enroll').classList.add('hide');
      renderPresets();
    });
  }

  async function finalizeEnroll(session, rerecordId) {
    $('enroll-body').innerHTML = '<p><b>Making the voiceprint&hellip;</b></p>' +
      '<p class="note">Keep this screen open.</p>';
    try {
      const r = await api('/scribe/enroll/finalize?session=' + encodeURIComponent(session), {
        method: 'POST', enroll: true, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: 'unnamed' }),              // record-first, name-LAST
      });
      // A finalize REFUSAL (e.g. 409 preset_bound_open_encounter) leaves the session in
      // state="recording" server-side, so /enroll/result would answer {state:"processing"}
      // FOREVER — the client used to poll 300x500ms and then show a generic error. Fail
      // FAST with the copy that already exists for this status.
      if (!r.ok) { return enrollFailed(r.status, rerecordId); }
    } catch (e) { return enrollFailed(0, rerecordId); }
    // poll /result
    for (let i = 0; i < 300; i++) {
      let j = null;
      try {
        const r = await api('/scribe/enroll/result?session=' + encodeURIComponent(session), { enroll: true });
        j = await r.json();
      } catch (e) { /* transient */ }
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

  function route() {
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
    user = e.currentTarget.value; $('who2').value = user; loadPresets().then(renderPicker);
  });
  $('who2').addEventListener('change', (e) => {
    user = e.currentTarget.value; $('who').value = user; renderPresets();
  });
  $('new-preset').addEventListener('click', () => runEnroll(null));
  $('start').addEventListener('click', start);
  $('stop').addEventListener('click', stop);
  window.addEventListener('hashchange', route);

  fillWho($('who'));
  fillWho($('who2'));
  route();
})();
"""


def render_index(token: str, clinicians: list[str] | None = None) -> str:
    """Return the PWA index HTML with the INGEST ``token`` embedded (HTML-attribute
    escaped) for the same-origin JS, plus the ``clinicians`` identity list.

    The ENROLL token is deliberately NOT embedded (page possession must not grant
    biometric mutation) — the clinician pastes it, memory-only.

    ``clinicians`` are ``scribe.clinicians`` slugs (staff ids, never patient data). They
    are embedded so the enrolment view can OFFER the identity rather than make the
    clinician hand-type it: the server matches the slug VERBATIM and fail-closes on a
    mismatch, so a typo would otherwise burn a consented recording on a 403. JSON is
    embedded in a ``data-`` attribute (HTML-attribute escaped, so ``"``/``<`` can never
    break out) and parsed by the external JS — never an inline script (CSP)."""
    payload = json.dumps(list(clinicians or []))
    return (
        _INDEX_HTML
        .replace(_TOKEN_PLACEHOLDER, html.escape(token, quote=True))
        .replace(_CLINICIANS_PLACEHOLDER, html.escape(payload, quote=True))
    )
