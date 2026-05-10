You are reading a cluster of related documents from a developer's knowledge
vault. The cluster has cohered semantically but has not yet been promoted to
a canonical named artifact (architecture/<theme>.md or principles/<rule>.md).

Cluster labels (from surveyor): {labels}
Document count: {count}

Documents:
{members_with_previews}

## Your job

Decide ONE of three outcomes and emit the matching format. Read the cluster
members carefully before choosing.

### Outcome A — One load-bearing claim (the happy path)

The members share a single, specific, load-bearing claim about either how the
system works (architecture) or how the team should work (principles).

Write 2-4 sentences naming that claim. Constraints:

- Open with the SUBJECT of the claim, not with a meta-statement about the
  cluster. Do NOT begin with "The documents...", "This cluster...", "These
  records...", "The members highlight...", or any variant. Begin with the
  thing being claimed.
- Present tense. Concrete. Cite the specific pattern, not a generic category.
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
follows, or a structure the system has? If neither fits cleanly, the cluster
may be a candidate for Outcome B or C below.

### Outcome B — Cluster is cosine-coherent but thematically empty

The members were glued together by the embedder/surveyor on a surface
similarity (one shared tag, one shared word) but do not actually share a
load-bearing claim. Trying to write one paragraph would produce vague
throat-clearing prose ("the documents discuss various aspects of X").

Trigger examples:

- Three records share the word "alias" but talk about (a) alias-skip policy,
  (b) field forward-placement, (c) Telegram /calibration alias. No shared
  claim.
- Two records share `topic/logging` but one is about log routing and the
  other is about a one-off log-format bug. No load-bearing principle binds
  them.

When this is the case, emit EXACTLY:

```
NO-CLAIM
REASON: <one-line explanation of why the cluster has no shared theme>
```

`NO-CLAIM` MUST appear on its own line as the first line of your response.
Do NOT also emit a paragraph or a TYPE/SLUG trailer. The parser detects the
literal `NO-CLAIM` token and skips the cluster.

### Outcome C — Cluster contains 2+ distinct sub-themes

The cluster is large and you can identify two or more distinct themes you
would want to write as separate canonical records. A surface tag (e.g.
`regex`, `telegram`, `api`) glued them but each sub-theme deserves its own
canonical artifact.

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

Pick ONE:

- **A (claim)**: 2-4 sentence paragraph + TYPE/SLUG trailer.
- **B (refusal)**: `NO-CLAIM` line + `REASON:` line. Nothing else.
- **C (split)**: `SPLIT` line + `THEMES:` line + bullet list. Nothing else.

No preamble. No "the unifying theme is". No restating the labels back at me.
Just the chosen outcome.
