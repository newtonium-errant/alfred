# Endpoint Signal Contract — Adaptive Turn-End (Increment 1)

**Status:** FROZEN. This document is the authoritative source for the linguistic
signal set and decision rules of the adaptive turn-end (endpointing) feature.
The builder implements the rules in §3 **literally**; the frozensets in §2 are
the exact ship values.

**Owner:** prompt-tuner (linguistic judgment layer). **Consumer:** builder
(`classify_tail` + the hold/commit mechanism in `voice_stt.py`, the config
dataclass in `config.py`, the telemetry sink modeled on `voice_stt_shadow.py`).

**Scope grounding:** `docs/adaptive_endpointing_scope.md` §2 (the mid-thought
signal set), §4 (error-bias), §5 (privacy). Mechanical claims below were
verified against source: `barge_in.normalize_text` (`barge_in.py:59-64`), the
finals-buffer tail join (`voice_stt.py:~507`, plus the extracted commit paths
`_commit_inline` ~:575 and `_commit_held` ~:639), and `smart_format`
(`config.py:203`, wired to the Deepgram URL at `stt_deepgram.py:78`). Line
numbers in the worktree are approximate and will settle when the feature commits.

**Implementation status (reconciled against the builder's landed code).** The
builder has implemented this contract in `src/alfred/web/endpoint_hold.py`
(`classify_tail`, `TailResult`, `EndpointHoldSettings`,
`normalize_endpoint_hold_settings`) and wired it at the seam
(`voice_stt.py:531` — `classify_tail(text, self._last_partial, self._endpoint)`).
That module currently carries a CONSERVATIVE STARTER lexicon (scope §2), NOT the
final calibration; the frozensets in §2 of THIS contract are the swap-in target
— a DATA change to the module's identically-named sets (`CONJUNCTIONS`,
`FILLERS`, `FILLER_PHRASES`, `DANGLING_FUNCTION_WORDS`). §3 documents the ACTUAL
implemented signature and return shape.

---

## 1. Posture — conservative-at-ship

Ship-time posture is **conservative: under-trigger the hold.** COMMIT (fire now)
is the DEFAULT decision; a HOLD requires a POSITIVE lexical signal. A word earns
a slot in a frozenset only if a sentence **ending on it is almost always still
going**. When in doubt, a word is EXCLUDED.

This encodes the §4 error-bias correctly: a false-HOLD costs a bounded ≤ ceiling
(~1500ms) of extra silence and nothing else; a false-FIRE cuts the operator off
mid-thought and is the costly, relationship-damaging error. The bias toward
holding is **asymmetric and gated** — it applies only *after* a positive lexical
signal fires. The zero-signal path is structurally never held, so crisp turns
stay snappy regardless of anything tuned later. Small, defensible sets are the
whole point: every included word is a potential false-hold.

Per-user learning (Increment 2) later tunes both the trigger set and the hold
size against real false-early/false-hold evidence; see §6 for the re-add
candidates whose data Increment 1 captures.

---

## 2. The three frozensets (exact ship values)

All entries are lowercase. Single-token unless marked as a multi-word phrase
(matched on the last two tokens).

### 2.1 CONJUNCTIONS
Trailing coordinating/subordinating conjunctions that signal continuation.

```
{and, so, but, or, because, if, when, although, unless, until}
```

**Rationale (kept):**
- `and, but, or, because` — essentially never end an utterance; `and`/`so`/
  `because` are the canonical friction the scope names.
- `if, when` — trailing → continuation ("check if…", "call me when…"); the few
  terminal uses are rare idioms ("say when") or become questions ("since when?"
  → `?` veto).
- `although, unless, until` — grammatically CANNOT end a sentence (each requires
  a following clause). Zero terminal risk.
- `so` — kept as a scope-canonical signal, but see the §6 monitor note: "I think
  so / I told you so / or so" are real terminal uses (mostly punctuated → vetoed,
  not always).

**Dropped from the scope starter list, with reasoning:**

| Word | Reason for exclusion |
|---|---|
| `that` | Highest FP risk in the list. Demonstrative/pronoun terminal use dominates speech: "do that", "move that", "I told you that", "like that". Subordinator-trailing is a minority. |
| `then` | Idiomatic terminal use is common and often un-punctuated: "okay then", "see you then", "back then", "even then". (§6 top re-add candidate.) |
| `while` | Frequent terminal noun: "in a while", "for a while", "been a while". The single-token rule cannot see the "a while" context. |
| `as` | Scope-flagged. Fixed phrases ("as is", "as such", "as usual") end on the *following* word; bare trailing `as` has thin unique recall. |
| `yet` | Scope-flagged. Adverbial terminal dominates: "not yet", "haven't seen it yet". Conjunction use ("simple yet elegant") is rare in speech. |
| `before` / `after` | Scope-flagged. Adverbial/prepositional terminal common: "seen it before", "the day after". |
| `since` | "ever since", "long since" are terminal; modest recall. |
| `though` | Sentence-final discourse marker extremely common: "I like it though", "not today though". **Asymmetry is intentional:** `although` is kept and `though` is dropped — `though` CAN end a sentence, `although` cannot. |
| `which` | Vanishingly rare as a trailing token in speech in either direction; dropped for leanness. |
| `plus` | Noun terminal: "that's a plus", "a big plus". |
| `nor` | Near-absent in spoken register; no recall. |

### 2.2 FILLERS (`FILLERS`) + filler phrases (`FILLER_PHRASES`)
Trailing hesitation markers.

```
FILLERS         = {um, umm, uh, uhh, er, hmm}
FILLER_PHRASES  = {let me, i mean}          (matched on the last two tokens)
```

**Rationale (kept):**
- `um, umm, uh, uhh, er, hmm` — carry no terminal meaning mid-tail.
- `let me` — bulletproof: grammatically REQUIRES a following verb, cannot end an
  utterance ("let me… check").
- `i mean` — strong self-repair continuation ("cancel the 3pm, I mean the 4pm").
  Weakest keeper / first monitor (terminal risk only in "…know what I mean",
  usually `?`-vetoed).

**Dropped, with reasoning:**

| Word | Reason for exclusion |
|---|---|
| `like` | Verb-terminal common and un-punctuated: "if you like", "whatever you like", "the ones I like", "as you like". (§6 per-user re-add candidate.) |
| `well` | **Filler-"well" is clause-INITIAL, not trailing** ("Well, I think…"). A *trailing* "well" is the adverb/adjective: "oh well", "as well", "get well", "did well". High FP, ~zero recall as a trailing token. |
| `you know` | Frequently a thought-CLOSING tag = COMPLETE: "we should just cancel it, you know". |
| `kind of` / `sort of` | Scope-flagged. Standalone hedge replies are terminal: "kind of.", "sort of." (§6 monitors — the word-finding "it's kind of a… big deal" is genuine friction but too risky at ship.) |
| `i guess` | Standalone/final resigned hedge terminal: "okay, I guess", "yeah I guess". |

### 2.3 DANGLING_FUNCTION_WORDS
Articles / prepositions / determiners / possessives an English utterance
essentially never ends on. This is the tightest set — "near-zero FP as an
utterance-final word" is a high bar.

```
{the, a, an, my, your, our, their, to, of}
```

**Rationale (kept):**
- `the, a, an` — articles; cannot end an utterance ("…move it to the [noun]").
  Highest-value word-finding signals.
- `my, your, our, their` — possessive DETERMINERS only; always take a noun.
  (`his`/`her` are deliberately excluded — see below.)
- `to` — highest-value dangling word. The infinitive marker ("I need to…",
  "we're going to…") is the most common word-finding pause and is unambiguously
  incomplete. Accepts a monitored residual FP from preposition-stranding ("send
  it to", "someone to talk to") — mostly `?`-vetoed, bounded otherwise.
- `of` — rarely strands ("made of", "proud of" are the only real terminal
  cases); trailing "of" overwhelmingly continues ("a couple of…", "one of…").

**Dropped, with reasoning:**

| Words | Reason for exclusion |
|---|---|
| `for, with, at, by, in, on` | Preposition-stranding / phrasal-verb terminal is common: "looking for", "deal with", "good at", "stop by", "come in", "hold on / come on / go on / so on". `on` is the worst offender. Where these continue, an article usually follows ("in **the**…") and `the` catches it anyway. |
| `his, her` | Double as possessive-pronoun / object-pronoun → terminal: "it's his", "call her", "tell her", "give it to her". Only the unambiguous determiners `my/your/our/their` survive. |
| `is, are, was, were` | Copula-final is extremely common and often un-punctuated: "here it is", "there you are", "yes it was", "leave it as is". (Dropping `is` also closes the scope-flagged "as is" trap.) |
| `will, would, could, should, might, must` | Elliptical-final common in dialogue: "yes I will", "I wish I could", "maybe we should", "if you must". Marginal unique recall — word-finding pauses usually land on the article / `to` / `of` slot, not the modal. |

**Not added** (considered, rejected for leanness): `into/onto/during/within/from`
(low frequency); `gonna/wanna/gotta` (transcription-dependent — smart_format
usually renders "going to" → already caught by `to`). Noted as future
considerations only.

---

## 3. Decision rules (normative — implement literally)

**Signature (as implemented in `endpoint_hold.py`):**
`classify_tail(text: str, last_partial: str, settings: EndpointHoldSettings) -> TailResult`
— a **pure function**. `TailResult` is a frozen dataclass with two fields:
`decision` (`"commit"` | `"hold"`) and `features` (the telemetry dict, below).
COMMIT is the default; a HOLD requires a positive lexical signal.

`last_partial` is a formal parameter but a **no-op for the Increment-1
decision** — accepted for interface stability and reserved for the scope's
future "partial longer than committed buffer" resume-tightening knob. Do NOT
wire `last_partial` into the decision logic in Increment 1: resume detection is
caller-side (the worker cancels an armed hold when a new partial/final arrives),
not a `classify_tail` concern. (This reconciles the interface note in the
original contract with the shipped 3-arg signature — the substance is
identical: the classifier's verdict is a pure function of `text` + `settings`.)

**Return `features` dict — all keys ALWAYS populated, regardless of decision:**
`trailing_is_conjunction`, `trailing_is_filler`, `trailing_is_dangling`,
`ends_with_terminal_punct`, `n_tokens`. The three category booleans are computed
PRE-TOGGLE and PRE-VETO: a category that is vetoed (terminal punct) OR whose
per-category toggle is OFF still records `trailing_is_X=true`, so a soak captures
what WOULD have held. The toggle (§7) suppresses only the HOLD DECISION for that
category — never its feature boolean.

### 3.1 Evaluation order — NON-NEGOTIABLE

**The TERMINAL-PUNCT VETO reads the RAW rstrip'd string BEFORE any tokenization
or normalization.** This ordering is normative and non-negotiable:
`barge_in.normalize_text` (`barge_in.py:62-63`) turns every non-alphanumeric
character into a space — it destroys the `.` / `?` / `!` that carries the
completeness cue. Therefore the veto MUST be evaluated on the raw string first;
tokenization happens only afterward, and only for the lexical check.

1. **VETO input — RAW string.** `stripped = text.rstrip()` (trailing WHITESPACE
   only, no normalization). `ends_with_terminal_punct = bool(stripped) and
   stripped[-1] in {".", "?", "!"}`. Source of the punctuation is `smart_format`
   (`config.py:203`, default `True`); if an operator sets `smart_format=false`
   the punctuation is simply never present, `ends_with_terminal_punct` is always
   `False`, and evaluation falls through to lexical-only (documented graceful
   degradation).
2. **Trailing token — dedicated strip, NOT `normalize_text`.**
   `last = text.strip().split()[-1].lower().strip(PUNCT_EDGES)` (or `""` when
   there are no words), where `PUNCT_EDGES = ".,!?;:\"'()[]{}—–-"` is stripped
   from the token EDGES only. This preserves internal apostrophes/hyphens
   ("don't" stays "don't", "twenty-one" stays intact). This is the exact reason
   `normalize_text` must not be reused for the single token: it would split
   "don't"→"don t" and make the last token "t".
3. **Last-two tokens — normalized path.** `last_two = " ".join(_tokens(text)[-2:])`
   (using `barge_in._tokens`, the normalized path). This is SAFE for the phrase
   check because no `FILLER_PHRASES` entry contains an apostrophe or internal
   punctuation, so the apostrophe-mangling that bars `normalize_text` from the
   single-token check is harmless here.
4. **Compute the category booleans + `n_tokens` UNCONDITIONALLY** (before the
   veto, before the toggles):
   - `trailing_is_conjunction = last ∈ CONJUNCTIONS`
   - `trailing_is_filler = (last ∈ FILLERS) or (last_two ∈ FILLER_PHRASES)`
   - `trailing_is_dangling = last ∈ DANGLING_FUNCTION_WORDS`
   - `n_tokens = len(_tokens(text))`

   Assemble the `features` dict now — it is returned on EVERY path (this is what
   lets a vetoed or toggled-off tail still record what would have held).
5. **Completeness VETO.** If `ends_with_terminal_punct` → return
   `TailResult("commit", features)`. High-confidence complete cue; overrides
   every lexical signal. **Missing terminal punctuation ALONE never holds** —
   absence-of-veto is not a signal; it only means evaluation proceeds to the
   toggle-gated hold. At ship, punctuation is a **binary veto**; "escalation"
   (scope §2) means missing-punct is *captured* as `ends_with_terminal_punct`
   but does NOT itself size the hold in Increment 1.
6. **Toggle-gated HOLD.** Otherwise HOLD iff any category fired AND its toggle is
   on:
   `hold = (trailing_is_conjunction and settings.hold_on_conjunction) or
   (trailing_is_filler and settings.hold_on_filler) or (trailing_is_dangling and
   settings.hold_on_dangling)`.
   Return `TailResult("hold" if hold else "commit", features)`.

Structure note: the three category booleans are OR-combined into the decision, so
"single-token vs multi-word filler" is not an ordered fallthrough —
`trailing_is_filler` is simply `(last ∈ FILLERS) or (last_two ∈ FILLER_PHRASES)`.
The result is identical to an ordered check because no `FILLER_PHRASES` entry's
final word appears in any single-token set; the two can never disagree.

### 3.2 Caller-owned bypasses (never reach `classify_tail`, never hold)

These are handled by the caller at the `EVENT_UTTERANCE_END` seam, upstream of
the `classify_tail(text, self._last_partial, self._endpoint)` call
(`voice_stt.py:531`): `self._closing`; `ev.trigger ∈ {finalize, fake}`;
`enabled=False`; and sub-`min_utterance_chars` (the existing `utterance_empty`
path). Consequence: a standalone "um"/"uh" (2 chars) is filtered upstream and
never holds. A standalone 3-char filler ("hmm"/"umm"/"uhh") CAN reach the
classifier and hold ~500ms — benign and bounded; flagged as an Increment-2
refinement (only hold on a filler when substantive content precedes it).

### 3.3 Edge cases (benign, snappy-default)

- **Empty / pure-punctuation trailing token.** An isolated pure-punctuation final
  token (e.g. a stray `—`) strips to `""` via `PUNCT_EDGES`; `""` is in no set,
  and with no second real token there is no phrase to form → all three category
  booleans `False` → **COMMIT** (the snappy default). (A punctuation token that
  trails two real filler-phrase words — e.g. "let me —" — still HOLDs via the
  normalized last-two path, which is the correct mid-thought read.)
- **Ellipsis.** Three ASCII periods (`...`) end in `.` → VETO → **COMMIT**. A
  single-character ellipsis `…` (U+2026) does NOT end in `.`/`?`/`!`, so it does
  not veto and falls through to the lexical check. Neither is a real production
  risk: `smart_format` emits `.`/`?`/`!` terminal punctuation, never raw
  ellipses (one line alongside the `smart_format=false` degradation note in §3.1
  step 1).

---

## 4. Worked walk-through (worked-example-accuracy artifact)

Each row walks the ACTUAL rules of §3, not a paraphrase.

| # | Tail (as `classify_tail` receives it) | Trace | Decision |
|---|---|---|---|
| 1 | `move it to the` | no `.?!`; last=`the` ∈ DANGLING | **HOLD** (word-finding) |
| 2 | `let me` | no punct; last=`me` ∉ single sets; last-two=`let me` ∈ `FILLER_PHRASES` | **HOLD** |
| 3 | `I need to call him because` | no punct; last=`because` ∈ CONJUNCTIONS | **HOLD** |
| 4 | `send it to Bob and, um` | no punct; last=`um` (edge-strip removes any trailing comma) ∈ `FILLERS` | **HOLD** |
| 5 | `yes` | no punct (smart_format omits period on crisp final); last=`yes` ∉ all sets | **COMMIT** (proves missing-punct-alone ≠ hold; stays snappy) |
| 6 | `move it to Friday.` | trailing `.` → VETO (and last token `friday` ∉ sets anyway) | **COMMIT** |
| 7 | `I'll think about that` | no punct; last=`that` — deliberately EXCLUDED | **COMMIT** (that-trap avoided) |
| 8 | `leave it as is` | no punct; last=`is` — EXCLUDED from DANGLING | **COMMIT** (copula-final trap avoided) |
| 9 | `so anyway` | no punct; last=`anyway` ∉ sets (trailing `so` is NOT the tail token) | **COMMIT** (proves last-token-only inspection) |

**Honest residual FP shown by the rules:** `who should I send it to` → if
smart_format emits `?`, VETO → COMMIT; if not, last=`to` ∈ DANGLING → HOLD (a
bounded ≤ ceiling false-hold from preposition-stranding). This is the known,
monitored cost of keeping `to` for its large infinitive recall.

---

## 5. Privacy (scope §5) — hard constraints

Live in-process inspection of partial/final tail text is fine (same posture as
barge reading spoken text live). Durable capture is constrained as follows, and
these are **hard constraints enforced by the sink schema and gated by the
independent QA/code review before merge:**

1. Durable telemetry records **FEATURES-ONLY booleans / scalars**:
   `trailing_is_conjunction`, `trailing_is_filler`, `trailing_is_dangling`,
   `ends_with_terminal_punct`, `n_tokens`, `decision`, `hold_ms_applied`,
   `resumed_within_hold`, `ms_trailing_silence_at_fire`, and the per-signal
   attribution category. The three category booleans are recorded on EVERY
   decision path — including a VETOED tail and a category whose toggle is OFF —
   so the soak corpus captures what WOULD have held (see the §3 return shape).
2. **The raw tail text is NEVER logged.**
3. **The specific matched word is NEVER logged** — only the category boolean.
   "because" must never appear in the sink; only `trailing_is_conjunction=true`.
   This is normative and tighter than a category-only guarantee: neither the raw
   tail nor the individual triggering token may be persisted. A single accidental
   raw-tail or matched-word field is a contract violation of the
   no-transcript-in-logs guarantee.

These categories are the ONLY content-derived values captured. This layer is one
step closer to content than barge's chars/scores-only capture, so it is the
single net-new privacy consideration and MUST be independently QA-reviewed before
merge.

---

## 6. Increment-2 per-user re-add candidates (deferred)

The Increment-1 telemetry (§5) captures the per-signal attribution category so
the data exists to greenlight these later, per-user, behind the scope §3 go/no-go
gate. None are shipped in Increment 1.

- **`then`** (conjunction) — top re-add candidate; strong enumeration
  continuation, held out only for its idiomatic terminal uses ("okay then").
- **`so`** (conjunction, currently KEPT) — monitor for false-holds from "I think
  so / I told you so"; a per-user weight-down is the natural correction.
- **`i mean`** (filler, currently KEPT) — weakest keeper; monitor for
  "…know what I mean" false-holds.
- **`kind of` / `sort of`** (fillers) — the word-finding "it's kind of a… big
  deal" is genuine friction; re-add per-user if the hedge-reply terminal rate is
  low for this operator.
- **`like`** (filler) — re-add per-user if the operator's filler-"like" rate
  proves high (held out for verb-terminal "if you like / whatever you like").

The correction signals that drive these (FALSE-EARLY-FIRE → weight-up the present
tail signal; FALSE-HOLD → weight-down) are defined in scope §3; the per-signal
weights are the deferred second learning dimension there.

---

## 7. Config interface recommendation (builder-owned naming, agreed interface)

Recommend the endpoint-hold config expose **per-category toggles** so a soak can
disable one noisy category without a code change:

- `hold_on_conjunction` (default `True`)
- `hold_on_filler` (default `True`)
- `hold_on_dangling` (default `True`)

Field naming is builder-owned (lives on `WebVoiceEndpointHoldConfig` alongside
`base_extend_ms` / `max_total_hold_ms` / `enabled`); this is a flagged, agreed
interface, not a frozen name. Aligns with the "signal toggles" already noted for
that dataclass in scope §2. When a category is toggled off, `classify_tail`
suppresses only the HOLD DECISION for that category (§3.1 step 6 — that category
can never produce a HOLD); its feature boolean is still computed and recorded for
telemetry (§3.1 step 4, §5). The `EndpointHoldSettings` dataclass in
`endpoint_hold.py` already carries these three toggles (`hold_on_conjunction`,
`hold_on_filler`, `hold_on_dangling`, all default `True`).
