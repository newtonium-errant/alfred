/**
 * Behavioural harness for the STAY-C PWA (P4-5 enrolment UI).
 *
 * Runs the REAL `APP_JS` (argv[2] = a file path) inside a minimal, SELF-CONTAINED
 * DOM/browser shim, drives a named scenario, and prints a JSON result the Python test
 * asserts on.
 *
 * Why not jsdom: the only copy in this repo is a TRANSITIVE dep of the separate `web/`
 * tree (absent from this worktree, free to vanish on a web/ dependency bump).
 *
 * ── ORDERING IS LOAD-BEARING ──────────────────────────────────────────────────
 * The app BOOTS on evaluation (its IIFE immediately calls route() -> loadPresets() -> GET
 * /scribe/presets). So the scenario's server fixtures MUST be applied BEFORE the boot.
 * The first version of this harness set them AFTER, so every picker/MRU scenario silently
 * served `{state:"empty",presets:[]}` — `cfg.presets`/`cfg.mru` were DEAD CONFIG, the
 * picker was never exercised, and a mutant that made the picker OFFER revoked/corrupt
 * presets survived the whole green suite. Section order is: cfg -> SCENARIO FIXTURES ->
 * shims -> BOOT -> interactions. Do not reorder.
 *
 * Shim fidelity properties (each one exists because a wrong shim HIDES a real bug):
 *   * `getElementById` THROWS on an unknown id — a shim gap is a hard error, never a
 *     silent false pass.
 *   * `escapeHtml` matches BROWSER semantics: textContent->innerHTML escapes & < > but
 *     NOT quotes. A stricter shim would hide attribute-injection bugs in the app.
 *   * the element registry DROPS ids owned by an innerHTML that gets replaced, so an
 *     "element exists" assertion can never pass on a stale ghost.
 *   * `location.hash` is a real SETTER that DISPATCHES `hashchange` to the handlers
 *     `window.addEventListener` registered. The first version stubbed addEventListener to
 *     a no-op, so the app's `window.addEventListener('hashchange', route)` was silently
 *     DISCARDED: route() ran only at boot, no scenario ever changed views, and BOTH routing
 *     mutants (inverted hide-toggles / listener removed) survived — a build where the
 *     clinician can never reach the presets view at all would have shipped green. A stub
 *     that swallows the thing under test is the same defect class as the boot-order bug.
 *
 * Assertions target the FETCH LOG (method + URL + bearer token) — the real server
 * contract from docs/scribe_enroll_api.md — plus picker/banner/view DOM state.
 */
import { readFileSync } from 'node:fs';

const appJsPath = process.argv[2];
const scenario = process.argv[3];

// ═══ 1. cfg defaults ══════════════════════════════════════════════════════════
const cfg = {
  supportedMimes: ['audio/webm'],
  defaultMime: 'audio/webm',
  presets: [],
  mru: null,
  serverState: 'empty',
  presetsStatus: 200,          // 404 ⇒ the enrolment face is INERT (enroll_token unset)
  statusPresetFit: 'ok',
  bindStatus: 200,
  startStatus: 200,
  finalizeStatus: 200,
  clinicians: ['np_jamie'],
  instantWindow: false,
  micLabel: 'Built-in Mic',
  // SUSPENSION GATES — hold an await open so a teardown can be interposed mid-flight. Each is
  // a promise the interaction code resolves after it has navigated away. Used ONLY by the
  // await-teardown scenarios (the generation-token BLOCK); the ordinary flows resolve instantly.
  holdFirstGetUserMedia: false,
  holdEnrollStart: false,
  holdEnrollFinalize: false,
  holdEnrollResult: false,
};

const USABLE = (id, name) => ({ preset_id: id, name: name, classification: 'usable' });

// ═══ 2. SCENARIO FIXTURES — MUST be applied BEFORE the app boots ══════════════
switch (scenario) {
  case 'record_binds_before_first_chunk':
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.mru = 'pst-a';
    cfg.serverState = 'ok'; cfg.instantWindow = true; break;
  case 'record_no_preset_never_binds':
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.serverState = 'ok'; break;
  case 'record_bind_failure_does_not_block':
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.mru = 'pst-a';
    cfg.serverState = 'ok'; cfg.bindStatus = 409; break;
  case 'record_ios_maps_mp4_to_m4a':
    cfg.supportedMimes = ['audio/mp4']; cfg.defaultMime = 'audio/mp4'; break;
  case 'record_unknown_preset_fit_tolerated':
    cfg.statusPresetFit = 'warming'; break;
  case 'record_prototype_preset_fit_tolerated':
    cfg.statusPresetFit = 'constructor'; break;       // a PROTOTYPE key, not a real value
  case 'mru_preselected':
    cfg.presets = [USABLE('pst-a', 'Room A'), USABLE('pst-b', 'Room B')];
    cfg.mru = 'pst-b'; cfg.serverState = 'ok'; break;
  case 'mru_points_at_unusable':                      // a server that wrongly offers one
    cfg.presets = [USABLE('pst-a', 'Room A'),
                   { preset_id: 'pst-bad', name: 'Old', classification: 'incompatible_engine' }];
    cfg.mru = 'pst-bad'; cfg.serverState = 'ok'; break;
  case 'picker_excludes_unusable':
    cfg.presets = [USABLE('pst-a', 'Room A'),
                   { preset_id: 'pst-r', name: 'Gone', classification: 'revoked' },
                   { preset_id: 'pst-c', name: 'Torn', classification: 'corrupt' },
                   { preset_id: 'pst-e', name: 'Old', classification: 'incompatible_engine' }];
    cfg.serverState = 'ok'; break;
  case 'registry_empty':
    cfg.presets = []; cfg.serverState = 'empty'; break;
  case 'registry_all_revoked':
    cfg.presets = [{ preset_id: 'pst-r', name: 'Gone', classification: 'revoked' }];
    cfg.serverState = 'all_incompatible'; break;
  case 'registry_all_corrupt':
    cfg.presets = [{ preset_id: 'pst-c', name: 'Torn', classification: 'corrupt' }];
    cfg.serverState = 'all_incompatible'; break;
  case 'registry_all_stale':
    cfg.presets = [{ preset_id: 'pst-e', name: 'Old', classification: 'incompatible_engine' }];
    cfg.serverState = 'all_incompatible'; break;
  case 'picker_locked_while_recording':
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.mru = 'pst-a'; cfg.serverState = 'ok'; break;
  case 'enroll_finalize_409':
    cfg.finalizeStatus = 409; break;
  case 'enroll_start_429':
    cfg.startStatus = 429; break;
  case 'enroll_staged_then_encounter_composed':
  case 'route_away_mid_enroll_abandons':
  case 'routing_toggles_views':
  case 'encounter_start_races_enroll_capture':
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.mru = 'pst-a'; cfg.serverState = 'ok'; break;
  case 'enroll_capture_races_encounter_start':
    // instantWindow: the encounter must actually COMPLETE a window, so "the encounter ran
    // normally" is asserted on a real chunk POST rather than on the absence of a failure.
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.mru = 'pst-a';
    cfg.serverState = 'ok'; cfg.instantWindow = true; break;
  // THE GENERATION-TOKEN BLOCK — a teardown interposed DURING captureEnroll's own awaits. Each
  // holds one await open, so the interaction can navigate away before it resolves. instantWindow
  // lets the FOLLOW-ON encounter complete a real chunk, proving the mic claim was released.
  case 'teardown_during_getusermedia':
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.mru = 'pst-a'; cfg.serverState = 'ok';
    cfg.holdFirstGetUserMedia = true; cfg.instantWindow = true; break;
  case 'teardown_during_enroll_start':
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.mru = 'pst-a'; cfg.serverState = 'ok';
    cfg.holdEnrollStart = true; cfg.instantWindow = true; break;
  case 'teardown_during_finalize_poll':
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.mru = 'pst-a'; cfg.serverState = 'ok';
    cfg.holdEnrollResult = true; break;
  case 'teardown_during_finalize_call':
    // finalize HELD and answers 409 on release: without the PRE-poll gen check, enrollFailed
    // renders the 409 copy into the torn-down body. (The POLL check cannot see this — the flow
    // never reaches the poll on a !ok finalize.)
    cfg.presets = [USABLE('pst-a', 'Room A')]; cfg.mru = 'pst-a'; cfg.serverState = 'ok';
    cfg.holdEnrollFinalize = true; cfg.finalizeStatus = 409; break;
  // THE INERT BOX — the DEFAULT ship posture: enroll_token unset ⇒ EVERY enroll-face path
  // 404s, /scribe/presets included (it is on the enroll face).
  case 'inert_record_view':
  case 'inert_presets_view':
    cfg.presetsStatus = 404; break;
  // an attribute-context injection attempt smuggled through a SERVER-supplied preset_id.
  case 'quote_in_preset_id':
    cfg.presets = [{ preset_id: 'pst-a" autofocus onfocus="steal()',
                     name: 'Room "A"', classification: 'usable' }];
    cfg.serverState = 'ok'; break;
  default: break;
}

// ═══ 3. shims ═════════════════════════════════════════════════════════════════
const calls = [];
const INGEST_TOKEN = 'INGEST_TOK';
const ENROLL_TOKEN = 'ENROLL_TOK';

// SUSPENSION GATES — a deferred the interaction resolves after navigating away, so a teardown
// lands DURING the held await. `releaseX()` (exposed on globalThis) resolves the matching gate.
function makeGate() { let release; const p = new Promise((r) => { release = r; }); return { p, release }; }
const gumGate = makeGate();          // first getUserMedia
const enrollStartGate = makeGate();  // /scribe/enroll/start
const enrollFinalizeGate = makeGate(); // /scribe/enroll/finalize
const enrollResultGate = makeGate(); // first /scribe/enroll/result
let gumHeld = false, resultHeld = false;
globalThis.__releaseGum = gumGate.release;
globalThis.__releaseEnrollStart = enrollStartGate.release;
globalThis.__releaseEnrollFinalize = enrollFinalizeGate.release;
globalThis.__releaseEnrollResult = enrollResultGate.release;

// BROWSER semantics: textContent -> innerHTML escapes & < > but NOT quotes. A stricter
// shim (escaping quotes too) would HIDE attribute-injection bugs in the app under test.
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const registry = new Map();

class El {
  constructor(id, tag) {
    this.id = id; this.tag = tag || 'div';
    this._html = ''; this._text = ''; this.value = '';
    this.disabled = false; this.listeners = {}; this.attrs = {};
    this._owned = [];                      // ids this element's innerHTML created
    this.classList = {
      _s: new Set(),
      add: (c) => this.classList._s.add(c),
      remove: (c) => this.classList._s.delete(c),
      toggle: (c, on) => { if (on) this.classList._s.add(c); else this.classList._s.delete(c); },
      contains: (c) => this.classList._s.has(c),
    };
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this._html = escapeHtml(v); }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    // Replacing innerHTML DESTROYS the previous children — drop them from the registry so
    // a stale ghost can never satisfy an "element exists" assertion.
    for (const old of this._owned) { registry.delete(old); }
    this._owned = [];
    this._html = String(v);
    const idRe = /id="([^"]+)"/g;
    let m;
    while ((m = idRe.exec(this._html)) !== null) {
      const tagM = this._html.slice(0, m.index).match(/<(\w+)[^<]*$/);
      registry.set(m[1], new El(m[1], tagM ? tagM[1] : 'div'));
      this._owned.push(m[1]);
    }
    this._actButtons = [];
    const actRe = /data-act="([^"]+)"[^>]*data-id="([^"]+)"/g;
    while ((m = actRe.exec(this._html)) !== null) {
      const b = new El('__act_' + m[1] + '_' + m[2], 'button');
      b.attrs['data-act'] = m[1]; b.attrs['data-id'] = m[2];
      this._actButtons.push(b);
    }
  }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  click() { (this.listeners['click'] || []).forEach((f) => f({ currentTarget: this })); }
  change() { (this.listeners['change'] || []).forEach((f) => f({ currentTarget: this })); }
  querySelectorAll(sel) {
    if (sel === 'button[data-act]') { return this._actButtons || []; }
    return [];
  }
}

for (const id of ['status', 'start', 'stop', 'picker', 'preset-msg', 'chip', 'who', 'who2',
                  'presets-list', 'new-preset', 'enroll', 'enroll-title', 'enroll-body',
                  'nav-record', 'nav-presets', 'view-record', 'view-presets']) {
  registry.set(id, new El(id));
}

const body = new El('body', 'body');
body.dataset = { ingestToken: INGEST_TOKEN, clinicians: JSON.stringify(cfg.clinicians) };

const document = {
  body,
  getElementById(id) {
    if (!registry.has(id)) {
      throw new Error('DOM shim: unknown element id "' + id + '" (app queried it, shim lacks it)');
    }
    return registry.get(id);
  },
  createElement(tag) { return new El(null, tag); },
};

const recorders = [];
class MediaRecorder {
  static isTypeSupported(m) { return cfg.supportedMimes.includes(m); }
  constructor(stream, opts) {
    this.mimeType = (opts && opts.mimeType) || cfg.defaultMime;
    this.state = 'inactive';
    recorders.push(this);
  }
  start() {
    this.state = 'recording';
    // ONE-SHOT instant window: the first window completes immediately, so a chunk POST is
    // reachable without waiting out the real 20s timer.
    //
    // HONEST SCOPE (this comment previously overclaimed): this does NOT by itself make the
    // bind-vs-chunk race decidable. `calls.push` is synchronous, so a bind issued anywhere
    // on the synchronous prefix is logged first regardless of statement order. The ordering
    // test's real teeth are that it catches a DROPPED bind and a bind deferred to a
    // MACROTASK — both mutation-proven. A pure statement reorder that still binds before
    // any chunk POSTs is not a defect, and the test correctly passes it.
    if (cfg.instantWindow) { cfg.instantWindow = false; queueMicrotask(() => this.stop()); }
  }
  stop() {
    if (this.state !== 'recording') { return; }
    this.state = 'inactive';
    if (this.ondataavailable) { this.ondataavailable({ data: { size: 2048 } }); }
    if (this.onstop) { this.onstop(); }
  }
}

async function fetchShim(url, opts) {
  const o = opts || {};
  const auth = (o.headers && o.headers['Authorization']) || '';
  calls.push({ method: o.method || 'GET', url, token: auth.replace('Bearer ', '') });
  const json = (obj, status) => ({
    ok: (status || 200) < 400, status: status || 200, json: async () => obj,
  });
  if (url.startsWith('/scribe/presets?')) {
    // 404 = INERT enrolment face (enroll_token unset). The route answers no body the client
    // can trust — the whole face is ABSENT, not merely empty.
    if (cfg.presetsStatus !== 200) { return json({}, cfg.presetsStatus); }
    return json({ user: 'np_jamie', state: cfg.serverState, presets: cfg.presets,
                  mru_preset_id: cfg.mru });
  }
  if (url.startsWith('/scribe/status')) {
    return json({ chunks: 1, state: 'recording', preset_fit: cfg.statusPresetFit });
  }
  if (url.startsWith('/scribe/encounter/preset')) { return json({}, cfg.bindStatus); }
  if (url.startsWith('/scribe/enroll/start')) {
    if (cfg.holdEnrollStart) { await enrollStartGate.p; }   // hold so a teardown lands mid-await
    return json({ session: 'enr-1-abc' }, cfg.startStatus);
  }
  if (url.startsWith('/scribe/enroll/finalize')) {
    if (cfg.holdEnrollFinalize) { await enrollFinalizeGate.p; }   // hold so a teardown lands mid-await
    return json({}, cfg.finalizeStatus);
  }
  if (url.startsWith('/scribe/enroll/result')) {
    if (cfg.holdEnrollResult && !resultHeld) { resultHeld = true; await enrollResultGate.p; }
    return json({ state: 'done', verdict: 'ok', preset_id: 'pst-x', stats: {} });
  }
  return json({});
}

// BROWSER-ACCURATE hash routing. `location.hash = x` DISPATCHES hashchange to the
// listeners the app registered — the app's own `location.hash = '#/presets'` navigations
// route exactly as they do in a browser, and so do the test's. (The previous no-op
// addEventListener stub swallowed the app's hashchange handler entirely.)
const winListeners = {};
let _hash = '#/record';
const location = {
  get hash() { return _hash; },
  set hash(v) {
    const next = String(v);
    if (next === _hash) { return; }
    _hash = next;
    (winListeners['hashchange'] || []).forEach((f) => f({ type: 'hashchange' }));
  },
};
const windowShim = {
  MediaRecorder,
  addEventListener: (ev, fn) => { (winListeners[ev] = winListeners[ev] || []).push(fn); },
  confirm: () => true,
};
// mic opens are COUNTED: "the enrolment path never touched the microphone" is the assertion
// that actually binds the consent property (a refusal that still opened the mic is a fail).
const micOpens = { n: 0 };
const navigatorShim = {
  mediaDevices: {
    getUserMedia: async () => {
      micOpens.n += 1;
      if (cfg.holdFirstGetUserMedia && !gumHeld) { gumHeld = true; await gumGate.p; }  // hold the FIRST only
      return {
        getTracks: () => [{ stop() {} }],
        getAudioTracks: () => [{ label: cfg.micLabel, stop() {} }],
      };
    },
  },
};
const cryptoShim = {
  getRandomValues: (a) => { for (let i = 0; i < a.length; i++) { a[i] = i + 1; } return a; },
};

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n) => { for (let i = 0; i < (n || 15); i++) { await tick(); } };

// ═══ 4. BOOT the real app (fixtures are already in place) ═════════════════════
const APP = readFileSync(appJsPath, 'utf8');
// eslint-disable-next-line no-new-func
new Function('document', 'window', 'MediaRecorder', 'fetch', 'location', 'navigator', 'crypto',
             'URLSearchParams', APP)(
  document, windowShim, MediaRecorder, fetchShim, location, navigatorShim,
  cryptoShim, URLSearchParams);

// ═══ 5. interactions ══════════════════════════════════════════════════════════
const el = (id) => registry.get(id);
const out = { scenario, calls: null, error: null };

async function setHash(h) {          // a real navigation: dispatches hashchange -> route()
  location.hash = h;
  await settle(15);
}
async function startEncounter() {
  el('start').click();
  await settle(25);
}
async function oneWindow() {
  const rec = recorders[recorders.length - 1];
  if (rec) { rec.stop(); }
  await settle(20);
}
async function openEnrollWithToken() {
  await setHash('#/presets');
  el('new-preset').click();
  await settle(12);
  if (registry.has('tok')) {
    el('tok').value = ENROLL_TOKEN;
    el('tok-ok').click();
    await settle(12);
  }
}
const views = () => ({
  record: !el('view-record').classList.contains('hide'),
  presets: !el('view-presets').classList.contains('hide'),
  navRecordOn: el('nav-record').classList.contains('on'),
  navPresetsOn: el('nav-presets').classList.contains('on'),
});

try {
  await settle();                     // let the boot's loadPresets()/renderPicker() run

  if (scenario === 'picker_locked_while_recording') {
    await startEncounter();
    out.pickerDisabled = el('picker').disabled;
    out.whoDisabled = el('who').disabled;
  } else if (scenario.startsWith('record_')) {
    if (scenario === 'record_no_preset_never_binds') {
      el('picker').value = ''; el('picker').change();
    }
    await startEncounter();
    await oneWindow();
    out.chip = el('chip').textContent;
  } else if (scenario.startsWith('mru_') || scenario === 'picker_excludes_unusable') {
    out.pickerHtml = el('picker').innerHTML;
    out.pickerValue = el('picker').value;
  } else if (scenario.startsWith('registry_')) {
    out.presetMsg = el('preset-msg').innerHTML;
    out.startDisabled = el('start').disabled;
  } else if (scenario === 'routing_toggles_views') {
    out.atBoot = views();
    await setHash('#/presets');
    out.atPresets = views();
    await setHash('#/record');
    out.backAtRecord = views();
  } else if (scenario === 'inert_record_view') {
    out.presetMsg = el('preset-msg').innerHTML;
    out.startDisabled = el('start').disabled;
    out.pickerHtml = el('picker').innerHTML;
  } else if (scenario === 'inert_presets_view') {
    await setHash('#/presets');
    out.presetsList = el('presets-list').innerHTML;
    out.newPresetHidden = el('new-preset').classList.contains('hide');
    el('new-preset').click();               // ...even if the button is somehow reached
    await settle(15);
    out.enrollBody = el('enroll-body').innerHTML;
    out.micOpens = micOpens.n;
  } else if (scenario === 'quote_in_preset_id') {
    out.pickerHtml = el('picker').innerHTML;
    await setHash('#/presets');
    out.presetsList = el('presets-list').innerHTML;
  } else if (scenario === 'enroll_blocked_during_encounter') {
    await startEncounter();                 // an encounter is LIVE
    await setHash('#/presets');
    el('new-preset').click();               // ...now try to enrol
    await settle(15);
    out.enrollBody = el('enroll-body').innerHTML;
  } else if (scenario === 'enroll_staged_then_encounter_composed') {
    // THE BLOCK PATH — a consent violation composed out of ORDINARY NAVIGATION (no race,
    // no devtools):
    //   1. #/presets -> "Create a voiceprint": runEnroll() sees recording=false and STAGES
    //      the intro, wiring a LIVE listener on [Start recording].
    //   2. #/record -> Start: the encounter is now LIVE on the patient's mic.
    //   3. back to #/presets: renderPresets() rewrites #presets-list and (pre-fix) never
    //      touched #enroll — the staged intro was still on screen, its listener live.
    //   4. tap the staged [Start recording] -> captureEnroll() opened a SECOND recorder on
    //      the LIVE PATIENT MIC and pushed those windows into a PERMANENT biometric centroid.
    // The staged button's handle is captured BEFORE the navigation, so this test still FIRES
    // it even though the route() teardown now removes the node — i.e. it pins captureEnroll's
    // OWN action-moment guard, independently of the teardown belt. Both must hold.
    await setHash('#/presets');
    el('new-preset').click();
    await settle(12);
    if (registry.has('tok')) {
      el('tok').value = ENROLL_TOKEN; el('tok-ok').click(); await settle(12);
    }
    const staged = registry.get('en-go');          // the STAGED button (handle kept)
    out.staged = !!staged;
    await setHash('#/record');
    await startEncounter();                        // the encounter is LIVE
    await setHash('#/presets');
    const micBefore = micOpens.n;
    if (staged) { staged.click(); await settle(25); }
    out.micOpensOnStagedClick = micOpens.n - micBefore;
    out.enrollBody = el('enroll-body').innerHTML;
  } else if (scenario === 'encounter_start_races_enroll_capture') {
    // THE RACE the mic CLAIM exists for. `enrollSession` is only set AFTER two awaits
    // (getUserMedia, /enroll/start), so a Start clicked in that window sees no session and
    // — on a `enrollSession`-only guard — sails through: two recorders, one mic, patient
    // speech in the enrolment buffer. The claim is taken SYNCHRONOUSLY, before the awaits.
    await openEnrollWithToken();
    el('en-go').click();                    // captureEnroll: claims, then awaits the mic
    el('start').click();                    // ...NO settle: the session does not exist yet
    await settle(30);
    out.status = el('status').textContent;
    out.micOpens = micOpens.n;
  } else if (scenario === 'enroll_capture_races_encounter_start') {
    // The MIRROR race: `recording` is only set AFTER start()'s getUserMedia await, so a
    // staged [Start recording] fired inside that window sees `recording === false`.
    await setHash('#/presets');
    el('new-preset').click();
    await settle(12);
    if (registry.has('tok')) {
      el('tok').value = ENROLL_TOKEN; el('tok-ok').click(); await settle(12);
    }
    const staged = registry.get('en-go');
    await setHash('#/record');
    el('start').click();                    // start(): claims, then awaits the mic
    staged.click();                         // ...NO settle: `recording` is still false
    await settle(30);
    out.enrollBody = el('enroll-body').innerHTML;
    out.micOpens = micOpens.n;              // 1 = the encounter's own; a 2nd = the breach
  } else if (scenario === 'teardown_during_getusermedia') {
    // BLOCK — a route-away while getUserMedia HOLDS the await (the OS mic prompt, SECONDS on
    // first run). captureEnroll has claimed micOwner but not yet registered enrollHalt, so the
    // ONLY thing that can stop the resumed continuation is the generation token.
    await openEnrollWithToken();
    el('en-go').click();                    // captureEnroll: claims mic+gen, awaits getUserMedia (HELD)
    await settle(15);
    await setHash('#/record');              // teardown: bumps gen, releases the stuck micOwner
    globalThis.__releaseGum();              // getUserMedia resolves → GEN CHECK #1 bails
    await settle(30);
    el('start').click();                    // the encounter must now START (mic was released)
    await settle(30);
  } else if (scenario === 'teardown_during_enroll_start') {
    // BLOCK — one await later: a route-away while /enroll/start holds. The server may hold bytes
    // for the opened session; the continuation must abandon them, release the mic, and NOT start
    // a window. Then the encounter must start.
    await openEnrollWithToken();
    el('en-go').click();                    // captureEnroll: past getUserMedia, awaits /enroll/start (HELD)
    await settle(15);
    await setHash('#/record');              // teardown: bumps gen, releases micOwner
    globalThis.__releaseEnrollStart();      // /enroll/start resolves → GEN CHECK #2 abandons + bails
    await settle(30);
    el('start').click();
    await settle(30);
  } else if (scenario === 'teardown_during_finalize_poll') {
    // Residual #5 — a route-away DURING the finalize poll must not render the naming form into
    // a torn-down body, and must NOT abandon (the worker is writing the centroid).
    await openEnrollWithToken();
    el('en-go').click();
    await settle(20);
    await oneWindow(); await oneWindow();
    el('en-done').click();                  // halt + finalize → first /result is HELD
    await settle(15);
    await setHash('#/record');              // teardown during the poll: bumps gen (must be a DIFFERENT hash)
    globalThis.__releaseEnrollResult();     // /result resolves 'done' → finalize GEN CHECK bails
    await settle(40);
    out.enrollBody = el('enroll-body').innerHTML;
  } else if (scenario === 'teardown_during_finalize_call') {
    await openEnrollWithToken();
    el('en-go').click();
    await settle(20);
    await oneWindow(); await oneWindow();
    el('en-done').click();                  // halt + finalize (HELD before the poll)
    await settle(15);
    await setHash('#/record');              // teardown DURING finalize: bumps gen
    globalThis.__releaseEnrollFinalize();   // finalize resolves 409 → PRE-poll GEN CHECK bails
    await settle(40);
    out.enrollBody = el('enroll-body').innerHTML;
  } else if (scenario === 'route_away_mid_enroll_abandons') {
    await openEnrollWithToken();
    el('en-go').click();                    // an enrolment CAPTURE is live (mic open)
    await settle(20);
    out.recorderRecordingBefore = recorders[recorders.length - 1].state === 'recording';
    await setHash('#/record');              // ...navigate AWAY mid-capture
    await settle(25);
    // a window left running would keep recording — and POSTing enrolment chunks — behind a
    // HIDDEN view, with the RAM bytes resident until the 10-minute TTL.
    out.recorderRecordingAfter = recorders[recorders.length - 1].state === 'recording';
  } else if (scenario === 'encounter_blocked_during_enroll') {
    await openEnrollWithToken();
    el('en-go').click();                    // an enrolment session is LIVE
    await settle(20);
    // NO hash navigation here — routing away now TEARS THE WIZARD DOWN (halt + abandon), so
    // a real user cannot reach [Start encounter] with a live enrolment. Clicking the
    // (hidden-view) Start button directly pins the BELT: start()'s own mutual-exclusion
    // guard, which must still hold if the teardown ever regresses.
    el('start').click();
    await settle(20);
    out.status = el('status').textContent;
    out.micOpens = micOpens.n;              // 1 = the enrolment's own; a 2nd = the breach
  } else if (scenario === 'enroll_cancel_abandons') {
    await openEnrollWithToken();
    el('en-go').click();
    await settle(20);
    await oneWindow();
    el('en-cancel').click();
    await settle(25);
  } else {   // enroll_flow / enroll_finalize_409 / enroll_start_429
    await openEnrollWithToken();
    if (registry.has('en-go')) {
      el('en-go').click();
      await settle(20);
      await oneWindow();
      await oneWindow();
      if (registry.has('en-done')) { el('en-done').click(); await settle(40); }
    }
    if (registry.has('nm2-ok')) {
      out.namePrefill = el('nm2').value;
      el('nm2').value = 'Clinic Room A';
      el('nm2-ok').click();
      await settle(20);
    }
    out.enrollBody = el('enroll-body').innerHTML;
  }
  out.calls = calls;
} catch (e) {
  out.error = String((e && e.stack) || e);
  out.calls = calls;
}
console.log(JSON.stringify(out));
process.exit(0);
