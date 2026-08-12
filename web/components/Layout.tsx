import { ReactNode, useState } from 'react';
import Link from 'next/link';
import { cn } from '../lib/utils';
import {
  CONSOLE_SURFACE,
  WARM_SURFACE,
  type KnownSurface,
  type SurfaceIdentity,
} from '../lib/algernon/consoleTokens';
import { ReportBugFab } from './ReportBugFab';

// One frontend deployment targets ONE instance (blueprint §5). The instance's
// display name is config-driven, NOT hardcoded — per the codebase's
// no-hardcoded-instance-name discipline. Defaults to the platform name.
const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

type LayoutProps = {
  children: ReactNode;
  /** Show the surface nav (authed pages). */
  showNav?: boolean;
  /**
   * Sign-out handler. Wired in FE-3 (auth) — when omitted, no sign-out control
   * renders, so FE-1 has no dead button. Kept prop-driven so the shell stays
   * Supabase-free (honeydew's Layout called `supabase.auth.signOut()` inline).
   */
  onSignOut?: () => void;
  /**
   * Unread-notification count for the nav badge (parity #22). 0 / omitted
   * renders no badge — the badge is a signal, not a permanent zero counter.
   */
  unreadCount?: number;
  /** Max content width; defaults to a comfortable reading column. */
  maxWidthClassName?: string;
  /**
   * Show the floating "Report a bug" button (#95). Defaults to `showNav` — the
   * authed surfaces are exactly where a bug report makes sense, and the login
   * screen is not somewhere to offer one (there is no verified reporter yet,
   * and the BFF would refuse it with `invalid_session` anyway).
   *
   * Pass `false` explicitly for a surface that must not carry it.
   */
  showBugReport?: boolean;
  /**
   * The instance whose content this surface is currently showing (#99) — passed
   * by the pages that have an instance switcher, so a bug report filed while
   * reading another instance RECORDS that instance instead of the build-time
   * home name. Omitted elsewhere: a surface with no instance concept of its own
   * is the home app, and the FAB falls back to the home name.
   *
   * Metadata only. Delivery of a report is home-only either way.
   */
  viewedInstance?: string;
  /**
   * WHICH SURFACE THIS IS. Emitted VERBATIM as `data-surface="<name>"` on the
   * root element, and that attribute — not this prop — is the containment
   * mechanism the whole arc keys off: a surface's stylesheet scopes itself
   * under `[data-surface="…"]` so its skin structurally cannot reach another
   * surface. The prop is just the ergonomic spelling of the attribute.
   *
   * DELIBERATELY OPEN. Any name is legal, and no value is privileged in the
   * emission path — `console` goes through exactly the same line as a name
   * this file has never heard of. That is what lets a surface own its skin
   * entirely in its own stylesheet (the Awareness feed's `sensor-log` is the
   * next one) and adopt this prop without waiting on a change here.
   *
   * A name with no entry in `SURFACE` below gets the `warm` chrome — today's
   * default appearance — plus its own attribute.
   *
   * WHAT THAT DOES NOT PROMISE is that adopting the prop changes nothing to
   * look at. Moving the attribute to the ROOT widens its coverage to the shell,
   * so any of the adopting surface's own `[data-surface="…"]`-scoped selectors
   * now reach the nav and the header too. Whether that is visible is a fact
   * about the adopter's stylesheet, not about this component: the Awareness
   * feed's 2026-08-12 selector audit found 3 of its 28 would repaint the shell
   * (its bare root rule, its `:focus-visible` retarget, and its danger-class
   * descendants). So an adopting surface owes itself a pin that its scoped
   * selectors stay off the shell — keeping property definitions on the root
   * while painting declarations move to a content-scoped element. This side
   * promises only the chrome fallback and the verbatim attribute.
   *
   * (An earlier version of this note called adoption "visually a no-op". It
   * stated the widening in the same breath and then concluded the opposite;
   * the audit refuted it. Retired in place rather than softened, because the
   * one person who reads this docstring is the one about to adopt the seam.)
   *
   * `warm` is the honeydew light theme every existing surface has always worn.
   * `console` is the ratified Phase B identity (LCARS grammar, modern dark
   * palette), worn today by the Decide deck.
   *
   * Deliberately ONE structure with N skins rather than N components. The nav,
   * the mobile overflow, the unread badge and the sign-out control are the same
   * affordances with the same test ids on every surface — forking the tree
   * would mean maintaining a copy of each per identity, and the second copy is
   * always the one that drifts.
   */
  surface?: SurfaceIdentity;
};

type SurfaceClasses = Record<string, string>;

/**
 * The chrome table for surfaces this component dresses itself. A surface that
 * skins itself entirely from its own stylesheet needs no entry here — see
 * `surfaceClasses` for what an unregistered name gets.
 */
const SURFACE: Record<KnownSurface, SurfaceClasses> = {
  warm: {
    root: 'min-h-screen bg-honeydew-50',
    header: 'sticky top-0 z-10 border-b border-honeydew-200 bg-honeydew-50/90 backdrop-blur',
    headerInner: 'mx-auto flex max-w-5xl items-center justify-between gap-2 px-4 py-3 sm:gap-4 sm:px-5',
    logo: 'flex min-w-0 shrink items-center gap-2 truncate text-lg font-extrabold text-honeydew-700',
    badge: 'rounded-full bg-honeydew-600 px-2 py-0.5 text-xs font-bold text-white',
    navLink: 'whitespace-nowrap rounded-xl px-3 py-1.5 text-sm font-semibold text-honeydew-700 hover:bg-honeydew-100',
    signOut:
      'whitespace-nowrap rounded-xl border border-honeydew-300 bg-white px-3 py-1.5 text-sm font-semibold text-honeydew-700 hover:bg-honeydew-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-2',
    burger:
      'shrink-0 rounded-xl border border-honeydew-300 bg-white px-2.5 py-1.5 text-lg leading-none text-honeydew-700 hover:bg-honeydew-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-2 sm:hidden',
    mobilePanel: 'border-t border-honeydew-200 bg-honeydew-50/95 backdrop-blur sm:hidden',
    mobileLink: 'rounded-xl px-3 py-2 text-sm font-semibold text-honeydew-700 hover:bg-honeydew-100',
    mobileSignOut:
      'rounded-xl border border-honeydew-300 bg-white px-3 py-2 text-left text-sm font-semibold text-honeydew-700 hover:bg-honeydew-50',
    main: 'mx-auto px-5 py-8',
  },
  console: {
    root: 'min-h-screen bg-console-hull',
    // No backdrop-blur and no translucency: the console reads as panels bolted
    // to a hull, and a frosted header would be the chrome the grammar refuses.
    header: 'sticky top-0 z-10 bg-console-void',
    headerInner: 'mx-auto flex max-w-5xl items-stretch gap-[3px] px-2 py-2 sm:px-3',
    logo:
      'console-railseg flex min-w-0 shrink items-center gap-2 truncate bg-console-panel px-4 text-sm font-extrabold uppercase tracking-[0.22em] text-affirm',
    badge: 'rounded-full bg-caution px-2 py-0.5 text-xs font-bold text-console-on-fill',
    navLink:
      'console-railseg flex items-center whitespace-nowrap bg-console-raise px-3.5 text-[11px] font-bold uppercase tracking-[0.14em] text-console-ink-dim hover:bg-affirm-wash hover:text-affirm',
    signOut:
      'console-railseg flex items-center whitespace-nowrap bg-console-raise px-3.5 text-[11px] font-bold uppercase tracking-[0.14em] text-console-ink-faint hover:text-negative',
    burger:
      'console-railseg shrink-0 bg-console-raise px-3 text-lg leading-none text-console-ink-dim sm:hidden',
    mobilePanel: 'bg-console-void px-2 pb-2 sm:hidden',
    mobileLink:
      'console-railseg bg-console-raise px-3.5 py-2.5 text-[11px] font-bold uppercase tracking-[0.14em] text-console-ink-dim',
    mobileSignOut:
      'console-railseg bg-console-raise px-3.5 py-2.5 text-left text-[11px] font-bold uppercase tracking-[0.14em] text-console-ink-faint',
    main: 'mx-auto px-3 py-4 sm:px-4',
  },
};

/**
 * The chrome for a surface name — falling back to `warm` for any name this
 * table has no entry for.
 *
 * The fallback is the load-bearing half of the open-name contract. A surface
 * that skins itself from its own stylesheet (scoped under its
 * `data-surface` attribute) gets today's default chrome plus its attribute,
 * so adopting the prop changes nothing visually and cannot crash on a name
 * nobody registered. Throwing, or returning undefined and letting every
 * `s.root` read blow up, would make the open contract a trap instead of a
 * seam.
 */
function surfaceClasses(name: SurfaceIdentity): SurfaceClasses {
  return (SURFACE as Record<string, SurfaceClasses | undefined>)[name] ?? SURFACE[WARM_SURFACE];
}

// The app's surfaces. The logo (✦) links to `/`, the home COMPOSER (B3-3); Chat
// moved to /chat when the composer took the landing. Keeping this an array
// preserves honeydew's nav structure so a new surface adds a link without
// re-architecting the header.
const NAV_LINKS = [
  { href: '/chat', label: 'Chat' },
  { href: '/brief', label: 'Brief' },
  { href: '/deck', label: 'Deck' },
  { href: '/feed', label: 'Feed' },
  { href: '/ingest', label: 'Ingest' },
  { href: '/batch', label: 'Scans' },
] as const;

// Sticky melon header + centered content container with the warm page wash.
// Borrowed from honeydew's Layout (the render/interaction layer); its Supabase
// sign-out + household nav + ReportIssueFab are dropped. The mobile hamburger
// structure is retained but only surfaces once there's something to overflow
// (a second nav link in M3, or the sign-out control in FE-3).
export function Layout({
  children,
  showNav = true,
  onSignOut,
  unreadCount = 0,
  maxWidthClassName = 'max-w-2xl',
  showBugReport,
  viewedInstance,
  surface = WARM_SURFACE,
}: LayoutProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  // A single nav item with no sign-out needs no mobile overflow menu.
  const showHamburger = NAV_LINKS.length > 1 || onSignOut != null;
  const s = surfaceClasses(surface);
  // Drives the console's two structural extras (the elbow, and the rail's
  // fill spacer). Skin only — the `data-surface` emission below is
  // deliberately free of any per-name branch.
  const isConsole = surface === CONSOLE_SURFACE;

  return (
    <div className={s.root} data-surface={surface}>
      <header className={s.header}>
        <div className={s.headerInner}>
          {isConsole && (
            // The LCARS elbow — the corner block that turns a vertical rail
            // into a horizontal header. Purely structural, so it is hidden from
            // assistive tech: it names nothing and does nothing.
            <span
              aria-hidden="true"
              data-testid="console-elbow"
              className="console-elbow w-11 shrink-0 bg-affirm-deep sm:w-16"
            />
          )}
          <Link href="/" className={s.logo}>
            <span aria-hidden="true">✦</span>
            {INSTANCE_NAME}
            {unreadCount > 0 && (
              // Unread-notification badge (parity #22). Calm pill — never
              // danger-red (reserved for true system errors); on the console it
              // is caution, which is that identity's "needs a beat" role, and
              // still never the negative one. Hidden at zero so idle isn't a
              // permanent "0" counter.
              <span
                data-testid="nav-unread-badge"
                aria-label={`${unreadCount} unread notifications`}
                className={s.badge}
              >
                {unreadCount}
              </span>
            )}
          </Link>

          {/* The console header is a rail of blocks that must fill its width;
              the warm header is a spread. One spacer expresses the difference. */}
          {isConsole && <span aria-hidden="true" className="flex-1" />}

          {showNav && (
            <>
              {/* Tablet / desktop: inline nav. */}
              <nav
                className={
                  isConsole
                    ? 'hidden shrink-0 items-stretch gap-[3px] sm:flex'
                    : 'hidden shrink-0 items-center gap-1 sm:flex sm:gap-2'
                }
              >
                {NAV_LINKS.map((l) => (
                  <Link
                    key={l.href}
                    href={l.href}
                    data-testid={`nav-${l.label.toLowerCase()}`}
                    className={s.navLink}
                  >
                    {l.label}
                  </Link>
                ))}
                {onSignOut && (
                  <button type="button" onClick={onSignOut} data-testid="nav-signout" className={s.signOut}>
                    Sign out
                  </button>
                )}
              </nav>

              {/* Mobile: hamburger toggles the dropdown panel below. */}
              {showHamburger && (
                <button
                  type="button"
                  data-testid="nav-menu-button"
                  aria-label="Menu"
                  aria-expanded={menuOpen}
                  onClick={() => setMenuOpen((o) => !o)}
                  className={s.burger}
                >
                  <span aria-hidden="true">{menuOpen ? '✕' : '☰'}</span>
                </button>
              )}
            </>
          )}
        </div>

        {/* Mobile dropdown panel. */}
        {showNav && showHamburger && menuOpen && (
          <nav data-testid="nav-mobile-panel" className={s.mobilePanel}>
            <div
              className={cn(
                'mx-auto flex max-w-5xl flex-col',
                isConsole ? 'gap-[3px]' : 'gap-1 px-4 py-2',
              )}
            >
              {NAV_LINKS.map((l) => (
                <Link
                  key={l.href}
                  href={l.href}
                  data-testid={`nav-m-${l.label.toLowerCase()}`}
                  className={s.mobileLink}
                >
                  {l.label}
                </Link>
              ))}
              {onSignOut && (
                <button type="button" onClick={onSignOut} data-testid="nav-m-signout" className={s.mobileSignOut}>
                  Sign out
                </button>
              )}
            </div>
          </nav>
        )}
      </header>
      <main className={cn(s.main, maxWidthClassName)}>{children}</main>
      {(showBugReport ?? showNav) && <ReportBugFab viewedInstance={viewedInstance} />}
    </div>
  );
}
