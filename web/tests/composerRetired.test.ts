import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

// THE LEGACY CHAT COMPOSER IS RETIRED, and so is the flag that selected it.
// The unified composer (#97) baked behind NEXT_PUBLIC_UNIFIED_COMPOSER, carried
// real operator load, and the operator signed off on the deletion — so
// `components/chat/Composer.tsx`, `lib/algernon/composerFlag.ts` and /chat's
// selection branch are all gone. This is the re-introduction pin, modelled on
// `briefRetired.test.ts`.
//
// WHAT IT PINS, AND WHY THOSE TWO THINGS. A retirement half-lands in two
// different ways and they fail at different moments. The FILES coming back is
// the obvious one. The subtler one is a READ of the variable reappearing — a
// page that consults it off `process.env` again would
// type-check, pass every other test, and quietly reintroduce a door whose OFF
// branch now renders nothing at all, because the component it used to select
// does not exist. That failure is invisible in CI (nothing stubs the variable)
// and total in the field for anyone whose build lacks it.
//
// IT PINS READS, NOT THE NAME. The name legitimately survives in prose — the
// tombstone in `.env.local.example` and the comments explaining what used to be
// on /chat both mention it on purpose, so that an operator following older
// notes lands on an explanation instead of a silent no-op. Asserting the string
// never appears would forbid exactly the documentation this retirement needs.
// What must not reappear is a read or a stub.
//
// THE NEEDLE IS ASSEMBLED, NOT WRITTEN OUT, and that is load-bearing rather
// than cute. This file scans `tests/` — itself included — so spelling the read
// pattern literally would make the pin match its own source and fail on a clean
// tree. The obvious repair is to exempt this file from the walk, and it is the
// wrong one: an exemption is a hole exactly where someone would later hide a
// read. Composing the string means NOTHING is exempt, and the walk below covers
// every file including this one.

const WEB = join(__dirname, '..');
const SCAN_ROOTS = ['pages', 'components', 'lib', 'tests'];

/** The retired variable's name, assembled so this file never contains it whole. */
const FLAG = ['NEXT', 'PUBLIC', 'UNIFIED', 'COMPOSER'].join('_');

/** Every .ts/.tsx file under the scanned roots, as [relative path, source]. */
function sources(): [string, string][] {
  const out: [string, string][] = [];
  const walk = (dir: string, rel: string) => {
    for (const entry of readdirSync(dir)) {
      const abs = join(dir, entry);
      if (statSync(abs).isDirectory()) {
        walk(abs, `${rel}/${entry}`);
      } else if (/\.tsx?$/.test(entry)) {
        out.push([`${rel}/${entry}`, readFileSync(abs, 'utf8')]);
      }
    }
  };
  for (const root of SCAN_ROOTS) walk(join(WEB, root), root);
  return out;
}

const SRC = sources();

describe('the pin can see what it claims to check', () => {
  // POSITIVE CONTROLS. Every absence assertion below passes vacuously against a
  // walker that reads nothing, or one whose pattern could never match anything.
  // Both halves are controlled here rather than assumed.
  it('the walk reaches real files in every root it claims to scan', () => {
    expect(SRC.length).toBeGreaterThan(200);
    for (const root of SCAN_ROOTS) {
      expect(
        SRC.some(([f]) => f.startsWith(`${root}/`)),
        `the walk found no .ts/.tsx under ${root}/ — an absence pin that never ` +
          'read the directory is green forever',
      ).toBe(true);
    }
    // And it reaches the specific file the retirement rewrote.
    const chat = SRC.find(([f]) => f === 'pages/chat.tsx');
    expect(chat, 'pages/chat.tsx was not scanned').toBeDefined();
    expect(chat![1]).toContain('UnifiedComposer');
  });

  it('a LIVE NEXT_PUBLIC read is detectable by the same pattern', () => {
    // The control that makes "no unified-composer read" mean something. If the
    // pattern below stopped matching env reads at all, every assertion in the
    // next block would pass against a page that had reintroduced the flag.
    // `NEXT_PUBLIC_VOICE_ENABLED` is the sibling DISPLAY flag and is genuinely
    // read in production (`components/chat/VoicePanel.tsx`).
    const readers = SRC.filter(([, s]) => s.includes('process.env.NEXT_PUBLIC_VOICE_ENABLED'));
    expect(
      readers.map(([f]) => f),
      'no file reads NEXT_PUBLIC_VOICE_ENABLED — either the voice flag was ' +
        'retired too (in which case pick another live NEXT_PUBLIC_* as the ' +
        'control) or this walker has stopped seeing source',
    ).toContain('components/chat/VoicePanel.tsx');
  });
});

describe('the legacy composer and its flag are gone', () => {
  it('components/chat/Composer.tsx does not exist', () => {
    expect(
      existsSync(join(WEB, 'components', 'chat', 'Composer.tsx')),
      'The legacy chat composer is back. It is RETIRED — the unified composer ' +
        '(#97) replaced it after a bake window the operator signed off on. Its ' +
        'shared behaviour lives in lib/algernon/useComposerText.ts and ' +
        'lib/algernon/imageAttach.ts, which is where a fix belongs. Confirm ' +
        'operator intent before restoring the component.',
    ).toBe(false);
  });

  it('lib/algernon/composerFlag.ts does not exist', () => {
    expect(
      existsSync(join(WEB, 'lib', 'algernon', 'composerFlag.ts')),
      'The #97 deploy gate is back. There is nothing for it to select — the ' +
        'composer it gated OFF to is deleted, so a restored flag ships a /chat ' +
        'that renders no composer at all on any build without the variable.',
    ).toBe(false);
  });

  it('nothing READS the flag, and nothing stubs it', () => {
    // A read in production and a stub in a test are the same regression seen
    // from two sides, so both are named. Prose mentions are deliberately
    // allowed — see this file's docstring.
    const offenders = SRC.filter(
      ([, s]) =>
        s.includes(`process.env.${FLAG}`) ||
        s.includes(`stubEnv('${FLAG}'`) ||
        s.includes(`stubEnv("${FLAG}"`),
    ).map(([f]) => f);

    expect(
      offenders,
      `${FLAG} is being read again. The flag is RETIRED ` +
        'and its OFF branch no longer exists, so a build without the variable ' +
        'would render /chat with no composer. If a new gate is genuinely ' +
        'needed, it needs a new name and both of its branches.',
    ).toEqual([]);
  });

  it('nothing imports the flag module', () => {
    const importers = SRC.filter(([, s]) => /from\s+['"][^'"]*composerFlag['"]/.test(s)).map(([f]) => f);
    expect(importers, 'something imports the deleted composerFlag module').toEqual([]);
  });

  it('nothing imports the legacy component', () => {
    // NARROWED ON PURPOSE: `chat/UnifiedComposer` must not match, and neither
    // must the home-page view composer at `lib/algernon/composer`, which is a
    // different thing that shares the word.
    const importers = SRC.filter(([, s]) =>
      /from\s+['"][^'"]*\/chat\/Composer['"]/.test(s),
    ).map(([f]) => f);
    expect(importers, 'something imports the deleted chat/Composer component').toEqual([]);
  });
});

describe('/chat renders the unified composer unconditionally', () => {
  const chat = () => readFileSync(join(WEB, 'pages', 'chat.tsx'), 'utf8');

  it('mounts it with no gate in front of it', () => {
    const src = chat();
    expect(src).toContain('<UnifiedComposer');
    expect(
      src.includes('unifiedComposerEnabled'),
      'pages/chat.tsx consults the retired flag helper again',
    ).toBe(false);
  });

  it('the page still renders SOME conditional — the pin is narrowing, not emptiness', () => {
    // Vacuity control in the briefRetired style: a chat page stripped of every
    // conditional would satisfy the assertion above while having deleted far
    // more than this lane intended. The auth gate, the boot state and the error
    // banner are all still conditional — only the composer's gate went.
    const src = chat();
    expect(src).toContain('if (!authed)');
    expect(src).toContain('{booting ? (');
    expect(src).toContain('{error && (');
  });
});

describe('the operator-facing tombstone is intact', () => {
  it('.env.local.example names the retired variable and says it does nothing', () => {
    // The one place the NAME must survive. An operator following older notes
    // greps for it; deleting the entry outright would return nothing and leave
    // them to conclude the docs were stale rather than the flag retired.
    const env = readFileSync(join(WEB, '.env.local.example'), 'utf8');
    expect(env).toContain('NEXT_PUBLIC_UNIFIED_COMPOSER');
    expect(env).toContain('NO LONGER EXISTS');
  });

  it('and does not carry it as a live assignment', () => {
    // Every line mentioning it must be a comment. An uncommented entry would
    // read as configuration to anyone copying this file for a new instance.
    const live = readFileSync(join(WEB, '.env.local.example'), 'utf8')
      .split('\n')
      .filter((l) => l.includes('NEXT_PUBLIC_UNIFIED_COMPOSER') && !l.trimStart().startsWith('#'));
    expect(live, 'the retired flag is set as live config in .env.local.example').toEqual([]);
  });
});
