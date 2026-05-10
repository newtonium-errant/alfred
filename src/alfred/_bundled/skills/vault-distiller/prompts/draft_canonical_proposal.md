You are reading a cluster of related documents from a developer's knowledge
vault. The cluster has cohered semantically but has not yet been promoted to
a canonical named artifact (architecture/<theme>.md or principles/<rule>.md).

Cluster labels (from surveyor): {labels}
Document count: {count}

Documents:
{members_with_previews}

## Your job

DEFAULT TO DRAFTING. The cluster reached you because the surveyor's embedder
found semantic coherence between these documents — your job is to NAME the
shared theme. Most clusters have one. A few don't. Two rare cases (NO-CLAIM
and SPLIT) exist for the genuinely-incoherent and the genuinely-multi-theme,
but they are EXCEPTIONS, not the safe fallback.

If you can identify even a thin shared theme — a recurring pattern, a common
structural choice, a repeated practice — DRAFT IT. Refusal is reserved for
clusters where the members are genuinely unrelated except for one surface
keyword.

Decide ONE of three outcomes and emit the matching format. Read the cluster
members carefully before choosing.

### Outcome A — Shared theme (the default path)

Use this for any cluster where the members share a recurring pattern,
structural choice, or practice — even when the specifics vary across
members. The theme does NOT need to be identical across all members; it
needs to be a unifying principle that ties them together.

Example of a cluster that QUALIFIES for Outcome A:

> Cluster of 3 decisions: (a) "Route-namespace registry pattern for Alfred
> transport", (b) "Section-provider registry with priority slots", (c)
> "/health is the only unauthenticated transport route". The members differ
> in surface specifics, but the unifying theme is concrete: Alfred's
> transport layer uses a centralized registry pattern. DRAFT IT.

Write 2-4 sentences naming that theme. Constraints:

- Open with the SUBJECT of the theme, not with a meta-statement about the
  cluster. Do NOT begin with "The documents...", "This cluster...", "These
  records...", "The members highlight...", or any variant. Begin with the
  thing being claimed.
- Present tense. Concrete. Cite the specific pattern, not a generic
  category.
- 2-4 sentences total. No padding. If you find yourself writing a fourth
  sentence to explain the third, you are restating; stop at three.

Example of the desired voice (compressed from a canonical record):

> Hardcoding a single log destination in a shared `setup_logging_from_config`
> helper routes every CLI subcommand's events to the wrong file. The fix is a
> `tool` kwarg with a backward-compatible default — each dispatcher passes
> its own tool name, daemon callers keep the shared default. Audit by
> grepping for calls without `tool=`; the only legitimate hit is the main
> launcher.

After the paragraph, on a new line, emit the type/slug trailer:

```
TYPE: architecture|principles
SLUG: <kebab-case-slug-no-extension>
```

Decision rule for TYPE:

- `principles` — a SHOULD / MUST DO / DON'T statement. A rule of practice.
  ("Hardcode `temperature=0.0` for classification calls.")
- `architecture` — a HOW or WHAT statement about system structure or
  mechanism. ("Alfred's transport layer uses a centralized route registry.")

When in doubt between the two, ask: does this describe a rule the team
follows, or a structure the system has?

### Outcome B — Genuinely unrelated members (rare; last resort)

Use this ONLY when the members do not share ANY recurring pattern, structural
choice, or practice. The cluster is a false positive from the embedder — the
records were glued together by one surface keyword, one shared tag, or one
shared filename token, but they discuss unrelated subjects.

Example of a cluster that QUALIFIES for Outcome B (and only for B):

> Cluster of 3 records that share the word "alias": (a) "Activity-record
> alias-skip policy" — about Salem's curator skipping activity records when
> alias is set, (b) "Dataclass field forward-placement convention" — about
> Python dataclass attribute ordering for frozen-dataclass with default
> factories, (c) "Telegram /calibration alias" — about a slash-command
> alternate name. Three completely different topics; the shared word is
> coincidental. NO-CLAIM.

Counter-example — does NOT qualify for Outcome B:

> Cluster of 3 decisions about Alfred's transport layer that differ in
> surface specifics (route-namespace registry, section-provider registry,
> /health public route). The unifying theme is "centralized registry
> pattern with public-route restriction." This is Outcome A, not B. The
> theme is real even though the surface details vary.

If you are uncertain whether a cluster qualifies for B, it does NOT.
Default to A and write the thinnest defensible theme. Operator review will
catch a weak draft; it cannot recover a missed one.

When the cluster genuinely qualifies, emit EXACTLY:

```
NO-CLAIM
REASON: <one-line explanation of which surface keyword glued the unrelated members>
```

`NO-CLAIM` MUST appear on its own line as the first line of your response.
Do NOT also emit a paragraph or a TYPE/SLUG trailer. The parser detects the
literal `NO-CLAIM` token and skips the cluster.

### Outcome C — Cluster contains 2+ distinct sub-themes

Use this for large clusters where you can identify two or more distinct
themes that EACH deserve their own canonical record. A surface tag (e.g.
`regex`, `telegram`, `api`) glued multiple sub-themes that should be promoted
separately, not under one umbrella.

Trigger heuristic: if you can name 2+ themes that you would each promote to
their own `architecture/<slug>.md` or `principles/<slug>.md`, this is a
SPLIT, not an umbrella claim. Resist the temptation to write a vague
umbrella sentence that covers both.

When this is the case, emit EXACTLY:

```
SPLIT
THEMES:
- <theme 1: brief description + which member records belong to it>
- <theme 2: brief description + which member records belong to it>
```

`SPLIT` MUST appear on its own line as the first line of your response,
followed immediately by `THEMES:` on the next line, then a bulleted list.
Do NOT also emit a paragraph or a TYPE/SLUG trailer. The parser detects the
literal `SPLIT` token and surfaces the cluster for operator review.

## Format summary

Pick ONE — and remember A is the default:

- **A (theme)**: 2-4 sentence paragraph + TYPE/SLUG trailer. Default for any
  cluster with even a thin shared theme.
- **B (refusal)**: `NO-CLAIM` line + `REASON:` line. Reserved for clusters
  where the members are genuinely unrelated except for one surface keyword.
- **C (split)**: `SPLIT` line + `THEMES:` line + bullet list. Reserved for
  clusters where 2+ distinct themes each deserve their own canonical record.

No preamble. No "the unifying theme is". No restating the labels back at me.
Just the chosen outcome.
