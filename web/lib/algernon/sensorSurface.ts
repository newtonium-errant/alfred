/**
 * The Awareness feed's surface name — ONE spelling, shared by the page, the
 * timeline, and the tests.
 *
 * It is a string in three independent places otherwise: the `surface` prop the
 * page hands Layout, the `[data-surface='…']` scope in styles/sensorLog.css, and
 * the assertions that guard containment. The stylesheet cannot import a
 * constant, so that spelling is pinned against this one by test rather than by
 * hope (`sensorLogContainment.test.tsx`). The other two share this literal, so a
 * rename cannot half-land.
 *
 * Lives in its own module rather than in feedView.ts because the tests import it
 * without wanting the view machinery, and rather than in consoleTokens.ts
 * because that is the shared layer's file and this name is the feed's own — the
 * open-seam contract is precisely that an adopting surface needs no entry there.
 */
export const SENSOR_SURFACE = 'sensor-log';
