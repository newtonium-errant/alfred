import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

// HOME'S SLOT CARDS JOIN THE VIEWSCREEN REGISTER — the operator's "this main
// view still has white blocks".
//
// Home was ratified viewscreen and its SHELL went dark, but the cards inside it
// never adopted: the T1/T2/T3 strip, DUTY/RHYTHM/FUEL and the push card were
// still cream panels with green ink sitting on a dark hull.
//
// Chat's Voice container bar is here too, for the same reason and by the same
// treatment — one register over (comms), one operator report later.
//
// WHY THESE AND NOT EVERY WARM PANEL IN THE APP. A component may be warm
// legitimately — `pages/share.tsx` renders `<Layout showNav={false}>` with no
// surface prop, so it is a WARM route, and it renders `IngestForm` (which
// contains `ProvenancePreview` and both upload pills). Rewriting a straddler's
// classes to console tokens fixes the dark route and breaks the warm one. Every
// file listed below renders on exactly ONE route, which is what makes direct
// token adoption correct for them and a marker seam the right answer for the
// straddlers. That reachability claim is asserted below, not assumed — it is the
// whole justification for the treatment.
//
// TWO DIRECTIONS, because "no warm classes" alone is satisfied by a component
// that has stopped rendering anything: the role tokens must still be here. A
// register restyles chrome and never a verdict, so the T1/T2/T3 state dots, the
// voice call's state dots and the danger ink survive the re-hull unchanged.

const ROOT = join(__dirname, '..');
const ADOPTED = [
  'components/feed/SlotBoard.tsx',
  'components/feed/RingsHeader.tsx',
  'components/PushToggle.tsx',
  // Chat's Voice container bar (comms register). Same treatment for the same
  // reason: pages/chat.tsx is its only render site, so it is not a straddler.
  'components/chat/VoicePanel.tsx',
  // The player's Morning Brief document render (viewscreen). Rendered only from
  // pages/player.tsx, so also not a straddler.
  'components/brief/BriefView.tsx',
  // The PAGES themselves. A page is its own route by definition, so it can never
  // straddle — and each of these carried panels of its own that no component fix
  // would have reached.
  'pages/index.tsx',
  'pages/chat.tsx',
] as const;

// `pages/player.tsx` adopts too, but it CANNOT join the list above, because the
// operator ruled three things keep their existing colours: the slide-progress
// dots, the filled Ask button, and the four transport buttons. Those are borders
// and fills rather than light blocks, and they already read correctly on a dark
// hull — so a whole-file "no warm class" scan would be asserting something the
// ruling says is wrong.
//
// The exceptions are therefore ENUMERATED, by shape rather than by line number
// so they survive the file moving. Pinning them is what stops the carve-out
// becoming a hiding place: a NEW warm panel added to the player would have to
// both match one of these shapes and keep the total at six.
const PLAYER_WARM_EXCEPTIONS: ReadonlyArray<[string, RegExp]> = [
  ['slide-progress dots', /rounded-full transition-all/],
  ['the filled Ask button', /data-testid="player-ask-send"|bg-honeydew-600 px-4/],
  ['a transport button', /rounded-full border-\[1\.5px\]/],
];
const PLAYER_WARM_LINE_COUNT = 6; // 1 dots + 1 Ask + 4 transport

const WARM = /\b(?:bg-white|bg-cream|text-cream|(?:bg|border|text|ring|placeholder|divide|from|to|via)-honeydew-\d+)\b/g;
// Role-carrying classes. These are verdict colour and are inviolate.
const ROLE = /\b(?:bg-status-[a-z-]+|text-status-[a-z-]+|text-danger|bg-danger-bg|text-affirm|bg-affirm[a-z-]*|text-caution|text-negative)\b/g;

function src(rel: string): string {
  return readFileSync(join(ROOT, rel), 'utf8');
}

describe('single-route panels adopt their register', () => {
  it.each(ADOPTED)('%s carries no warm class', (rel) => {
    expect([...src(rel).matchAll(WARM)].map((m) => m[0])).toEqual([]);
  });

  it('the scanner can still SEE a warm class — the positive control', () => {
    // Without this, a broken pattern would report every file clean and the pins
    // above would be green against a build that never adopted anything.
    const sample = 'className="rounded-xl border border-honeydew-200 bg-cream text-honeydew-700"';
    expect([...sample.matchAll(WARM)].map((m) => m[0])).toEqual([
      'border-honeydew-200',
      'bg-cream',
      'text-honeydew-700',
    ]);
  });

  it('carries the SAME role colours it carried before the re-hull', () => {
    // The half that makes "no warm classes" mean adoption rather than deletion —
    // and it is a per-token census, not a count, because a re-hull that swapped
    // one verdict hue for another would keep the count and break the vocabulary.
    //
    // NOT `.length > 0` per file, which is what this pin said first and which
    // FAILED honestly: PushToggle has no role-coloured element at all (its "Turn
    // off" border was warm CHROME, not a verdict), so a per-file lower bound was
    // asserting something untrue about a correct component.
    //
    // MEASURED, not transcribed. This pin already caught its own set growing —
    // when pages/index.tsx and pages/chat.tsx joined ADOPTED it went red, which
    // is the correct behaviour and the reason the figures were re-run rather than
    // adjusted by arithmetic. The invocation that produces them, over exactly the
    // files in ADOPTED:
    //   grep -ohE "\b(bg-status-[a-z-]+|text-status-[a-z-]+|text-danger|\
    //     bg-danger-bg|text-affirm|bg-affirm[a-z-]*|text-caution|text-negative)\b" \
    //     <ADOPTED> | sort | uniq -c
    const census: Record<string, number> = {};
    for (const rel of ADOPTED) {
      for (const m of src(rel).matchAll(ROLE)) census[m[0]] = (census[m[0]] ?? 0) + 1;
    }
    expect(census['bg-affirm-deep']).toBe(1); // the undo bar, matched to the deck's
    expect(census['bg-danger-bg']).toBe(4);
    expect(census['bg-status-done']).toBe(1); // VoicePanel's own, a distinct token
    expect(census['bg-status-done-fg']).toBe(2);
    expect(census['bg-status-progress']).toBe(1);
    expect(census['bg-status-progress-fg']).toBe(2);
    expect(census['text-danger']).toBe(6);
    expect(census['text-status-done-fg']).toBe(5);
    expect(census['text-status-progress-fg']).toBe(1);
  });

  it('spends no console token at an alpha it cannot honour', () => {
    // `--console-*` are plain hex, NOT the `<alpha-value>` channel form Tailwind
    // needs, so `bg-console-panel/70` compiles to nothing and the element loses
    // its background silently. The tailwind config says so; this catches the
    // translation that forgets it (the re-hull did, once, on the undo bar —
    // `bg-cream/70` became `bg-console-panel/70` before it became the deck's own
    // `bg-affirm-deep`).
    for (const rel of ADOPTED) {
      expect([...src(rel).matchAll(/\bconsole-[a-z-]+\/\d+/g)].map((m) => m[0])).toEqual([]);
    }
  });

  it('the player keeps warm colour ONLY where the operator ruled it should', () => {
    // NON-GLOBAL on purpose. `WARM` carries /g, and `RegExp.test` on a /g regex
    // advances `lastIndex` between calls — so testing it line by line would skip
    // matches and under-report, which is a pin quietly lying rather than failing.
    const warmLine = new RegExp(WARM.source);
    const warmLines = src('pages/player.tsx')
      .split('\n')
      .filter((l) => warmLine.test(l));
    // POSITIVE CONTROL: there ARE warm lines to classify. If the player were
    // fully converted this pin would be vacuous, and the ruling would have been
    // silently overridden rather than obeyed.
    expect(warmLines.length).toBe(PLAYER_WARM_LINE_COUNT);

    for (const line of warmLines) {
      const matched = PLAYER_WARM_EXCEPTIONS.filter(([, re]) => re.test(line));
      // Named, so a failure says WHICH element is unaccounted for rather than
      // just that a count moved.
      expect(matched.length, `unaccounted warm line: ${line.trim().slice(0, 90)}`).toBeGreaterThan(0);
    }
  });

  it('re-hulls only components no WARM route renders', () => {
    // The reachability claim this treatment rests on. `/share` takes no surface
    // prop and so is warm; it must not reach any of them, or the adoption
    // above would be shipping a dark panel onto a warm page.
    const share = src('pages/share.tsx');
    for (const name of ['SlotBoard', 'RingsHeader', 'PushToggle', 'VoicePanel', 'BriefView']) {
      expect(share).not.toContain(`<${name}`);
    }
    // POSITIVE CONTROL: /share really is warm and really does render page
    // content — so "does not contain" is a fact about this page, not about an
    // empty file or a mis-read path.
    expect(share).toContain('<Layout showNav={false}>');
    expect(share).toContain('<IngestForm');
    expect(share).not.toContain('surface=');
  });
});
