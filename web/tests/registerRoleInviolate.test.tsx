import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

// Layout renders next/link; jsdom has no router mounted.
vi.mock('next/router', () => ({
  useRouter: () => ({
    replace: vi.fn(), push: vi.fn(), asPath: '/', query: {},
    events: { on: vi.fn(), off: vi.fn() },
  }),
}));
import { Layout } from '../components/Layout';
import { Button } from '../components/ui/button';
import { SENSOR_SURFACE } from '../lib/algernon/sensorSurface';
import { VIEWSCREEN_SURFACE } from '../lib/algernon/viewscreenSurface';
import { CRT_SURFACE } from '../lib/algernon/crtSurface';
import { COMMS_SURFACE } from '../lib/algernon/commsSurface';

// A REGISTER RESTYLES CHROME. IT NEVER RESTYLES A VERDICT.
//
// The operator has learned one colour vocabulary: affirm/negative/caution/info
// mean the same things on every surface. A register that repainted them would
// make him re-learn it per room, which is not a vocabulary.
//
// THE DEFECT THIS PREVENTS IS A SPECIFICITY ACCIDENT, not a design choice — and
// it is the shape a first cut takes. `[data-surface='crt'] button` is (0,1,1);
// a Tailwind role class like `.text-danger` is (0,1,0). The element selector
// silently WINS. Nothing about that reads as wrong in a diff, no test of the
// button notices, and the verdict colours quietly go. There are 22 role-coloured
// usages on the crt/comms pages; an element rule reaches every one.
//
// So the registers reach controls through opt-in MARKERS (`ui-field` /
// `ui-btn`), and role-coloured markup never carries one. These pins hold both
// halves of that: the DOM half (a role-coloured control keeps its role class and
// takes no marker, inside EVERY hull) and the structural half (no register
// stylesheet contains a rule that could outrank a role class in the first
// place).
//
// PARAMETRIZED OVER ALL FOUR REGISTERS. The lesson the elbow pin and the token
// guard both taught this week: a guard written for the members that prompted it
// leaves the family with a hole, in whichever direction the family later grows.

const REGISTERS = [
  { name: SENSOR_SURFACE, file: 'sensorLog.css' },
  { name: VIEWSCREEN_SURFACE, file: 'viewscreen.css' },
  { name: CRT_SURFACE, file: 'crt.css' },
  { name: COMMS_SURFACE, file: 'comms.css' },
] as const;

function cssFor(file: string): string {
  return readFileSync(join(__dirname, '..', 'styles', file), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, ''); // prose about code reads as code — strip it
}

function selectors(css: string): string[] {
  return css
    .split('}')
    .map((chunk) => chunk.slice(0, chunk.indexOf('{')))
    .filter((head) => head.trim() && !head.trim().startsWith('@'))
    .flatMap((head) => head.split(','))
    .map((s) => s.replace(/\s+/g, ' ').trim())
    .filter(Boolean);
}

describe.each(REGISTERS)('$name keeps the verdict hues out of reach', ({ name, file }) => {
  const css = cssFor(file);

  it('has rules to check — the positive control', () => {
    expect(selectors(css).length).toBeGreaterThan(3);
  });

  it('re-points a role class only to the SAME role', () => {
    // NOT "never names a role class" — that was this pin's first form and it
    // failed the INCUMBENT for doing the right thing. `sensorLog.css` maps
    // `.text-danger -> var(--sensor-negative)`: the warm role class re-expressed
    // in the register's own vocabulary, where `--sensor-negative` itself aliases
    // `--console-negative`. Meaning preserved, hue preserved, palette local.
    // That is the pattern, not the violation.
    //
    // The violation is a role re-pointed to a DIFFERENT role — `.text-danger`
    // painted with an affirm token. That is what makes the operator re-learn
    // his colour vocabulary per room, and it is what this now checks.
    const ROLE_OF: Record<string, string> = {
      danger: 'negative', negative: 'negative',
      affirm: 'affirm', caution: 'caution', info: 'info',
    };
    const rules = css
      .split('}')
      .map((chunk) => {
        const i = chunk.indexOf('{');
        return i < 0 ? null : { sel: chunk.slice(0, i), body: chunk.slice(i + 1) };
      })
      .filter((r): r is { sel: string; body: string } => r != null);

    const offenders: string[] = [];
    for (const { sel, body } of rules) {
      const named = [...sel.matchAll(/\.(?:text|bg|border)-(affirm|negative|caution|info|danger)\b/g)]
        .map((m) => ROLE_OF[m[1]]);
      if (named.length === 0) continue;
      const assigned = [...body.matchAll(/var\(--[a-z-]*?-(affirm|negative|caution|info)(?:-[a-z]+)?\)/g)]
        .map((m) => m[1]);
      for (const want of new Set(named)) {
        for (const got of assigned) {
          if (got !== want) offenders.push(`${sel.trim()} -> ${got} (expected ${want})`);
        }
      }
    }
    expect(
      offenders,
      `${file} paints one role's class with another role's token — the operator ` +
        'would have to re-learn the colour vocabulary in this room.',
    ).toEqual([]);
  });

  it('scopes every surface rule to a CLASS, never a bare element', () => {
    // THE SPECIFICITY PIN, and the one that actually stops the accident.
    // Under a `[data-surface]` scope, a rule must end in a class (an owned
    // `.<register>-*` or an opted-in `.ui-*`). A bare element tail —
    // `[data-surface='crt'] button` — is (0,1,1) and outranks every role class
    // on that element. Pseudo-elements/classes on a class tail are fine.
    const scoped = selectors(css).filter((s) => s.includes('[data-surface='));
    const bareElementTail = scoped.filter((s) => {
      const tail = s.split(' ').pop() || '';
      const head = tail.split(':')[0]; // drop :focus-visible / ::placeholder
      // A bare TYPE selector only. An ATTRIBUTE tail (`[data-testid='feed-row']`)
      // is (0,1,0) — class-equivalent, not the hazard; the first cut of this pin
      // flagged those too and would have failed sensorLog.css for a rule with no
      // specificity problem at all. Only a type selector adds the (0,0,1) that
      // tips a scoped rule over a role class.
      return /^[a-z][a-z0-9]*$/.test(head);
    });
    expect(
      bareElementTail,
      `${file} scopes a rule to a bare ELEMENT under [data-surface]. That is ` +
        '(0,1,1) and silently outranks a role class at (0,1,0) — the verdict ' +
        'colours go without a diff looking wrong. Reach controls through the ' +
        'ui-* marker instead.',
    ).toEqual([]);
  });
});

describe.each(REGISTERS)('a role-coloured control survives inside $name', ({ name }) => {
  it('keeps its role class and takes no register marker', () => {
    const { unmount } = render(
      <Layout onSignOut={() => {}} surface={name}>
        <Button variant="destructive" data-testid="role-control">
          Delete
        </Button>
      </Layout>,
    );
    const cls = screen.getByTestId('role-control').className;
    expect(cls).toContain('text-danger');
    // The marker is what a register can reach. A role-coloured control must not
    // carry one — that is the whole containment, expressed in the markup.
    expect(cls).not.toContain('ui-btn');
    unmount();
  });

  it('a NEUTRAL control in the same hull DOES take the marker', () => {
    // The vacuity control. Without it, a build where nothing carries `ui-btn`
    // would satisfy every assertion above while the registers reached nothing.
    const { unmount } = render(
      <Layout onSignOut={() => {}} surface={name}>
        <Button variant="outline" data-testid="neutral-control">
          Cancel
        </Button>
      </Layout>,
    );
    expect(screen.getByTestId('neutral-control').className).toContain('ui-btn');
    unmount();
  });
});
