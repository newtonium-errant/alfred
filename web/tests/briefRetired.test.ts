import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { slideDeepLink } from '../lib/algernon/player';
import { SURFACE_PATHS } from '../lib/algernon/contactRouter';

// `/brief` IS RETIRED — the player replaces it (console-completion arc,
// operator-ratified 2026-08-14). This is the re-introduction pin, modelled on
// `tests/telegram/test_today_command.py`'s routines-section pin: assert the
// OUTPUT is gone AND the WIRING is gone, because the two fail at different
// moments. A nav link restored while the redirect still stands would pass an
// output-only check and ship a menu entry that bounces.
//
// WHY IT IS PINNED RATHER THAN LEFT TO REVIEW. The page looks trivially
// restorable — it was 197 lines and its component still exists — so the natural
// reading of a future diff is "someone deleted the brief page, put it back."
// The failure messages below carry the ruling and who to ask, so a future agent
// meets the decision at the assertion instead of undoing it.

const WEB = join(__dirname, '..');
const read = (...p: string[]) => readFileSync(join(WEB, ...p), 'utf8');

describe('the pin can see what it claims to check', () => {
  // POSITIVE CONTROL. Every absence assertion below passes vacuously if the
  // file moved or was renamed — an absence check against a path that no longer
  // exists is green forever and guards nothing.
  it('reads the real Layout and the real page', () => {
    const layout = read('components', 'Layout.tsx');
    expect(layout.length).toBeGreaterThan(2000);
    expect(layout).toContain('NAV_LINKS');
    expect(read('pages', 'brief.tsx').length).toBeGreaterThan(200);
  });

  it('the nav array still HAS entries — so "no /brief" is narrowing, not emptiness', () => {
    // Without this, deleting NAV_LINKS entirely would satisfy the pin below.
    const layout = read('components', 'Layout.tsx');
    for (const href of ['/chat', '/deck', '/feed']) {
      expect(layout).toContain(`href: '${href}'`);
    }
  });
});

describe('/brief is retired', () => {
  it('is not in the nav', () => {
    expect(
      read('components', 'Layout.tsx').includes("href: '/brief'"),
      "/brief was re-added to NAV_LINKS — it is RETIRED (the player replaces it, " +
        'operator-ratified 2026-08-14) and the route only redirects. A nav entry ' +
        'that bounces lies about where it goes. Confirm operator intent before ' +
        'restoring it.',
    ).toBe(false);
  });

  it('the route redirects to the player rather than rendering', () => {
    const page = read('pages', 'brief.tsx');
    expect(page).toContain('getServerSideProps');
    expect(page).toContain("destination: '/player'");
    // The WIRING half: the page must not have regrown a reader. Its old body
    // fetched /api/brief/latest for two kinds; those fetches now live on the
    // player, and a copy reappearing here means the retirement was reverted
    // halfway.
    expect(
      page.includes('/api/brief/latest'),
      'pages/brief.tsx fetched the outbound spool again — that read moved to ' +
        '/player when the page was retired. Two surfaces fetching it is how the ' +
        'retirement half-lands.',
    ).toBe(false);
    expect(page.includes('BriefView')).toBe(false);
  });

  it('nothing routes to it any more', () => {
    // The two producers that pointed here, both retargeted deliberately.
    expect(SURFACE_PATHS.brief).toBe('/player');
    expect(read('pages', 'index.tsx').includes('href="/brief"')).toBe(false);
    expect(read('pages', 'player.tsx').includes('href="/brief"')).toBe(false);
  });

  it('the brief SURFACE NAME survives — only the route moved', () => {
    // `brief` is wire vocabulary, parity-pinned against Python's SURFACES.
    // Deleting the key would break that pin; this asserts the retirement did
    // not overreach from the ROUTE into the VOCABULARY.
    expect(Object.keys(SURFACE_PATHS)).toContain('brief');
  });
});

describe('what the page carried moved with it', () => {
  it('the player renders both artifacts the retired page rendered', () => {
    // The whole reason this retirement was not a paint change: /brief was the
    // ONLY surface fetching `kind=daily_sync`, and the only full-text render of
    // either artifact. Home fetches the brief but reads only its `date`.
    const player = read('pages', 'player.tsx');
    expect(player).toContain('daily_sync');
    expect(player).toContain('testId="brief-view"');
    expect(player).toContain('testId="daily-sync-view"');
  });

  it('home still does NOT render the markdown — the reason the move was needed', () => {
    // Pinning the premise itself. If home ever does render the brief text, the
    // player's "read it below" copy and this whole arrangement deserve a
    // rethink — and this assertion is where that conversation starts.
    const home = read('pages', 'index.tsx');
    expect(home).toContain('/api/brief/latest');
    expect(home.includes('BriefView')).toBe(false);
  });
});

describe('the slide fallback is a decision, not a leftover', () => {
  // THE CONDITION THIS PIN WAS REGISTERED AGAINST HAS CHANGED, and the flip is
  // deliberate rather than an appeasement. The old premise was that the player
  // carries the brief's full text always-on, which made "just below" the honest
  // destination for an unmapped section. The operator retired that always-on
  // render on 2026-08-15 ("if we're just retiring the old written brief and
  // keeping the player then no need to fix it before removing it"), so the text
  // now renders ONLY when the player cannot play.
  //
  // That inverts the fallback's truth condition rather than merely moving it: a
  // slide link exists only while slides exist, and slides exist only while the
  // narration works — precisely when the brief text is NOT rendered. The anchor
  // would therefore have pointed every unmapped section of a WORKING briefing at
  // a region holding only the Daily Sync. So the fallback is gone and unmapped
  // sections have no destination at all.
  it('an unmapped section has NO destination', () => {
    expect(slideDeepLink('mystery')).toBeNull();
    expect(slideDeepLink('health')).toBeNull();
    expect(slideDeepLink('weather')).toBeNull();
  });

  it('the anchor still exists for the links that DO fire', () => {
    // The id survives the fallback's removal because the two "Full text below ↓"
    // affordances still point at it — and they render only in the states where
    // the document renders, which is what keeps them true.
    const player = read('pages', 'player.tsx');
    expect(player).toContain('id="brief-text"');
    expect(player).toContain('href="#brief-text"');
  });

  it('the mapped sections still have theirs', () => {
    // Vacuity control: a map that returned null for everything would satisfy the
    // assertion above while having deleted the feature.
    expect(slideDeepLink('day_state')).toBe('/feed');
    expect(slideDeepLink('day_plan')).toBe('/deck');
  });

  it('never returns the retired route', () => {
    for (const id of ['day_state', 'day_plan', 'health', 'weather', 'sign_off', '']) {
      expect(slideDeepLink(id)).not.toBe('/brief');
    }
  });
});
