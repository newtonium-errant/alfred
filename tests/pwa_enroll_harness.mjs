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
 *
 * Assertions target the FETCH LOG (method + URL + bearer token) — the real server
 * contract from docs/scribe_enroll_api.md — plus picker/banner DOM state.
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
  statusPresetFit: 'ok',
  bindStatus: 200,
  startStatus: 200,
  finalizeStatus: 200,
  clinicians: ['np_jamie'],
  instantWindow: false,
  micLabel: 'Built-in Mic',
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
  default: break;
}

// ═══ 3. shims ═════════════════════════════════════════════════════════════════
const calls = [];
const INGEST_TOKEN = 'INGEST_TOK';
const ENROLL_TOKEN = 'ENROLL_TOK';

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
    return json({ user: 'np_jamie', state: cfg.serverState, presets: cfg.presets,
                  mru_preset_id: cfg.mru });
  }
  if (url.startsWith('/scribe/status')) {
    return json({ chunks: 1, state: 'recording', preset_fit: cfg.statusPresetFit });
  }
  if (url.startsWith('/scribe/encounter/preset')) { return json({}, cfg.bindStatus); }
  if (url.startsWith('/scribe/enroll/start')) {
    return json({ session: 'enr-1-abc' }, cfg.startStatus);
  }
  if (url.startsWith('/scribe/enroll/finalize')) { return json({}, cfg.finalizeStatus); }
  if (url.startsWith('/scribe/enroll/result')) {
    return json({ state: 'done', verdict: 'ok', preset_id: 'pst-x', stats: {} });
  }
  return json({});
}

const location = { hash: '#/record' };
const windowShim = { MediaRecorder, addEventListener: () => {}, confirm: () => true };
const navigatorShim = {
  mediaDevices: {
    getUserMedia: async () => ({
      getTracks: () => [{ stop() {} }],
      getAudioTracks: () => [{ label: cfg.micLabel, stop() {} }],
    }),
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
  location.hash = '#/presets';
  el('new-preset').click();
  await settle(12);
  if (registry.has('tok')) {
    el('tok').value = ENROLL_TOKEN;
    el('tok-ok').click();
    await settle(12);
  }
}

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
  } else if (scenario === 'enroll_blocked_during_encounter') {
    await startEncounter();                 // an encounter is LIVE
    location.hash = '#/presets';
    el('new-preset').click();               // ...now try to enrol
    await settle(15);
    out.enrollBody = el('enroll-body').innerHTML;
  } else if (scenario === 'encounter_blocked_during_enroll') {
    await openEnrollWithToken();
    el('en-go').click();                    // an enrolment session is LIVE
    await settle(20);
    location.hash = '#/record';
    el('start').click();                    // ...now try to start an encounter
    await settle(20);
    out.status = el('status').textContent;
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
