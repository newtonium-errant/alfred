/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx}',
    './components/**/*.{js,ts,jsx,tsx}',
    // lib/ carries className constants too (typography.ts, ringGeometry.ts's
    // RING_STROKE_CLASS). Scan it so those literals generate their utilities
    // rather than relying on the same class happening to appear in a component.
    './lib/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        // Warm honeydew-melon scale, anchored on the existing palette.
        honeydew: {
          50: '#f6faf1', // panel / page wash
          100: '#eef6e4',
          200: '#e2ead9', // soft border
          300: '#cdd9c2', // border
          400: '#a9cf86', // disabled / muted fill
          500: '#7bbf4f', // primary fill
          600: '#5a8f3d', // mid / headings
          700: '#2f6b1f', // dark / strong text
          800: '#245417',
          900: '#1b3f11',
        },
        // Pill status accents (bg + text pairs used by StatusBadge / Badge cva).
        status: {
          todo: '#eef2f7',
          'todo-fg': '#475569',
          progress: '#fef3c7',
          'progress-fg': '#92611a',
          // 'blocked' is "On hold" — waiting on something, not the user failing.
          // De-red'd to the calm amber family (matches Due soon / Past target /
          // in-progress), per the no-shame finding #1 in docs/design/design-
          // language.md. The DB enum value stays 'blocked'; this is display only.
          blocked: '#fef3c7',
          'blocked-fg': '#92611a',
          done: '#dff0cf',
          'done-fg': '#2f6b1f',
        },
        danger: {
          DEFAULT: '#b3261e',
          bg: '#fde2e1',
        },
        cream: '#fdfdf8', // card surface

        // ── ALGERNON CONSOLE identity (interface-reimagine arc) ─────────────
        // The dark-first LCARS-grammar vocabulary. Values live as CSS custom
        // properties in styles/console.css (which carries the full contract:
        // what each role MEANS, and why `caution` covers both defer and heavy);
        // these are the Tailwind names surfaces actually type.
        //
        // ADDITIVE AND OPT-IN. None of the warm honeydew surfaces reference
        // these, so adding them restyles nothing — a surface joins the identity
        // by rendering inside Layout's `surface="console"` variant.
        //
        // NO OPACITY MODIFIERS: these resolve to plain hex through var(), not
        // the `<alpha-value>` channel form, so `bg-console-panel/50` silently
        // produces nothing. Layer by climbing the surface ramp instead
        // (hull → panel → raise), which is how depth stays legible on a dark
        // ground; `console-scrim` is the one deliberate translucent token.
        console: {
          void: 'var(--console-void)',
          hull: 'var(--console-hull)',
          panel: 'var(--console-panel)',
          raise: 'var(--console-raise)',
          edge: 'var(--console-edge)',
          'edge-bright': 'var(--console-edge-bright)',
          ink: 'var(--console-ink)',
          'ink-dim': 'var(--console-ink-dim)',
          'ink-faint': 'var(--console-ink-faint)',
          'ink-ghost': 'var(--console-ink-ghost)',
          'on-fill': 'var(--console-on-fill)',
          scrim: 'var(--console-scrim)',
        },
        // The four FUNCTION roles. Colour carries meaning here — spend them by
        // what the element DOES, never by what looks good in the slot.
        affirm: {
          DEFAULT: 'var(--console-affirm)',
          deep: 'var(--console-affirm-deep)',
          wash: 'var(--console-affirm-wash)',
        },
        negative: {
          DEFAULT: 'var(--console-negative)',
          deep: 'var(--console-negative-deep)',
          wash: 'var(--console-negative-wash)',
        },
        caution: {
          DEFAULT: 'var(--console-caution)',
          deep: 'var(--console-caution-deep)',
          wash: 'var(--console-caution-wash)',
        },
        info: {
          DEFAULT: 'var(--console-info)',
          deep: 'var(--console-info-deep)',
          wash: 'var(--console-info-wash)',
        },
        environment: {
          DEFAULT: 'var(--console-environment)',
          deep: 'var(--console-environment-deep)',
          wash: 'var(--console-environment-wash)',
        },
        // Instance provenance — identity, not judgement. Hues chosen to avoid
        // every function role, and assigned by a stable hash of the instance
        // name (lib/algernon/consoleTokens.ts), never by a name literal.
        accent: {
          1: 'var(--console-accent-1)',
          2: 'var(--console-accent-2)',
          3: 'var(--console-accent-3)',
          4: 'var(--console-accent-4)',
          '1-wash': 'var(--console-accent-1-wash)',
          '2-wash': 'var(--console-accent-2-wash)',
          '3-wash': 'var(--console-accent-3-wash)',
          '4-wash': 'var(--console-accent-4-wash)',
        },
      },
      fontFamily: {
        // Friendly rounded sans, injected via next/font in _app.tsx.
        sans: ['var(--font-honeydew)', 'ui-rounded', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        xl: '0.875rem',
        '2xl': '1.25rem',
        '3xl': '1.75rem',
      },
      boxShadow: {
        soft: '0 1px 2px rgba(47, 107, 31, 0.04), 0 4px 16px rgba(47, 107, 31, 0.06)',
        card: '0 1px 3px rgba(47, 107, 31, 0.05), 0 8px 24px rgba(47, 107, 31, 0.07)',
      },
      // Celebration motion (engagement series, shared-infra item 1). Always use
      // via the motion-safe: variant so prefers-reduced-motion users never see
      // them; Playwright runs with reducedMotion 'reduce' so they can't flake e2e.
      keyframes: {
        // One-shot pill pop on a confirmed completion (1 -> 1.12 -> 1).
        pop: {
          '0%': { transform: 'scale(1)' },
          '40%': { transform: 'scale(1.12)' },
          '100%': { transform: 'scale(1)' },
        },
        // Light edge shimmer sweeping the bar fill when it advances.
        barShimmer: {
          '0%': { transform: 'translateX(-100%)', opacity: '0.9' },
          '100%': { transform: 'translateX(100%)', opacity: '0' },
        },
        // One drifting celebration emoji (4a arrival banner): rises and fades.
        confettiDrift: {
          '0%': { transform: 'translateY(0) rotate(0deg)', opacity: '1' },
          '100%': { transform: 'translateY(-90px) rotate(24deg)', opacity: '0' },
        },
        // Gentle banner entrance; final keyframe IS the base state, so no
        // fill-mode is needed (unlike the parking animations below).
        riseIn: {
          '0%': { transform: 'translateY(8px)', opacity: '0' },
          '100%': { transform: 'translateY(0)', opacity: '1' },
        },
      },
      animation: {
        pop: 'pop 0.3s ease-out 1',
        // `forwards` is load-bearing: the final keyframe is translateX(100%) +
        // opacity 0, so the one-shot shimmer PARKS invisible. Without it the
        // span snaps back to base style (opacity 1) and the gradient would sit
        // permanently over the fill once the run ends.
        barShimmer: 'barShimmer 0.7s ease-out 1 forwards',
        // Same `forwards` contract as barShimmer (motion.spec.ts asserts it):
        // a finished drift must park at opacity 0, never snap back visible.
        confettiDrift: 'confettiDrift 1.5s ease-out 1 forwards',
        riseIn: 'riseIn 0.35s ease-out 1',
      },
    },
  },
  plugins: [],
};
