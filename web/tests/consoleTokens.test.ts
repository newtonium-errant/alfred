import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  CONSOLE_DATUM,
  CONSOLE_LABEL,
  INSTANCE_ACCENT_CLASSES,
  ROLE_BORDER_CLASS,
  ROLE_FILL_CLASS,
  ROLE_TEXT_CLASS,
  ROLE_WASH_CLASS,
  instanceAccentClass,
  railClassForWeight,
  roleChipClass,
  roleForVerdict,
  roleForWeight,
  type ConsoleRole,
} from '../lib/algernon/consoleTokens';

// The console identity layer (interface-reimagine arc, Phase B deck lane).
//
// Two DIFFERENT things are pinned here and they fail in different ways:
//
//   1. The MEANING mapping (verdict/weight/instance → role). A wrong mapping
//      still renders — in the wrong colour, saying the wrong thing about what
//      just happened. Only an assertion can catch it.
//
//   2. The WIRING (does the class name a real token; does the token name a real
//      CSS variable). This is the failure mode with no symptom: a typo'd
//      `bg-affirm-washed` is not an error anywhere — Tailwind emits no rule,
//      the element renders with no background, and every other test stays
//      green. Same for `var(--console-affrim)` in the Tailwind config. These
//      two chains are checked against the actual config and the actual
//      stylesheet, not against a restated copy of them.

// eslint-disable-next-line @typescript-eslint/no-var-requires
const tailwindConfig = require('../tailwind.config.cjs') as {
  theme: { extend: { colors: Record<string, unknown> } };
};
const CONSOLE_CSS = readFileSync(resolve(__dirname, '../styles/console.css'), 'utf8');

const ROLES: ConsoleRole[] = ['affirm', 'negative', 'caution', 'info'];

/**
 * Every Tailwind colour name the config defines, in the `family-shade` (and
 * bare `family`, for a DEFAULT) spelling a class uses. Built from the config
 * object itself so this can never drift from what Tailwind actually generates.
 */
function definedColorNames(): Set<string> {
  const names = new Set<string>();
  const colors = tailwindConfig.theme.extend.colors;
  for (const [family, value] of Object.entries(colors)) {
    if (typeof value === 'string') {
      names.add(family);
      continue;
    }
    for (const shade of Object.keys(value as Record<string, unknown>)) {
      names.add(shade === 'DEFAULT' ? family : `${family}-${shade}`);
    }
  }
  return names;
}

/** The colour name a utility class refers to, or null if it isn't a colour utility. */
function colorNameOf(cls: string): string | null {
  const m = /^(?:bg|text|border)-(.+)$/.exec(cls);
  return m ? m[1] : null;
}

describe('console identity — the meaning mapping', () => {
  it('draws the judgement axis in affirm and negative', () => {
    expect(roleForVerdict('affirm')).toBe('affirm');
    expect(roleForVerdict('reject')).toBe('negative');
  });

  it('draws BOTH defer verdicts in caution, and neither in negative', () => {
    // The load-bearing one. `skip` (a slot candidate set aside for the session)
    // and `snooze` mean different things to the store, and the ruling behind
    // #14 is explicit that no one control may mean both — but they are the same
    // AXIS, and the axis is what colour reports (D3: horizontal judges,
    // vertical defers). They are told apart by their words, not their hue.
    expect(roleForVerdict('snooze')).toBe('caution');
    expect(roleForVerdict('skip')).toBe('caution');
    expect(roleForVerdict('skip')).toBe(roleForVerdict('snooze'));

    // The failure this exists to prevent: a set-aside drawn as a rejection
    // would tell the operator a card that is COMING BACK was turned down.
    expect(roleForVerdict('skip')).not.toBe(roleForVerdict('reject'));
  });

  it('carries no verdict when nothing was judged', () => {
    expect(roleForVerdict(null)).toBe('info');
  });

  it('draws a heavy verb in caution and a light one without it', () => {
    // Heavy shares the defer family's amber deliberately: a heavy verb does not
    // fire on the motion, it arms (D4), so at a glance it belongs with "not
    // yet" rather than with either verdict.
    expect(roleForWeight('heavy')).toBe('caution');
    expect(roleForWeight('light')).not.toBe('caution');
  });

  it('marks a heavy card rail with the hatch, not colour alone', () => {
    // Positive control on both sides: the heavy rail must be hatched AND the
    // light rail must not be, or "the rail reports weight" is unfalsifiable.
    expect(railClassForWeight(true)).toContain('console-hatch');
    expect(railClassForWeight(false)).not.toContain('console-hatch');
  });
});

describe('console identity — instance provenance is not a verdict', () => {
  it('gives the same instance the same accent every time', () => {
    // Stability is the property: a chip that changed colour between deploys
    // would train recognition and then betray it.
    const a = instanceAccentClass('Salem');
    expect(instanceAccentClass('Salem')).toBe(a);
    expect(instanceAccentClass('salem')).toBe(a); // case-folded
    expect(instanceAccentClass('  Salem  ')).toBe(a); // trimmed
  });

  it('always answers with one of the declared accent slots', () => {
    // Includes names nobody has registered — the whole point of hashing rather
    // than keeping a per-instance map is that a fifth instance already works.
    for (const name of ['Salem', 'KAL-LE', 'Hypatia', 'VERA', 'an-instance-nobody-named-yet', '']) {
      expect(INSTANCE_ACCENT_CLASSES).toContain(instanceAccentClass(name));
    }
  });

  it('separates at least two of the real instances', () => {
    // A hash that answered one slot for everything would satisfy every
    // assertion above while making the accent useless. Four names into four
    // slots need not be a perfect permutation (collisions are an accepted
    // cost), but a constant function is a broken one.
    const slots = new Set(['Salem', 'KAL-LE', 'Hypatia', 'VERA'].map(instanceAccentClass));
    expect(slots.size).toBeGreaterThan(1);
  });

  it('never draws an instance in a function role', () => {
    // The invariant behind the departure from the approved sketch (which
    // tinted Salem's chip the same teal as affirm): provenance must not be
    // readable as a judgement about the item.
    const roleNames = ROLES.map((r) => r);
    for (const cls of INSTANCE_ACCENT_CLASSES) {
      for (const role of roleNames) {
        expect(cls).not.toContain(role);
      }
    }
  });
});

describe('console identity — the wiring has no silent failure', () => {
  it('spends every role through a class Tailwind actually defines', () => {
    const defined = definedColorNames();
    const maps = { ROLE_TEXT_CLASS, ROLE_BORDER_CLASS, ROLE_WASH_CLASS, ROLE_FILL_CLASS };

    // Guard the denominator: if a map were empty this loop would pass while
    // checking nothing.
    let checked = 0;
    for (const [mapName, map] of Object.entries(maps)) {
      for (const role of ROLES) {
        const entry = map[role];
        expect(entry, `${mapName}.${role} is missing`).toBeTruthy();
        for (const cls of entry.split(/\s+/)) {
          const color = colorNameOf(cls);
          expect(color, `${mapName}.${role}: "${cls}" is not a colour utility`).not.toBeNull();
          expect(defined, `${mapName}.${role}: no Tailwind colour named "${color}"`).toContain(color);
          checked += 1;
        }
      }
    }
    expect(checked).toBeGreaterThanOrEqual(ROLES.length * Object.keys(maps).length);
  });

  it('builds a role chip out of real classes too', () => {
    const defined = definedColorNames();
    for (const role of ROLES) {
      const parts = roleChipClass(role).split(/\s+/);
      expect(parts.length).toBe(3); // wash + border + text
      for (const cls of parts) {
        expect(defined).toContain(colorNameOf(cls));
      }
    }
  });

  it('draws instance accents and the shared type styles from real classes', () => {
    const defined = definedColorNames();
    for (const cls of INSTANCE_ACCENT_CLASSES) {
      for (const part of cls.split(/\s+/)) {
        expect(defined, `accent class "${part}"`).toContain(colorNameOf(part));
      }
    }
    // The two shared type styles carry colour utilities among their sizing ones.
    for (const style of [CONSOLE_LABEL, CONSOLE_DATUM]) {
      for (const part of style.split(/\s+/)) {
        const color = colorNameOf(part);
        // `text-[10px]` is a size, not a colour — skip arbitrary values.
        if (color === null || color.startsWith('[')) continue;
        expect(defined, `type style class "${part}"`).toContain(color);
      }
    }
  });

  it('points every console token at a CSS variable that console.css defines', () => {
    // The other half of the same silent chain: a Tailwind colour whose value is
    // `var(--console-typo)` generates a perfectly valid rule that resolves to
    // nothing. Checked against the stylesheet's real text.
    const colors = tailwindConfig.theme.extend.colors as Record<string, unknown>;
    const consoleFamilies = ['console', 'affirm', 'negative', 'caution', 'info', 'accent'];
    let checked = 0;
    for (const family of consoleFamilies) {
      const value = colors[family];
      expect(value, `tailwind colours are missing the "${family}" family`).toBeTruthy();
      for (const raw of Object.values(value as Record<string, string>)) {
        const m = /^var\((--[a-z0-9-]+)\)$/.exec(raw);
        expect(m, `"${raw}" is not a var() reference`).not.toBeNull();
        const varName = (m as RegExpExecArray)[1];
        expect(CONSOLE_CSS, `console.css never defines ${varName}`).toContain(`${varName}:`);
        checked += 1;
      }
    }
    // Positive control on the denominator — this must actually have looked at
    // the whole palette, not at an empty family list.
    expect(checked).toBeGreaterThan(25);
  });

  it('defines the shape primitives the identity refers to by name', () => {
    // `console-hatch` is returned by railClassForWeight and is a plain CSS
    // class, not a Tailwind utility — so nothing else would notice it being
    // renamed or dropped.
    for (const cls of ['.console-hatch', '.console-elbow', '.console-railseg', '.console-accreted']) {
      expect(CONSOLE_CSS).toContain(cls);
    }
    expect(railClassForWeight(true).split(/\s+/)).toContain('console-hatch');
  });
});
