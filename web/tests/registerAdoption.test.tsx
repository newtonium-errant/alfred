import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { exactToken } from './_exactToken';
import { render } from '@testing-library/react';
import { ProvenancePreview } from '../components/ingest/ProvenancePreview';
import { EmptyState } from '../components/EmptyState';

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

// ── THE `ui-panel` SEAM ─────────────────────────────────────────────────────
//
// The STRADDLERS. `ProvenancePreview` renders on /ingest (crt) and on /share
// (warm — `<Layout showNav={false}>`, no surface prop); `EmptyState` renders on
// six dark routes and on /share. Neither can be fixed by rewriting classes: that
// fixes the dark route and ships a dark panel onto the warm one. So the warm
// chrome stays the UNMARKED DEFAULT and the registers reach in through an opt-in
// marker, exactly as they already do for `ui-field` and `ui-btn`.
//
// PARAMETRIZED OVER THE REGISTERS A MARKED PANEL CAN ACTUALLY REACH, and
// `sensor-log` is deliberately NOT one of them — with its own pin below saying
// so, so the omission is a recorded decision rather than a hole.
//
// That started as the opposite. The family-hole lesson says a guard written for
// the members that prompted it leaves a gap, so the first cut put `.ui-panel`
// into all four stylesheets — and `sensorLogSkin.test.tsx` immediately went red
// with twelve orphaned selectors. It was right: no marked panel renders on
// /feed, so those rules matched nothing. Dead CSS is dead CSS whether it comes
// from carelessness or from over-generalising, and the fix was to delete the
// rules rather than to widen the pin that caught them. Completeness is measured
// against what is REACHABLE, not against the list of registers.

const PANEL_REGISTERS = [
  { surface: 'viewscreen', prefix: 'viewscreen', file: 'viewscreen.css' },
  { surface: 'crt', prefix: 'crt', file: 'crt.css' },
  { surface: 'comms', prefix: 'comms', file: 'comms.css' },
] as const;

// The registers a FENCED BLOCK can reach — a different set from the panel's,
// because reachability is per-marker rather than per-seam. `EvidenceBody` is
// rendered by both `FeedRow` (feed / sensor-log) and `DeckCard` (deck /
// console), which is why this list is four long where the panel's is three, and
// why crt is absent from it: crt claims `.ui-panel`, but no FencedText consumer
// renders on batch / ingest / login.
const CODE_REGISTERS = [
  { surface: 'comms', prefix: 'comms', file: 'comms.css' },
  { surface: 'viewscreen', prefix: 'viewscreen', file: 'viewscreen.css' },
  { surface: 'sensor-log', prefix: 'sensor', file: 'sensorLog.css' },
  { surface: 'console', prefix: 'console', file: 'console.css' },
] as const;

// ── THE MARKER FAMILY ───────────────────────────────────────────────────────
//
// THE LIST IS THE GUARD. Everything below is parametrised over MARKER × REGISTER
// rather than over registers alone, so a new marker inherits all three seam
// rules by being added here — one line — instead of by someone remembering to
// copy three assertions.
//
// This file already carried the family-hole lesson in its own header, and the
// fenced-seam lane then added two markers to the seam without adding them here.
// The guard read as if it covered the seam; it covered `ui-panel`. Painting
// `ui-code` with `--console-affirm` and `ui-code-label` with `--console-negative`
// left the suite fully green — role hues were inviolate by COMMENT on the two
// newest members. That is the family hole reappearing one file from the sentence
// describing it, which is the argument for a list over a local assertion: a spot
// fix would have closed these two and left the third member to repeat it.
//
// Each member names its OWN reachable registers. Completeness is measured
// against what is REACHABLE — the other half of this file's lesson, and the
// reason `sensor-log` is absent from the panel row and crt from the code rows.
const MARKER_FAMILY = [
  { marker: 'ui-panel', registers: PANEL_REGISTERS },
  { marker: 'ui-code', registers: CODE_REGISTERS },
  { marker: 'ui-code-label', registers: CODE_REGISTERS },
] as const;

/** Every (marker, register) pair the family claims, flattened for `describe.each`. */
const FAMILY_PAIRS = MARKER_FAMILY.flatMap(({ marker, registers }) =>
  registers.map((r) => ({ marker, ...r })),
);

/**
 * A bare (unscoped) declaration of exactly this marker.
 *
 * EXACT TOKEN, not a prefix. `\b` matches between `e` and `-`, so `\.ui-code\b`
 * reads `.ui-code-label` as a bare `.ui-code` — a marker's own sibling would be
 * reported as its violation. The negative lookahead is what makes the check
 * about the class it names; the same substring trap scored a reviewer's
 * renamed-selector mutation lower than a deletion on this very family.
 */
function bareRule(marker: string): RegExp {
  return new RegExp(`^\\s*${exactToken(`.${marker}`).source}`);
}

/**
 * The scoped rule for exactly this marker on this surface.
 *
 * EXACT TOKEN for the same reason as `bareRule`, and it is worth stating why
 * this one needed it too: an `includes()` filter here matches `.ui-code-label`
 * when extracting `.ui-code`, because the sibling's selector CONTAINS the
 * member's. Spending a role hue on the label then reddened the block's row as
 * well — over-reporting rather than missing, but a row that fails for its
 * sibling's fault names the wrong rule to whoever reads it, and the next
 * `ui-code-*` member would fold into that row invisibly.
 */
function scopedRule(surface: string, marker: string): RegExp {
  return new RegExp(`\\[data-surface='${surface}'\\]\\s+${exactToken(`.${marker}`).source}`);
}

const MARKED_PANELS = [
  ['components/ingest/ProvenancePreview.tsx', 'ui-panel'],
  ['components/EmptyState.tsx', 'ui-panel'],
] as const;

function css(file: string): string {
  return readFileSync(join(ROOT, 'styles', file), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
}

describe.each(FAMILY_PAIRS)('$surface reaches .$marker', ({ marker, surface, prefix, file }) => {
  const sheet = css(file);

  it('declares the rule under its own attribute', () => {
    expect(sheet).toContain(`[data-surface='${surface}'] .${marker}`);
    // Scoped, never bare: an unscoped marker rule would repaint it on every
    // surface including the warm ones, which is the defect the seam exists to
    // avoid rather than a shortcut to it.
    const bare = sheet.split('\n').filter((l) => bareRule(marker).test(l));
    expect(bare).toEqual([]);
  });

  it('spends only its OWN tokens, and never a role', () => {
    // THE ASSERTION THE TWO NEWEST MARKERS WERE MISSING. A register restyles
    // chrome, never a verdict — so `raise`/`panel`/`edge`/`ink` only, and no
    // affirm/negative/caution/info from any prefix.
    const block = sheet
      .split('}')
      .filter((c) => scopedRule(surface, marker).test(c))
      .join('}');
    const vars = [...block.matchAll(/var\(--([a-z-]+)\)/g)].map((m) => m[1]);
    expect(vars.length).toBeGreaterThan(0); // positive control: there ARE declarations
    for (const v of vars) {
      expect(v.startsWith(`${prefix}-`)).toBe(true);
      expect(/-(affirm|negative|caution|info)/.test(v)).toBe(false);
    }
  });
});

describe.each(PANEL_REGISTERS)('$surface reaches adopted panels', () => {
  // The sheet-level assertions moved to the MARKER_FAMILY block above; what
  // stays here is the marker-specific DOM control, which is about
  // ProvenancePreview and EmptyState carrying `.ui-panel` at all.
  it('has a marked panel to reach — the vacuity control', () => {
    // RENDERED, not grepped. This pin's first form was `src(file).toContain
    // ('ui-panel')`, and removing the marker from ProvenancePreview did not red
    // it — because the COMMENT above the marker says the word "ui-panel" too.
    // It was measuring a string in a file rather than a class on an element: the
    // guard-checks-a-proxy failure, caught by mutating the thing it claimed to
    // protect. The DOM cannot be fooled by prose.
    const { container: panel } = render(
      <ProvenancePreview
        target={undefined}
        recordType="document"
        title=""
        source=""
        ingestedBy="andrew"
        originInstance="Salem"
      />,
    );
    expect(panel.querySelector('.ui-panel')).not.toBeNull();

    const { container: empty } = render(<EmptyState icon="💬" message="nothing here" />);
    expect(empty.querySelector('.ui-panel')).not.toBeNull();
  });
});

describe('/share stays warm — the regression the seam exists to prevent', () => {
  it('renders a straddler whose warm chrome is intact and unreachable by any register', () => {
    // /share takes NO surface prop, so no `[data-surface='…'] .ui-panel` rule can
    // match anything on it. That is what makes the marker safe: adopting cost
    // /share nothing.
    const share = src('pages/share.tsx');
    expect(share).toContain('<IngestForm');
    expect(share).not.toContain('surface=');

    // The warm chrome is STILL THERE on the straddler — it is the unmarked
    // default, not a leftover. If a later hand converted it to console tokens
    // this reds, which is the exact regression the finding predicted.
    // RENDERED, not grepped — reviewer-g2's WARN-1. This block was the THIRD
    // string-grep in this file that a COMMENT could satisfy: the straddlers'
    // source explains the marker in prose, so `toContain('ui-panel')` passed on
    // the explanation rather than on the class. The vacuity control above had
    // already been converted for exactly this; the same confession applies here,
    // and one instance fixed while its sibling stays is how a species survives.
    const { container: panel } = render(
      <ProvenancePreview
        target={undefined}
        recordType="document"
        title=""
        source=""
        ingestedBy="andrew"
        originInstance="Salem"
      />,
    );
    const panelRoot = panel.querySelector('.ui-panel');
    expect(panelRoot).not.toBeNull();
    // The WARM chrome is on the SAME element as the marker — that adjacency is
    // the whole contract: unmarked default plus opt-in reach, so /share (which
    // sets no data-surface) renders it exactly as it always did.
    expect(panelRoot?.className).toContain('bg-honeydew-100');
    expect(panelRoot?.className).toContain('border-honeydew-300');

    const { container: empty } = render(<EmptyState icon="💬" message="nothing here" />);
    const disc = empty.querySelector('.ui-panel');
    expect(disc).not.toBeNull();
    expect(disc?.className).toContain('bg-honeydew-100');
  });

  it('the straddlers are NOT in the token-adopted set — the two treatments stay apart', () => {
    // A straddler that also got the token treatment would be dark on /share. The
    // two lists must never intersect.
    for (const [rel] of MARKED_PANELS) {
      expect(ADOPTED as readonly string[]).not.toContain(rel);
    }
  });
});

describe('sensor-log DOES have panel rules — the premise flipped', () => {
  it('declares them, because a marked panel is now reachable from /feed', () => {
    // THIS PIN USED TO ASSERT THE OPPOSITE, and it was right when it was
    // written. It read: "sensor-log deliberately has no panel rules — declares
    // none, because no marked panel is reachable from /feed", and it paired the
    // absence with the reachability fact precisely so that it would red rather
    // than quietly persist if a marked panel ever landed on the feed.
    //
    // IT DID. The chat-polish lane put `ui-panel` on `FeedRow` itself — the feed
    // row IS the marked panel now, and it renders on /feed and on home. So the
    // rule this pin guarded the absence of is owed, and the pin is FLIPPED with
    // the new premise recorded rather than deleted: a deleted pin takes the
    // reasoning with it, and the next person to wonder why sensor-log carries
    // panel rules would find nothing.
    //
    // The teeth are unchanged and still point both ways — the rule must exist
    // AND the reachability fact that makes it non-dead must hold.
    expect(css('sensorLog.css')).toContain('.ui-panel');

    // The reachability half, asserted rather than asserted-about: the row that
    // renders on /feed carries the marker.
    expect(src('components/feed/FeedRow.tsx')).toContain('ui-panel');
    expect(src('pages/feed.tsx')).toContain('<FeedRow');

    // POSITIVE CONTROL, in the direction that can still go wrong. The two
    // ORIGINAL marked panels are ingest-side and are still NOT on the feed, so
    // this register's new rules are earned by FeedRow specifically and not by a
    // marker that has quietly spread everywhere.
    const feedSurfaces = [src('pages/feed.tsx'), src('components/feed/FeedRow.tsx')].join('\n');
    for (const [rel] of MARKED_PANELS) {
      const component = rel.split('/').pop()!.replace('.tsx', '');
      expect(feedSurfaces).not.toContain(`<${component}`);
    }
    expect(src('components/ingest/IngestForm.tsx')).toContain('<ProvenancePreview');
  });
});
