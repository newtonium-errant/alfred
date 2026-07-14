/**
 * Behavioural harness for the STAY-C PWA (P4-5 enrolment UI).
 *
 * Runs the REAL `APP_JS` (piped in on argv[2] as a file path) inside a minimal,
 * SELF-CONTAINED DOM/browser shim and drives a named scenario, then prints a JSON
 * result the Python test asserts on.
 *
 * Why not jsdom: the only copy in this repo is a TRANSITIVE dep of the separate `web/`
 * tree (not present in this worktree, and free to vanish on a web/ dependency bump).
 * A scribe test must not couple to that.
 *
 * The shim's ONE load-bearing safety property: `getElementById` THROWS on an unknown id
 * rather than returning a dummy. A shim gap therefore surfaces as a hard error — it can
 * NEVER silently mask a broken app and produce a false pass.
 *
 * Assertions target the FETCH LOG (method + URL + which bearer token) — i.e. the actual
 * server contract from docs/scribe_enroll_api.md — not DOM fidelity, which the on-box
 * browser smoke (#54) owns.
 */
import { readFileSync } from 'node:fs';

const appJsPath = process.argv[2];
const scenario = process.argv[3];

// ── knobs a scenario can set ───────────────────────────────────────────────────
const cfg = {
  supportedMimes: ['audio/webm'],   // what MediaRecorder.isTypeSupported says yes to
  defaultMime: 'audio/webm',
  presets: [],
  mru: null,
  statusPresetFit: 'ok',
  bindStatus: 200,
  clinicians: ['np_jamie'],
};

// ── fetch log (the contract under test) ────────────────────────────────────────
const calls = [];
const INGEST_TOKEN = 'INGEST_TOK';
const ENROLL_TOKEN = 'ENROLL_TOK';

// ── minimal DOM shim ───────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

class El {
  constructor(id, tag) {
    this.id = id; this.tag = tag || 'div';
    this._html = ''; this._text = ''; this.value = '';
    this.disabled = false; this.listeners = {}; this.attrs = {};
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
    this._html = String(v);
    // Register any elements the app just rendered, so getElementById finds them.
    const idRe = /id="([^"]+)"/g;
    let m;
    while ((m = idRe.exec(this._html)) !== null) {
      const tagM = this._html.slice(0, m.index).match(/<(\w+)[^<]*$/);
      registry.set(m[1], new El(m[1], tagM ? tagM[1] : 'div'));
    }
    // Track data-act buttons for querySelectorAll.
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

const registry = new Map();
// The ids present in the served HTML (must stay in lockstep with _INDEX_HTML).
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
      // LOAD-BEARING: never return a dummy. A shim gap must be a HARD ERROR, not a
      // silent false pass.
      throw new Error('DOM shim: unknown element id "' + id + '" (app queried it, shim lacks it)');
    }
    return registry.get(id);
  },
  createElement(tag) { return new El(null, tag); },
};

// ── MediaRecorder shim (instances exposed so the driver can end a window) ──────
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
    // ADVERSARIAL MODE — the window completes IMMEDIATELY. Without this a chunk cannot
    // POST until the real ~20s window elapses, so the binding wins the race BY DEFAULT
    // and an ordering assertion cannot distinguish "binds first" from "binds late"
    // (mutation-proven: moving bindPreset() after startWindow() still passed). Firing
    // the window instantly makes the race REAL: an un-awaited bind loses.
    // ONE-SHOT: only the FIRST window fires instantly (that is all the race needs).
    // Leaving it armed would loop forever: onstop -> startWindow -> instant stop -> ...
    if (cfg.instantWindow) { cfg.instantWindow = false; queueMicrotask(() => this.stop()); }
  }
  stop() {
    if (this.state !== 'recording') { return; }
    this.state = 'inactive';
    if (this.ondataavailable) { this.ondataavailable({ data: { size: 2048 } }); }
    if (this.onstop) { this.onstop(); }
  }
}

// ── fetch shim ─────────────────────────────────────────────────────────────────
async function fetchShim(url, opts) {
  const o = opts || {};
  const auth = (o.headers && o.headers['Authorization']) || '';
  const token = auth.replace('Bearer ', '');
  calls.push({ method: o.method || 'GET', url, token });

  const json = (obj, status) => ({
    ok: (status || 200) < 400, status: status || 200, json: async () => obj,
  });
  if (url.startsWith('/scribe/presets?')) {
    return json({ user: 'np_jamie', state: cfg.presets.length ? 'ok' : 'empty',
                  presets: cfg.presets, mru_preset_id: cfg.mru });
  }
  if (url.startsWith('/scribe/status')) {
    return json({ chunks: 1, state: 'recording', preset_fit: cfg.statusPresetFit });
  }
  if (url.startsWith('/scribe/encounter/preset')) { return json({}, cfg.bindStatus); }
  if (url.startsWith('/scribe/enroll/start')) { return json({ session: 'enr-1-abc' }); }
  if (url.startsWith('/scribe/enroll/result')) {
    return json({ state: 'done', verdict: 'ok', preset_id: 'pst-x', stats: {} });
  }
  return json({});
}

// ── globals ────────────────────────────────────────────────────────────────────
// NOTE: these are INJECTED as function parameters, not assigned to globalThis (node 24
// makes `navigator` getter-only). The app therefore runs against exactly these shims.
const location = { hash: '#/record' };
const windowShim = { MediaRecorder, addEventListener: () => {}, confirm: () => true };
const navigatorShim = {
  mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) },
};
const cryptoShim = {
  getRandomValues: (a) => { for (let i = 0; i < a.length; i++) { a[i] = i + 1; } return a; },
};

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n) => { for (let i = 0; i < (n || 12); i++) { await tick(); } };

// ── run the REAL app ───────────────────────────────────────────────────────────
const APP = readFileSync(appJsPath, 'utf8');
// eslint-disable-next-line no-new-func
new Function('document', 'window', 'MediaRecorder', 'fetch', 'location', 'navigator', 'crypto',
             'URLSearchParams', APP)(
  document, windowShim, MediaRecorder, fetchShim, location, navigatorShim,
  cryptoShim, URLSearchParams);

// ── scenario driver ────────────────────────────────────────────────────────────
const el = (id) => registry.get(id);

async function recordFlow({ preset }) {
  await settle();
  if (preset) { el('picker').value = preset; el('picker').change(); }
  el('start').click();
  await settle(20);
  // end one capture window
  const rec = recorders[recorders.length - 1];
  if (rec) { rec.stop(); }
  await settle(20);
}

async function enrollFlow() {
  await settle();
  location.hash = '#/presets';
  el('new-preset').click();
  await settle(10);
  // paste the enroll token
  el('tok').value = ENROLL_TOKEN;
  el('tok-ok').click();
  await settle(10);
  el('en-go').click();                 // intro -> start recording
  await settle(20);
  // two windows
  for (let i = 0; i < 2; i++) {
    const rec = recorders[recorders.length - 1];
    if (rec) { rec.stop(); }
    await settle(10);
  }
  el('en-done').click();
  await settle(40);
  // record-first, NAME-LAST: the verdict screen offers the name field. Complete it.
  if (registry.has('nm2-ok')) {
    out.namePrefill = el('nm2').value;      // prefilled with mic label + date
    el('nm2').value = 'Clinic Room A';
    el('nm2-ok').click();
    await settle(20);
  }
}

const out = { scenario, calls: null, error: null };
try {
  if (scenario === 'record_binds_before_first_chunk') {
    cfg.presets = [{ preset_id: 'pst-a', name: 'Room A', classification: 'usable' }];
    cfg.mru = 'pst-a';
    cfg.instantWindow = true;          // make the bind-vs-chunk race REAL (see MediaRecorder)
    await recordFlow({ preset: 'pst-a' });
  } else if (scenario === 'record_no_preset_never_binds') {
    cfg.presets = [{ preset_id: 'pst-a', name: 'Room A', classification: 'usable' }];
    cfg.mru = null;
    await recordFlow({ preset: '' });
  } else if (scenario === 'record_bind_failure_does_not_block') {
    cfg.presets = [{ preset_id: 'pst-a', name: 'Room A', classification: 'usable' }];
    cfg.mru = 'pst-a'; cfg.bindStatus = 409;
    await recordFlow({ preset: 'pst-a' });
  } else if (scenario === 'record_ios_maps_mp4_to_m4a') {
    cfg.supportedMimes = ['audio/mp4'];      // iPhone: NO webm support
    cfg.defaultMime = 'audio/mp4';
    await recordFlow({ preset: '' });
  } else if (scenario === 'record_unknown_preset_fit_tolerated') {
    cfg.statusPresetFit = 'warming';         // a 5b value the 5a client must tolerate
    await recordFlow({ preset: '' });
    out.chip = el('chip').textContent;
  } else if (scenario === 'enroll_flow') {
    await enrollFlow();
  } else {
    throw new Error('unknown scenario ' + scenario);
  }
  out.calls = calls;
} catch (e) {
  out.error = String(e && e.stack || e);
  out.calls = calls;
}
console.log(JSON.stringify(out));
process.exit(0);
