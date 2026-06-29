// Global test setup (vitest setupFiles).
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

// Testing Library's automatic cleanup only self-registers when a global
// `afterEach` exists (i.e. vitest `globals: true`). We run with globals off +
// explicit imports, so register cleanup ourselves — without it, renders
// accumulate in document.body across tests and getByTestId finds duplicates.
afterEach(() => {
  cleanup();
});

// jsdom doesn't implement scrollIntoView, which ChatThread calls in an effect —
// stub it so component renders don't throw. Plain DOM assertions are used in the
// tests (no jest-dom), so no matcher registration is needed.
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
