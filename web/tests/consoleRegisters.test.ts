import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { VIEWSCREEN_SURFACE } from '../lib/algernon/viewscreenSurface';
import { CRT_SURFACE } from '../lib/algernon/crtSurface';
import { COMMS_SURFACE } from '../lib/algernon/commsSurface';

// THE REGISTER CONTRACT — one guard for all three console-completion registers.
//
// This closes a hole that was open for `sensor-log` and would have been open
// three more times: NOTHING asserted that a register's own token family is
// complete. Rename `--sensor-ink-dim` in the stylesheet while a component still
// says `var(--sensor-ink-dim)` and the property silently resolves to nothing —
// no error anywhere, the text just loses its colour, and every test stays green.
// That is the same shape as the `role-shaped hole` in consoleTokens.test.ts:
// a typo'd token is not an error in CSS, it is an absence.
//
// jsdom does not evaluate an `_app`-imported stylesheet, so none of this can be
// a computed-style assertion. It is a source-text contract instead, parsed
// rather than grepped — the first cut of the sensor-log equivalent reported
// `180deg` and a sentence from a file header as "unscoped selectors", because
// comments and gradient arguments look exactly like selectors to a line matcher.

const REGISTERS = [
  { name: VIEWSCREEN_SURFACE, file: 'viewscreen.css', prefix: 'viewscreen' },
  { name: CRT_SURFACE, file: 'crt.css', prefix: 'crt' },
  { name: COMMS_SURFACE, file: 'comms.css', prefix: 'comms' },
] as const;

// The four function roles. A register may restyle chrome; it may NOT restyle a
// verdict — an operator who learned "amber means look at this" on the deck must
// read the same amber in every room.
const ROLES = ['affirm', 'negative', 'caution', 'info'] as const;

function cssFor(file: string): string {
  const raw = readFileSync(join(__dirname, '..', 'styles', file), 'utf8');
  return raw.replace(/\/\*[\s\S]*?\*\//g, ''); // comments out, before any parse
}

function rules(css: string): Array<{ selector: string; body: string }> {
  return css
    .split('}')
    .map((chunk) => {
      const i = chunk.indexOf('{');
      return i < 0
        ? null
        : { selector: chunk.slice(0, i).replace(/\s+/g, ' ').trim(), body: chunk.slice(i + 1) };
    })
    .filter((r): r is { selector: string; body: string } => r != null && r.selector !== '');
}

function declarations(body: string): string[] {
  return body
    .split(';')
    .map((d) => d.replace(/\s+/g, ' ').trim())
    .filter(Boolean);
}

describe.each(REGISTERS)('the $name register', ({ name, file, prefix }) => {
  const css = cssFor(file);

  it('has a stylesheet with real content — the positive control', () => {
    // Every assertion below passes vacuously against an empty string read from
    // a path that silently moved.
    expect(css.length).toBeGreaterThan(400);
    expect(rules(css).length).toBeGreaterThan(3);
  });

  it('scopes its definitions under its OWN name, spelled as the TS constant', () => {
    // The stylesheet cannot import the constant, so this is the pin that keeps
    // the two spellings equal. An absence check against a stale selector passes
    // forever, which is why the template literal uses the imported name.
    expect(css).toContain(`[data-surface='${name}']`);
  });

  it('the attribute rule is INERT — custom properties only, nothing that paints', () => {
    // The attribute lives on the Layout ROOT, which is shared chrome: the nav,
    // the header and the bug-report FAB are all inside it. A painting rule here
    // reaches all three. Definitions inherit and paint nothing, so they are safe
    // there; everything that paints belongs on the surface's own content class.
    const attr = rules(css).filter((r) => r.selector === `[data-surface='${name}']`);
    expect(attr).toHaveLength(1);

    const decls = declarations(attr[0].body);
    expect(decls.length).toBeGreaterThan(8);
    expect(decls.filter((d) => !d.startsWith('--'))).toEqual([]);
  });

  it('every rule is surface-scoped or an owned class — it cannot reach a sibling', () => {
    const escaped = rules(css)
      .map((r) => r.selector)
      .flatMap((s) => s.split(','))
      .map((s) => s.trim())
      .filter((s) => !s.includes(`[data-surface='${name}']`) && !s.startsWith(`.${prefix}-`));
    expect(escaped).toEqual([]);
  });

  it('defines every token it references — the role-shaped hole, closed', () => {
    // THE POINT OF THIS FILE. Collect every `var(--<prefix>-…)` the stylesheet
    // reads, and require the definitions block to define it. A renamed property
    // with a stale reader is invisible in CSS: it resolves to nothing and the
    // element simply loses that value.
    const attr = rules(css).find((r) => r.selector === `[data-surface='${name}']`)!;
    const defined = new Set(
      declarations(attr.body)
        .map((d) => d.slice(0, d.indexOf(':')).trim())
        .filter((d) => d.startsWith('--')),
    );
    const referenced = [...css.matchAll(new RegExp(`var\\((--${prefix}-[a-z0-9-]+)\\)`, 'g'))]
      .map((m) => m[1]);

    expect(referenced.length).toBeGreaterThan(2); // vacuity control
    for (const token of referenced) {
      expect(defined.has(token), `${file} reads ${token} but never defines it`).toBe(true);
    }
  });

  it('keeps the four function roles pointed at the SHARED hue', () => {
    // THE HARD CONSTRAINT, made falsifiable. A register restyles chrome, never a
    // verdict. "Make the errors green too, it looks more terminal" must fail
    // here rather than ship — an error is the negative hue in every room,
    // because a colour vocabulary the operator has to re-learn per surface is
    // not a vocabulary.
    const attr = rules(css).find((r) => r.selector === `[data-surface='${name}']`)!;
    const decls = declarations(attr.body);
    let checked = 0;
    for (const role of ROLES) {
      const decl = decls.find((d) => d.startsWith(`--${prefix}-${role}:`));
      if (!decl) continue; // a register need not spend every role — sensor-log omits caution
      expect(decl, `${file}: --${prefix}-${role} must alias --console-${role}`).toBe(
        `--${prefix}-${role}: var(--console-${role})`,
      );
      checked += 1;
    }
    expect(checked, `${file} aliases none of the four roles`).toBeGreaterThan(0);
  });

  it('holds no colour of its own — every value re-points at the shared layer', () => {
    // One place in the app decides a hue. A literal here would be a second.
    const attr = rules(css).find((r) => r.selector === `[data-surface='${name}']`)!;
    const colourish = declarations(attr.body).filter((d) => /#[0-9a-f]{3,8}\b|\brgba?\(/i.test(d));
    expect(colourish).toEqual([]);
  });

  it('is actually imported by the app shell', () => {
    // A stylesheet nothing imports is a stylesheet that does nothing, and every
    // other assertion in this file would still pass.
    const app = readFileSync(join(__dirname, '..', 'pages', '_app.tsx'), 'utf8');
    expect(app).toContain(`../styles/${file}`);
  });
});

describe('the registers do not collide', () => {
  it('each owns a distinct name', () => {
    const names = REGISTERS.map((r) => r.name);
    expect(new Set(names).size).toBe(names.length);
  });

  it('no register defines another register’s token family', () => {
    // Containment at the token level: `crt.css` must not define `--comms-*`.
    // The quotation is the deliberate exception and it is spelled the other way
    // round — comms names its OWN `--comms-quoted-*` and points them at the
    // shared layer, rather than reaching into crt.css for `--crt-phosphor`.
    for (const { file, prefix } of REGISTERS) {
      const css = cssFor(file);
      const foreign = REGISTERS.filter((r) => r.prefix !== prefix)
        .flatMap((r) => [...css.matchAll(new RegExp(`(--${r.prefix}-[a-z0-9-]+)\\s*:`, 'g'))])
        .map((m) => m[1]);
      expect(foreign, `${file} defines another register's tokens`).toEqual([]);
    }
  });
});
