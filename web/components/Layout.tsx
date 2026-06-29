import { ReactNode, useState } from 'react';
import Link from 'next/link';
import { cn } from '../lib/utils';

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
  /** Max content width; defaults to a comfortable reading column. */
  maxWidthClassName?: string;
};

// The app's surfaces. M1 ships Chat only; Routines lands in M3 (the no-shame
// display). Keeping this an array preserves honeydew's nav structure so M3 adds
// a link without re-architecting the header.
const NAV_LINKS = [{ href: '/', label: 'Chat' }] as const;

// Sticky melon header + centered content container with the warm page wash.
// Borrowed from honeydew's Layout (the render/interaction layer); its Supabase
// sign-out + household nav + ReportIssueFab are dropped. The mobile hamburger
// structure is retained but only surfaces once there's something to overflow
// (a second nav link in M3, or the sign-out control in FE-3).
export function Layout({
  children,
  showNav = true,
  onSignOut,
  maxWidthClassName = 'max-w-2xl',
}: LayoutProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  // A single nav item with no sign-out needs no mobile overflow menu.
  const showHamburger = NAV_LINKS.length > 1 || onSignOut != null;

  return (
    <div className="min-h-screen bg-honeydew-50">
      <header className="sticky top-0 z-10 border-b border-honeydew-200 bg-honeydew-50/90 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-2 px-4 py-3 sm:gap-4 sm:px-5">
          <Link
            href="/"
            className="flex min-w-0 shrink items-center gap-2 truncate text-lg font-extrabold text-honeydew-700"
          >
            <span aria-hidden="true">✦</span>
            {INSTANCE_NAME}
          </Link>

          {showNav && (
            <>
              {/* Tablet / desktop: inline nav. */}
              <nav className="hidden shrink-0 items-center gap-1 sm:flex sm:gap-2">
                {NAV_LINKS.map((l) => (
                  <Link
                    key={l.href}
                    href={l.href}
                    data-testid={`nav-${l.label.toLowerCase()}`}
                    className="whitespace-nowrap rounded-xl px-3 py-1.5 text-sm font-semibold text-honeydew-700 hover:bg-honeydew-100"
                  >
                    {l.label}
                  </Link>
                ))}
                {onSignOut && (
                  <button
                    type="button"
                    onClick={onSignOut}
                    data-testid="nav-signout"
                    className="whitespace-nowrap rounded-xl border border-honeydew-300 bg-white px-3 py-1.5 text-sm font-semibold text-honeydew-700 hover:bg-honeydew-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-2"
                  >
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
                  className="shrink-0 rounded-xl border border-honeydew-300 bg-white px-2.5 py-1.5 text-lg leading-none text-honeydew-700 hover:bg-honeydew-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-honeydew-600 focus-visible:ring-offset-2 sm:hidden"
                >
                  <span aria-hidden="true">{menuOpen ? '✕' : '☰'}</span>
                </button>
              )}
            </>
          )}
        </div>

        {/* Mobile dropdown panel. */}
        {showNav && showHamburger && menuOpen && (
          <nav
            data-testid="nav-mobile-panel"
            className="border-t border-honeydew-200 bg-honeydew-50/95 backdrop-blur sm:hidden"
          >
            <div className="mx-auto flex max-w-5xl flex-col gap-1 px-4 py-2">
              {NAV_LINKS.map((l) => (
                <Link
                  key={l.href}
                  href={l.href}
                  data-testid={`nav-m-${l.label.toLowerCase()}`}
                  className="rounded-xl px-3 py-2 text-sm font-semibold text-honeydew-700 hover:bg-honeydew-100"
                >
                  {l.label}
                </Link>
              ))}
              {onSignOut && (
                <button
                  type="button"
                  onClick={onSignOut}
                  data-testid="nav-m-signout"
                  className="rounded-xl border border-honeydew-300 bg-white px-3 py-2 text-left text-sm font-semibold text-honeydew-700 hover:bg-honeydew-50"
                >
                  Sign out
                </button>
              )}
            </div>
          </nav>
        )}
      </header>
      <main className={cn('mx-auto px-5 py-8', maxWidthClassName)}>{children}</main>
    </div>
  );
}
