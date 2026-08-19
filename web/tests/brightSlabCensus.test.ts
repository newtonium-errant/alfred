import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { COMMS_SURFACE } from '../lib/algernon/commsSurface';
import { VIEWSCREEN_SURFACE } from '../lib/algernon/viewscreenSurface';
import { CRT_SURFACE } from '../lib/algernon/crtSurface';
import { SENSOR_SURFACE } from '../lib/algernon/sensorSurface';

// THE BRIGHT-SLAB CENSUS, AS A TEST.
//
// The operator reported light/cream card surfaces sitting inside the dark
// registers — three times, over two days, on three different components. Three
// reports are not the population, and the lane that fixed only the three would
// be back the following week: the same components render on FIVE dark
// registers, and every register that lacked a rule was a report waiting to
// happen. `FeedRow` proved it — sensorLog.css had restyled that row for months
// while home rendered the identical component as a cream slab, because
// viewscreen.css never had the rule. Nobody could see that, because nothing
// enumerated it.
//
// So this file is the DENOMINATOR, mechanically derived and checked, rather
// than a list somebody maintains. It builds the import graph over
// pages/components/lib, maps each page to the register it renders in, and asks
// two questions no reviewer can answer by reading a diff:
//
//   1. Does every (marker, register) pair that CAN render have a rule — and
//      does every pair that cannot NOT have one? (holes, and dead CSS)
//   2. Does every light-background literal on a dark-reachable component carry
//      a marker — or appear below with a written reason?
//
// The second question is the one that closes the class. A new bright slab added
// tomorrow lands in a component this graph already reaches, and the test names
// the file and line rather than waiting for a screenshot.
//
// WHAT IT CANNOT CLAIM. jsdom evaluates no stylesheet, so nothing here reads a
// real cascade — these are source-text facts, exactly as the register guards
// next door are. The marker/utility adjacency check uses a line WINDOW around
// the literal (className strings wrap), so it is a heuristic about which
// element a class sits on. Both limits argue the same way: this catches the
// systematic misses, and eyes on the screen remain the only check on whether
// the result looks right.

const WEB = join(__dirname, '..');
const ROOTS = ['pages', 'components', 'lib'];

const DARK_REGISTERS = [COMMS_SURFACE, VIEWSCREEN_SURFACE, CRT_SURFACE, SENSOR_SURFACE, 'console'] as const;
const STYLESHEET: Record<string, string> = {
  [COMMS_SURFACE]: 'comms.css',
  [VIEWSCREEN_SURFACE]: 'viewscreen.css',
  [CRT_SURFACE]: 'crt.css',
  [SENSOR_SURFACE]: 'sensorLog.css',
  console: 'console.css',
};
const MARKERS = [
  'ui-panel', 'ui-field', 'ui-btn', 'ui-label', 'ui-code',
  'ui-alert', 'ui-wash-affirm', 'ui-wash-caution',
] as const;

// ---------------------------------------------------------------------------
// the graph
// ---------------------------------------------------------------------------
function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules') continue;
    const p = join(dir, entry);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.tsx?$/.test(p)) out.push(p);
  }
  return out;
}

const FILES = ROOTS.flatMap((r) => walk(join(WEB, r)));
const SRC = new Map(FILES.map((f) => [f, readFileSync(f, 'utf8')]));

function resolveImport(from: string, spec: string): string | null {
  if (!spec.startsWith('.')) return null;
  const base = resolve(dirname(from), spec);
  for (const c of [`${base}.tsx`, `${base}.ts`, join(base, 'index.tsx'), join(base, 'index.ts')]) {
    if (SRC.has(c)) return c;
  }
  return null;
}

const IMPORTS = new Map<string, Set<string>>();
for (const [file, text] of SRC) {
  const set = new Set<string>();
  for (const m of text.matchAll(/from\s+'([^']+)'/g)) {
    const r = resolveImport(file, m[1]);
    if (r) set.add(r);
  }
  IMPORTS.set(file, set);
}

/** The surface constants, by the name a page spells them with. */
const SURFACE_BY_CONST: Record<string, string> = {
  COMMS_SURFACE, VIEWSCREEN_SURFACE, CRT_SURFACE, SENSOR_SURFACE,
};

const PAGES = FILES.filter((f) => f.startsWith(join(WEB, 'pages')) && !f.includes(`${join('pages', 'api')}`));

/** Which register(s) a page renders in. `warm` is the unmarked default. */
function registersOf(page: string): Set<string> {
  const text = SRC.get(page)!;
  const regs = new Set<string>();
  for (const m of text.matchAll(/surface=\{?["']?([A-Za-z_-]+)["']?\}?/g)) {
    regs.add(SURFACE_BY_CONST[m[1]] ?? m[1]);
  }
  for (const m of text.matchAll(/data-surface=\{?([A-Z_]+)\}?/g)) {
    if (SURFACE_BY_CONST[m[1]]) regs.add(SURFACE_BY_CONST[m[1]]);
  }
  for (const tag of text.match(/<Layout[^>]*>/gs) ?? []) {
    if (!/surface=/.test(tag)) regs.add('warm');
  }
  return regs;
}

const PAGE_REGISTERS = new Map(PAGES.map((p) => [p, registersOf(p)]));

/** Every page that can render this file, transitively. */
const REACHING_PAGES = new Map<string, Set<string>>(FILES.map((f) => [f, new Set<string>()]));
for (const page of PAGES) {
  const seen = new Set<string>();
  const stack = [page];
  while (stack.length) {
    const cur = stack.pop()!;
    if (seen.has(cur)) continue;
    seen.add(cur);
    REACHING_PAGES.get(cur)!.add(page);
    for (const dep of IMPORTS.get(cur) ?? []) stack.push(dep);
  }
}

function registersReaching(file: string): Set<string> {
  const regs = new Set<string>();
  for (const page of REACHING_PAGES.get(file) ?? []) {
    for (const r of PAGE_REGISTERS.get(page) ?? []) regs.add(r);
  }
  return regs;
}

const REL = (f: string) => relative(WEB, f).replace(/\\/g, '/');

// ---------------------------------------------------------------------------
// (1) the marker matrix
// ---------------------------------------------------------------------------
const SHEET = new Map(
  Object.entries(STYLESHEET).map(([surface, file]) => [
    surface,
    readFileSync(join(WEB, 'styles', file), 'utf8').replace(/\/\*[\s\S]*?\*\//g, ''),
  ]),
);

/**
 * The declaration each marker exists to make. A marker "declared" without it is
 * not declared in any sense the operator can see.
 *
 * THIS TABLE IS THE FIX FOR A PIN THAT DID NOT FIRE. The first cut asked only
 * whether the marker's NAME appeared under the register's attribute, and the
 * mutation that should have caught it — delete `[data-surface='viewscreen']
 * .ui-panel { … }`, the rule that paints the slab — left the guard green,
 * because `.ui-panel.text-honeydew-900` in the ink rules still contained the
 * name. The guard was reading a label instead of the thing, the same shape the
 * role-inviolate pin next door records itself failing at twice.
 */
const PAINTS: Record<(typeof MARKERS)[number], string> = {
  'ui-panel': 'background-color',
  'ui-field': 'background-color',
  'ui-btn': 'background-color',
  'ui-code': 'background-color',
  'ui-alert': 'background-color',
  'ui-wash-affirm': 'background-color',
  'ui-wash-caution': 'background-color',
  // The only ink-only marker: a label has no ground of its own.
  'ui-label': 'color',
};

/** Selector → body, for every rule in a register's stylesheet. */
function rulesOf(surface: string): { selectors: string[]; body: string }[] {
  const out: { selectors: string[]; body: string }[] = [];
  for (const chunk of SHEET.get(surface)!.split('}')) {
    const brace = chunk.indexOf('{');
    if (brace === -1) continue;
    const head = chunk.slice(0, brace).replace(/\s+/g, ' ').trim();
    if (!head || head.startsWith('@')) continue;
    out.push({ selectors: head.split(',').map((s) => s.trim()), body: chunk.slice(brace + 1) });
  }
  return out;
}

/**
 * Does `surface` carry a rule that PAINTS `marker` — one whose selector ENDS at
 * the bare marker, and whose body makes the declaration the marker is for?
 *
 * Ending at the marker is what separates the rule that paints it from the rules
 * that reach THROUGH it: `.ui-panel .ui-btn` ends at `.ui-btn` and belongs to
 * that marker, `.ui-panel.text-honeydew-700` ends at the utility, and neither
 * says the panel itself has a ground.
 */
function declaresPaint(surface: string, marker: string): boolean {
  const property = PAINTS[marker as (typeof MARKERS)[number]];
  const endsAtMarker = new RegExp(`\\.${marker}$`);
  return rulesOf(surface).some(
    (rule) =>
      rule.selectors.some((s) => s.includes(`[data-surface='${surface}']`) && endsAtMarker.test(s)) &&
      new RegExp(`(^|;|\\s)${property}\\s*:`).test(rule.body),
  );
}

/** Registers that can render an element carrying `marker`. */
function registersCarrying(marker: string): Set<string> {
  const regs = new Set<string>();
  const token = new RegExp(`(^|['"\\s])${marker}(['"\\s]|$)`);
  for (const [file, text] of SRC) {
    if (!token.test(text)) continue;
    for (const r of registersReaching(file)) regs.add(r);
  }
  return regs;
}

describe('the marker matrix matches what can actually render', () => {
  it('the graph resolved something — the positive control for every row below', () => {
    // Every assertion in this file is a statement about a graph. A graph that
    // silently resolved nothing (a moved directory, a changed import style)
    // would make all of them vacuously true at once.
    expect(FILES.length).toBeGreaterThan(80);
    expect(PAGES.length).toBeGreaterThan(6);
    const withDeps = [...IMPORTS.values()].filter((s) => s.size > 0).length;
    expect(withDeps).toBeGreaterThan(50);
    // …and the register map found the surfaces, not just the pages.
    const allRegs = new Set([...PAGE_REGISTERS.values()].flatMap((s) => [...s]));
    for (const r of DARK_REGISTERS) expect(allRegs).toContain(r);
  });

  it.each(MARKERS)('%s is declared by exactly the registers that can render it', (marker) => {
    const canRender = registersCarrying(marker);
    const holes: string[] = [];
    const dead: string[] = [];
    for (const surface of DARK_REGISTERS) {
      const declared = declaresPaint(surface, marker);
      if (canRender.has(surface) && !declared) {
        holes.push(`${surface} can render .${marker} but ${STYLESHEET[surface]} declares no rule`);
      }
      if (!canRender.has(surface) && declared) {
        dead.push(`${STYLESHEET[surface]} declares .${marker} but no consumer reaches ${surface}`);
      }
    }
    // A HOLE is the bug the operator reported: the component is marked, the
    // register is dark, and nothing paints it.
    expect(holes).toEqual([]);
    // DEAD CSS is the opposite failure and the house already rules on it —
    // crt.css deliberately carries no `ui-code` because no FencedText consumer
    // reaches a crt surface, and "a rule nothing can reach is dead CSS".
    expect(dead).toEqual([]);
  });

  it('every marker is carried by SOMETHING — no marker exists only in a stylesheet', () => {
    // The vacuity control for the row above. A marker nothing carries has an
    // empty `canRender`, so its "no holes" assertion passes for free while its
    // "no dead CSS" assertion would have to delete every rule to agree.
    for (const marker of MARKERS) {
      expect(registersCarrying(marker).size, `.${marker} is carried by no component`).toBeGreaterThan(0);
    }
  });
});

// ---------------------------------------------------------------------------
// (2) the census proper
// ---------------------------------------------------------------------------
const LIGHT_LITERAL =
  /bg-(?:white|cream|honeydew-(?:50|100|200)|danger-bg|status-(?:todo|progress|blocked|done)(?![a-z-])|amber-(?:50|100))(?![a-z0-9-])/g;
const MARKER_NEARBY = new RegExp(
  `\\b(?:${MARKERS.join('|')})\\b`
  // The transcript's two hulls are named through IMPORTED CONSTANTS rather
  // than class literals (commsSurface.ts owns the spelling), so a check that
  // knew only the rendered class name reported both chat bubbles as unruled.
  + '|comms-turn-(?:assistant|operator)\\b'
  + '|COMMS_TURN_(?:ASSISTANT|OPERATOR)_CLASS',
);

/**
 * The RULED EXCEPTIONS — light on purpose, with the reason.
 *
 * This is the half of the census a graph cannot decide. Each key is
 * `<file>:<literal>` and each value is why that literal is correctly light even
 * though the component is reachable from a dark register. A reason is required:
 * an entry with an empty string fails the shape check below, because "someone
 * added a key to make the test pass" is exactly the drift this list would
 * otherwise invite.
 */
const RULED_LIGHT: Record<string, string> = {
  'components/Layout.tsx:bg-honeydew-50':
    'The WARM entry of the chrome table. It is selected by surface at runtime — a dark ' +
    'surface takes CONSOLE_CHROME and never reads this string — so the literal being ' +
    'in a file the dark registers import says nothing about what they render.',
  'components/Layout.tsx:bg-honeydew-100': 'Same — the warm chrome table entry.',
  'components/Layout.tsx:bg-white': 'Same — the warm chrome table entry.',
  'components/ReportBugFab.tsx:bg-white':
    'The FAB\'s warm skin, kept as the UNMARKED DEFAULT for the same reason ui-panel ' +
    'keeps one: /share is a warm route and renders this button. The dark surfaces take ' +
    'their skin from the chrome table (`s.fab`), not from this fallback.',
  'components/ReportBugFab.tsx:bg-honeydew-50': 'Same — the warm fallback skin\'s hover state.',
  'components/feed/EvidenceBody.tsx:bg-honeydew-50':
    'The `warm` entry of this component\'s own per-surface SKIN map. It carries ui-panel ' +
    'so a dark register reaches it; the literal is the warm default underneath.',
  'components/ui/badge.tsx:bg-status-todo':
    'Dead component — `git grep` finds no importer of ui/badge.tsx anywhere in web/. ' +
    'It renders on no surface, dark or warm.',
  'components/ui/badge.tsx:bg-status-progress': 'Same — ui/badge.tsx has no importer anywhere in web/.',
  'components/ui/badge.tsx:bg-status-blocked': 'Same — ui/badge.tsx has no importer anywhere in web/.',
  'components/ui/badge.tsx:bg-status-done': 'Same — ui/badge.tsx has no importer anywhere in web/.',
  'lib/algernon/commsSurface.ts:bg-cream':
    'PROSE, not a class. The docstring quotes `bg-cream` while explaining which slab the ' +
    'operator photographed. The scan reads source text and cannot tell a quoted class from ' +
    'a rendered one; this entry is that distinction, written down.',
};

type Row = { file: string; line: number; literal: string; marked: boolean; registers: string[] };

function census(): Row[] {
  const rows: Row[] = [];
  for (const [file, text] of SRC) {
    const regs = [...registersReaching(file)].filter((r) => (DARK_REGISTERS as readonly string[]).includes(r));
    if (regs.length === 0) continue;
    const lines = text.split('\n');
    lines.forEach((line, i) => {
      for (const literal of new Set([...line.matchAll(LIGHT_LITERAL)].map((m) => m[0]))) {
        // className strings wrap, and cva variant maps put the marker on the
        // same STRING but not always the same line. A window is the honest
        // instrument here, and it is stated rather than hidden.
        const window = lines.slice(Math.max(0, i - 6), i + 7).join('\n');
        rows.push({
          file: REL(file), line: i + 1, literal,
          marked: MARKER_NEARBY.test(window),
          registers: regs.sort(),
        });
      }
    });
  }
  return rows;
}

describe('every light surface reachable from a dark register is ruled on', () => {
  const rows = census();

  it('the census found the population it is supposed to bound', () => {
    // Vacuity control. A regex that stopped matching, or a graph that stopped
    // resolving, empties this and every finding below with it.
    expect(rows.length).toBeGreaterThan(30);
    const files = new Set(rows.map((r) => r.file));
    expect(files.size).toBeGreaterThan(10);
    // The three the operator actually photographed must be IN the denominator —
    // a census that excluded the reported cases would bound the wrong set.
    for (const f of [
      'components/NotificationList.tsx',
      'components/feed/FeedRow.tsx',
      'pages/chat.tsx',
    ]) {
      expect(files, `${f} must be inside the census`).toContain(f);
    }
  });

  it('carries a marker, or a written reason — nothing is merely unnoticed', () => {
    const unruled = rows
      .filter((r) => !r.marked)
      .filter((r) => RULED_LIGHT[`${r.file}:${r.literal}`] === undefined)
      .map((r) => `${r.file}:${r.line}  ${r.literal}  (renders on: ${r.registers.join(', ')})`);
    expect(
      [...new Set(unruled)],
      'A light background is reachable from a dark register with neither a ui-* marker ' +
        'nor an entry in RULED_LIGHT. Either mark it so the register can reach it, or ' +
        'add it to RULED_LIGHT with the reason it is correctly light.',
    ).toEqual([]);
  });

  it('every ruled exception states a reason, and still corresponds to real code', () => {
    // Two ways this list rots: an entry added with an empty reason to silence a
    // failure, and an entry left behind after the code it excused was deleted.
    // A stale entry is the worse one — it reads as a decision somebody made.
    const stale: string[] = [];
    for (const [key, reason] of Object.entries(RULED_LIGHT)) {
      expect(reason.trim().length, `${key} has no reason`).toBeGreaterThan(20);
      const [file, literal] = key.split(':');
      const text = SRC.get(join(WEB, file));
      if (text === undefined || !text.includes(literal)) stale.push(key);
    }
    expect(stale, 'RULED_LIGHT excuses code that no longer exists').toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// (3) the values the markers resolve to
// ---------------------------------------------------------------------------
describe('a marker resolves to its register\'s own dark vocabulary', () => {
  const SHARED = readFileSync(join(WEB, 'styles', 'console.css'), 'utf8');

  /** Every `--token: value` the app defines, last definition winning. */
  const DEFS = new Map<string, string>();
  for (const text of [SHARED, ...SHEET.values()]) {
    for (const m of text.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
      DEFS.set(m[1], m[2].trim());
    }
  }

  /** Follow an alias to the literal it ends at. Unresolvable is a FAILURE. */
  function terminal(token: string): string {
    const seen = new Set<string>();
    let cur = token;
    for (let hop = 0; hop < 12; hop += 1) {
      if (seen.has(cur)) return `cyclic:${cur}`;
      seen.add(cur);
      const value = DEFS.get(cur);
      if (value === undefined) return `undefined:${cur}`;
      const next = /^var\((--[a-z0-9-]+)\)$/.exec(value);
      if (!next) return value;
      cur = next[1];
    }
    return `too-deep:${token}`;
  }

  it.each(DARK_REGISTERS)('%s paints markers with dark hexes, chain-followed', (surface) => {
    // THE CHAIN, NOT THE NAME. A token called `--comms-panel` bound to #ffffff
    // satisfies every name-shaped check in the repo and ships the slab the
    // operator reported. So each background is followed to the literal it
    // actually ends at, and that literal is required to be dark.
    const css = SHEET.get(surface)!;
    const checked: string[] = [];
    for (const m of css.matchAll(
      new RegExp(`\\[data-surface='${surface}'\\][^{]*\\.(ui-[a-z-]+)[^{]*\\{([^}]*)\\}`, 'g'),
    )) {
      const bg = /background-color:\s*var\((--[a-z0-9-]+)\)/.exec(m[2]);
      if (!bg) continue;
      const value = terminal(bg[1]);
      checked.push(`${m[1]} -> ${bg[1]} -> ${value}`);
      const hex = /^#([0-9a-f]{6})$/i.exec(value);
      expect(hex, `${surface}: .${m[1]} background ${bg[1]} resolves to "${value}", not a hex`).not.toBeNull();
      // Relative luminance, the cheap sRGB approximation. A "dark register"
      // token that came out at 0.5 would be the bug wearing a token name.
      const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex![1].slice(i, i + 2), 16) / 255);
      const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
      expect(lum, `${surface}: .${m[1]} resolves to ${value}, which is not dark`).toBeLessThan(0.3);
    }
    // Vacuity control, per register: a regex that matched no rule would pass
    // this row without following a single chain.
    expect(checked.length, `${surface} had no marker background to follow`).toBeGreaterThan(1);
  });
});
