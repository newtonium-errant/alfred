/**
 * Match a class / selector token EXACTLY, never as a prefix of its siblings.
 *
 * ─────────────────────────────────────────────────────────────────────────────
 * THE COMMENT THAT EXISTS TO STOP THE FIFTH APPEARANCE.
 *
 * One weakness wore four costumes in a single week, and every one of them was
 * written by someone who knew about the previous one:
 *
 *   1. `sourceText.includes(cls)` in sensorLogSkin's orphan guard — `.ui-code`
 *      satisfied by the text inside `ui-code-label`, so a live rule could go
 *      orphaned and the guard would call it covered.
 *   2. `\.ui-code\b` in a bare-rule check. `\b` is a boundary between a word
 *      character and a NON-word character, and `-` is non-word — so `\b`
 *      matches happily in the middle of `ui-code-label`. The regex looks
 *      strict and is not.
 *   3. A reviewer's rename mutation (`.ui-code` → `.ui-code-GONE`) scored a
 *      LOWER red count than a deletion, because the renamed selector still
 *      contained the original token. The weakness distorting the instrument
 *      measuring it.
 *   4. The block extraction inside the FIX for (2) — written ninety seconds
 *      after (2)'s explanation, by the same hand, in the next function.
 *
 * WHY IT KEEPS HAPPENING: `includes()` reads as membership at a glance ("is
 * this class in this rule?") and the type system agrees it is a legal question
 * to ask, so nothing objects at the moment of writing. And `\b` is what a
 * careful person reaches for when they already know `includes()` is wrong,
 * which is why costume (2) shows up immediately after costume (1). Neither is
 * visible by reading a diff — all four instances passed inspection and were
 * only ever caught by RUNNING a sibling-shadow mutation.
 *
 * WHY THERE IS NO LEADING LOOKBEHIND. A `(?<![\w-])` prefix looks symmetric and
 * is wrong for CSS: `.foo.ui-code` is a legitimate compound selector, and a
 * lookbehind would see `o` before the dot and refuse to match — turning a
 * strictness fix into a false negative. The leading edge is already supplied by
 * the token's own punctuation (`.` or `:`) for selectors, and by whitespace for
 * class-attribute text. The trailing edge is the one that leaks.
 * ─────────────────────────────────────────────────────────────────────────────
 *
 * Pass the token as it appears in the text being searched — `.ui-code` for a
 * selector, `ui-code` for a `className` string, `:focus-visible` for a pseudo.
 */
export function exactToken(token: string): RegExp {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`${escaped}(?![\\w-])`);
}

/** `exactToken` as a predicate, for the common `.filter(...)` / `.some(...)` shape. */
export function hasExactToken(haystack: string, token: string): boolean {
  return exactToken(token).test(haystack);
}
