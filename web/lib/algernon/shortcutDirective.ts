// Leading-directive parser for voice-dictated shortcut captures — DETERMINISTIC,
// no LLM. A dictated note that OPENS with a routing directive ("message for
// Hypatia, …", "for Hypatia: …", "Hypatia: …") is re-homed to that instance's
// ingest target and the directive prefix is stripped from the body. Anything
// that doesn't open with a directive to a KNOWN name is NOT a directive: the
// text is preserved fully intact and routed to the default (fail-safe — a
// misheard name must never lose words; Salem-as-keeper + the re-homing loop
// catch strays later).
//
// Per-user vocabulary (STT mangles "Hypatia" → "high patia" / "hi patia", etc.)
// is CONFIG-layer via SHORTCUT_DIRECTIVE_ALIASES (JSON), with built-in defaults.

// Canonical instance → its spoken aliases. The canonical name is always a
// self-alias (added by loadDirectiveAliases). Keys lower-cased; matched
// case-insensitively against the configured ingest targets by the route.
export const DEFAULT_DIRECTIVE_ALIASES: Record<string, string[]> = {
  hypatia: ['hypatia', 'high patia', 'hi patia'],
  'kal-le': ['kal-le', 'kalle', 'callie', 'cal e'],
  vera: ['vera'],
  salem: ['salem'],
};

function withSelfAliases(map: Record<string, string[]>): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const [canonical, aliases] of Object.entries(map)) {
    const set = new Set(aliases.map((a) => a.trim()).filter(Boolean));
    set.add(canonical); // the canonical name is always sayable
    out[canonical] = [...set];
  }
  return out;
}

/**
 * Load the directive alias map from SHORTCUT_DIRECTIVE_ALIASES (JSON object of
 * canonical → string[]), falling back to the built-in defaults. GARBAGE-SAFE:
 * malformed JSON, a non-object, or an all-invalid map degrades to defaults and
 * NEVER throws (this runs on an outward auth'd route).
 */
export function loadDirectiveAliases(): Record<string, string[]> {
  const raw = process.env.SHORTCUT_DIRECTIVE_ALIASES;
  if (!raw || !raw.trim()) return withSelfAliases(DEFAULT_DIRECTIVE_ALIASES);
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return withSelfAliases(DEFAULT_DIRECTIVE_ALIASES);
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return withSelfAliases(DEFAULT_DIRECTIVE_ALIASES);
  }
  const out: Record<string, string[]> = {};
  for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof k !== 'string' || !k.trim() || !Array.isArray(v)) continue;
    const aliases = v
      .filter((x): x is string => typeof x === 'string' && x.trim().length > 0)
      .map((s) => s.trim());
    if (aliases.length) out[k.trim().toLowerCase()] = aliases;
  }
  return Object.keys(out).length ? withSelfAliases(out) : withSelfAliases(DEFAULT_DIRECTIVE_ALIASES);
}

// Escape regex metacharacters, then render a multi-word alias with flexible
// internal whitespace (STT spacing varies): "high patia" → /high\s+patia/.
function aliasToPattern(alias: string): string {
  return alias
    .trim()
    .split(/\s+/)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('\\s+');
}

export interface DirectiveMatch {
  /** The canonical instance name the alias resolved to (e.g. "hypatia"). */
  canonical: string;
  /** The matched directive prefix, trimmed (provenance only, e.g. "message for hypatia,"). */
  spokenForm: string;
  /** The capture body with the directive prefix stripped (verbatim otherwise). */
  rest: string;
}

/**
 * If ``text`` OPENS with a directive naming a KNOWN alias (and has a non-empty
 * remainder), return the resolved canonical + the stripped body; else ``null``
 * (→ not a directive → the caller keeps the text intact + default target).
 *
 * Recognising the alias here is independent of whether that instance is a
 * CONFIGURED ingest target — the route makes that second check and, on a
 * recognised-but-unconfigured name, ALSO keeps the text intact (+ logs), so the
 * spoken intent survives into the fallback inbox for the re-homing loop.
 *
 * Accepted forms (case-insensitive, whitespace-tolerant — the regex encodes the
 * lowercase/collapse-space normalization):
 *   "message for <alias>[,:.]? …" · "for <alias>: …" · "<alias>: …"
 * Forms 2 and 3 REQUIRE the colon (so "for the record …" / a bare word isn't a
 * false directive). Input is length-bounded by the schema, so the anchored
 * patterns are safe from pathological backtracking.
 */
export function matchLeadingDirective(
  text: string,
  aliasMap: Record<string, string[]>,
): DirectiveMatch | null {
  if (!text || !text.trim()) return null;

  // (canonical, alias) pairs, LONGEST alias first so a longer alias can't be
  // shadowed by a shorter one that is its prefix.
  const pairs: Array<{ canonical: string; alias: string }> = [];
  for (const [canonical, aliases] of Object.entries(aliasMap)) {
    for (const alias of aliases) pairs.push({ canonical, alias });
  }
  pairs.sort((a, b) => b.alias.length - a.alias.length);

  for (const { canonical, alias } of pairs) {
    const a = aliasToPattern(alias);
    // The remainder must OPEN with a non-whitespace char (`\S.*`, dotall) — a
    // directive with an empty / all-whitespace body is not a match (never strip
    // to nothing; `.+` alone would capture a lone trailing space).
    const forms = [
      new RegExp(`^\\s*message\\s+for\\s+${a}\\b\\s*[,:.]?\\s+(\\S.*)$`, 'is'),
      new RegExp(`^\\s*for\\s+${a}\\b\\s*:\\s+(\\S.*)$`, 'is'),
      new RegExp(`^\\s*${a}\\b\\s*:\\s+(\\S.*)$`, 'is'),
    ];
    for (const re of forms) {
      const m = re.exec(text);
      if (m) {
        const rest = m[1];
        const spokenForm = text.slice(0, text.length - rest.length).trim();
        return { canonical, spokenForm, rest };
      }
    }
  }
  return null;
}
