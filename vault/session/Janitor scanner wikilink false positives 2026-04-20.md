---
type: session
created: '2026-04-20'
name: Janitor scanner wikilink false positives 2026-04-20
description: Fix two janitor scanner false-positive classes inflating LINK001 and ORPHAN001 counts
intent: Drop LINK001 from ~1820 to a realistic count by normalizing YAML-wrapped wikilink captures and locking _templates/ out of default ignore_dirs
participants:
  - '[[person/Andrew Newton]]'
project: '[[project/Alfred]]'
related:
  - '[[constraint/YAML Line-Wrapped Wikilinks Cause Janitor Link Scanner False Positives]]'
tags:
  - janitor
  - scanner
  - false-positives
  - link001
status: completed
---

# Janitor scanner wikilink false positives 2026-04-20

## Intent

Morning brief on 2026-04-20 flagged 2224 open issues (1820 LINK001) — up
from a ~1500–1650 baseline. Investigation ruled out genuine vault drift:
the overnight distiller consolidation created ~50+ new records with long
descriptive names, which exposed a latent regex false positive in the
wikilink scanner. Secondary class: placeholder wikilinks in
`_templates/` showing up whenever the ignore_dirs list is missing that
entry.

## Work Completed

### Fix 1 — Normalize embedded whitespace in wikilink captures

`src/alfred/janitor/parser.py::extract_wikilinks` now collapses any
internal whitespace run in a captured target down to a single space
before returning. The existing regex `\[\[([^\]|]+)(?:\|[^\]]+)?\]\]`
uses a character class that includes newlines, so it was capturing
YAML-folded targets like:

```
- '[[assumption/Law Firm Billing Accepted on Summary Confirmation Without Line-Item
    Review]]'
```

as the literal string `assumption/...Line-Item\n    Review`. The stem
index is built from filenames (no embedded newlines), so lookup always
failed → spurious LINK001.

The fix is surgical: one list-comprehension in `extract_wikilinks`. All
callers — scanner `_build_inbound_index`, `_check_record`, LINK002
checks, and `pipeline.py` inbound/outbound index builders — transparently
get the normalized form. Wikilinks never carry meaningful internal
whitespace (Obsidian renders them as one token), so the normalization
is semantically safe.

### Fix 2 — Default `ignore_dirs` includes `_templates` and `_bases`

`src/alfred/janitor/config.py::VaultConfig.ignore_dirs` default now
includes `_templates` and `_bases` alongside `.obsidian`. The user's
`config.yaml` and `config.yaml.example` already listed these, so this
is defense-in-depth for fresh installs without a config.yaml override.
No user-facing config change required.

### Tests — 10 new, all green

Added to `tests/test_janitor_scanner.py`:

- `TestExtractWikilinksNormalizesWhitespace` (4 tests) — pins the
  single-line output of `extract_wikilinks` for wrapped, unwrapped,
  bare-stem, and mixed inputs.
- `TestScannerYamlWrappedWikilinks` (2 tests) — end-to-end: wrapped
  wikilink to an existing target does NOT produce LINK001; wrapped
  wikilink to a missing target DOES, and the message is single-line.
- `TestScannerRegressionRegularWikilinks` (2 tests) — unwrapped valid
  link is silent; unwrapped broken link still flags.
- `TestScannerSkipsTemplatesDir` (2 tests) — a real-shaped template
  with placeholder wikilinks produces zero issues; default
  `VaultConfig().ignore_dirs` pins `_templates`, `_bases`, `.obsidian`.

Baseline 513 → 523 tests. Full suite green.

## Scanner Run — Before / After

Run against the real vault (`/home/andrew/alfred/vault`):

| Code      | Before | After | Delta |
|-----------|-------:|------:|------:|
| LINK001   |  1821  |  156  | -1665 (-91%) |
| ORPHAN001 |   386  |  323  |   -63 |
| LINK002   |    56  |   11  |   -45 |
| STUB001   |     8  |    8  |     0 |
| FM001     |     4  |    4  |     0 |
| DUP001    |     2  |    2  |     0 |
| DIR001    |     1  |    1  |     0 |
| **Total** | 2278   |  505  | -1773 |

ORPHAN001 and LINK002 both dropped because the wrap false positives
were blocking inbound-link resolution — once the regex captures the
right target, the inbound index finds the reference and those records
stop looking orphaned.

## New Patterns Surfaced

The 156 remaining LINK001 entries include legitimate broken links that
were hidden under the false-positive noise:

- `[[_docs/alfred-instructions]]`, `[[_docs/architecture]]`, etc. —
  references to a `_docs/` tree that doesn't exist in the vault
  (possibly a legacy scaffold reference).
- `[[Start Here]]`, `[[note/Pocketpills Prescription Refill Reminder 2026-04-15]]`
  — renamed or missing records.
- `[[org/Rural Route Transportation]]` — likely canonical name drift.
- `[[...]]` — literal ellipsis wikilink somewhere (prompt template
  leakage?). Worth investigating.
- Several file-suffix `.md` wikilinks (e.g. `[[note/Something.md]]`) —
  scanner expects stems without `.md`; agent or a template is emitting
  the extension. May be a separate normalization opportunity.

None of these were actionable while buried under 1665 wrap-spurious
entries. Now they're visible for real remediation (separate task).

## Restart Recommendation

The janitor daemon is a long-running process and holds the old scanner
code in memory. To pick up these fixes in production, restart the
janitor:

```bash
alfred down
alfred up
```

Until restart, the running daemon will keep emitting false positives on
its sweep interval. The next sweep after restart should show the
post-fix counts.

## Alfred Learnings

### New gotcha — Python regex character classes match newlines by default

The wikilink regex `[^\]|]+` happily consumed newlines because
`[^...]` excludes only the listed chars, not newlines. Combined with
PyYAML's default line folding for quoted strings, this silently
corrupts every captured target whose encoded list entry exceeds 80
columns. Any regex-based extractor operating on raw YAML-serialized
frontmatter needs to either (a) normalize whitespace on capture, (b)
use `re.MULTILINE`/`re.DOTALL` explicitly and unwrap YAML continuation
lines first, or (c) parse frontmatter and walk the data structure. We
picked (a) because it's one line and fixes every existing call site
transparently.

### Pattern validated — dataclass defaults as the floor, not the ceiling

User's `config.yaml` already had `_templates` in `ignore_dirs`, but
the `VaultConfig` default only had `.obsidian`. Fresh installs (or
config overrides that reset the list) would miss scaffold templates.
Making defaults match known-good production values is zero-cost
defense.

### Missing knowledge — scale-triggered bugs

This bug pre-existed the overnight distiller batch. It only became
visible when >50 records with names long enough to trigger YAML
line-folding entered the vault simultaneously. Sort of gotcha worth
noting: **"deterministic checks are only deterministic at the scale
they were tested at."** If record counts or naming patterns change
dramatically overnight, trust that drift metrics will overshoot and
investigate the scanner before assuming real regression.

### Correction — constraint record was ahead of the code

`constraint/YAML Line-Wrapped Wikilinks Cause Janitor Link Scanner False
Positives.md` already documented this exact bug. The scanner-fix work
lagged the epistemic record. Closing the loop now; the constraint can
be marked resolved by referencing this session.
