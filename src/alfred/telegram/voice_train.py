"""Voice & method-source training pipeline (2026-05-07 arc).

Two slash commands at the bot layer (``/train`` + ``/method-source``)
ingest a paste-or-attached-content into a TWO-RECORD shape:

    Raw record  (write-immediate, sub-2s ack)
        essay/<slug>.md           ←  /train
        source/<slug>.md          ←  /method-source

    Structured record  (built async by the extraction worker)
        voice/<slug>.md           ←  /train (voice profile)
        method/<slug>.md          ←  /method-source (method profile)

Then a third tier for /train only — async cluster + overall profile
builders that rebuild aggregate records when ≥2 leaves share a
``cluster:`` tag (cluster tier) or ≥2 cluster summaries exist
(overall tier).

Architecture
------------

The slash-command handlers (``bot.on_train`` / ``bot.on_method_source``)
do three things, all sub-2s:

    1. Resolve the input text (slash-arg → message text → most-recent
       conversation paste).
    2. Save the RAW record immediately via ``ops.vault_create`` with
       ``extraction_status: pending``.
    3. Enqueue a job onto the JSONL extraction queue.
    4. Reply "saved, voice/method extraction queued".

The extraction WORKER runs as an asyncio task inside the talker
daemon — same lifecycle as the gap-timeout sweeper / heartbeat /
transport scheduler. Every poll interval it:

    1. Reads pending jobs from the JSONL queue.
    2. For each job: calls Opus with the appropriate extraction prompt.
    3. Writes the structured record via ``ops.vault_create``.
    4. Flips the raw record's ``extraction_status`` from ``pending`` →
       ``complete`` (or ``failed`` on error).
    5. DMs the operator on completion / failure.
    6. For /train: triggers cluster + overall profile rebuilds when
       the new leaf's cluster tag now has ≥2 leaves.

Queue file format
-----------------

JSONL — one job per line. Append-on-enqueue, read-and-truncate on
worker tick. Schema:

    {
      "job_id": "uuid4",
      "kind": "voice" | "method",
      "raw_rel_path": "document/essay/<slug>.md",
      "raw_name": "If You're Not Doing This Then You're Being Left Behind",
      "raw_body": "<the full essay text>",
      "cluster": "veteran" | null,           # /train only
      "image_metadata": [{...}] | [],         # /method-source w/ image
      "chat_id": 12345,                       # for completion DM
      "enqueued_at": "iso8601",
      "instance": "Hypatia",                  # Hypatia-only Phase 1
    }

Cross-instance neutrality
-------------------------

The module is config-flippable per-instance. Helpers (slug, queue,
vault routing, profile schema) take instance/config arguments and
DON'T hardcode ``hypatia``. Phase 1 only Hypatia opts in via the
``telegram.voice_train.command_enabled`` config knob; future Salem
or KAL-LE adoption is a config flip.

Failure model
-------------

* RAW save fails → reply "couldn't save raw record: <err>". No queue
  entry written. Operator can re-run.
* QUEUE write fails → raw record's ``extraction_status`` flipped to
  ``failed`` so the operator knows to re-run.
* WORKER extraction fails → raw record stays ``failed``; structured
  record NOT written. Operator re-issues the slash command on the
  same paste.
* CLUSTER / OVERALL builder fails → leaves untouched, log emitted,
  next leaf addition retries.

Per ``feedback_intentionally_left_blank.md``: every failure path here
emits an explicit user-facing reply (or DM in the worker case).
Silent absence is forbidden.
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import structlog

from alfred.vault import ops

from ._anthropic_compat import messages_create_kwargs
from .utils import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------
#
# Same shape as ``fiction.slug_from_title`` (lowercase ASCII + hyphens,
# NFKD-normalize for accented Latin, 80-char cap with last-hyphen-snap)
# but lives here as a standalone helper so the voice_train module
# doesn't pull in the fiction module just for slug logic. The two
# helpers MUST stay aligned in shape — operator filenames feel
# uniform across slash-command outputs only when the slug rule is
# the same.

_SLUG_DROP = re.compile(r"[^a-z0-9-]+")
_SLUG_MAX_LEN = 80
_DEFAULT_SLUG = "untitled"


def slug_from_text(text: str) -> str:
    """Derive a filename-safe slug from a longer text or title.

    First non-empty line is used as the title-source. NFKD-normalize +
    strip combining marks (so ``café`` → ``cafe``), lowercase, collapse
    whitespace to hyphens, drop non-alphanumerics, collapse runs of
    hyphens, strip edges, cap at 80 chars.

    Empty / all-punctuation input returns ``"untitled"`` so the caller
    always lands at a real path.
    """
    if not isinstance(text, str):
        return _DEFAULT_SLUG
    # Use the first non-empty line as the title. Operators frequently
    # paste an essay where the first line is the headline.
    title_line = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Strip a leading markdown heading (``# Title`` / ``## Title``)
        # before falling through.
        m = re.match(r"^#+\s*(.+)$", stripped)
        title_line = m.group(1).strip() if m else stripped
        break
    if not title_line:
        return _DEFAULT_SLUG
    normalized = unicodedata.normalize("NFKD", title_line)
    ascii_only = "".join(
        c for c in normalized if not unicodedata.combining(c)
    )
    s = ascii_only.strip().lower()
    if not s:
        return _DEFAULT_SLUG
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_DROP.sub("", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s:
        return _DEFAULT_SLUG
    if len(s) > _SLUG_MAX_LEN:
        truncated = s[:_SLUG_MAX_LEN]
        last_hyphen = truncated.rfind("-")
        if last_hyphen >= _SLUG_MAX_LEN // 2:
            s = truncated[:last_hyphen]
        else:
            s = truncated
        s = s.rstrip("-")
    return s or _DEFAULT_SLUG


def title_from_text(text: str) -> str:
    """Derive a human-readable title from input text.

    Uses the first non-empty line, stripped of markdown heading
    markers. Falls back to a date-stamped placeholder if the input
    is empty or non-string.

    Distinct from ``slug_from_text`` because ``vault_create`` wants
    BOTH (the ``name`` arg is human-readable; the path is slugged
    separately).
    """
    if not isinstance(text, str):
        return f"Untitled — {_date.today().isoformat()}"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^#+\s*(.+)$", stripped)
        title = m.group(1).strip() if m else stripped
        # Cap at a reasonable length to keep filenames manageable.
        # Filesystems handle long names fine but operators don't read
        # 200-char filenames.
        if len(title) > 100:
            title = title[:100].rstrip()
        return title
    return f"Untitled — {_date.today().isoformat()}"


# ---------------------------------------------------------------------------
# Queue file (JSONL)
# ---------------------------------------------------------------------------


@dataclass
class ExtractionJob:
    """One pending extraction job (one line in the JSONL queue)."""

    job_id: str
    kind: Literal["voice", "method"]
    raw_rel_path: str
    raw_name: str
    raw_body: str
    cluster: str | None = None
    image_metadata: list[dict[str, Any]] = field(default_factory=list)
    chat_id: int = 0
    enqueued_at: str = ""
    instance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "raw_rel_path": self.raw_rel_path,
            "raw_name": self.raw_name,
            "raw_body": self.raw_body,
            "cluster": self.cluster,
            "image_metadata": list(self.image_metadata),
            "chat_id": self.chat_id,
            "enqueued_at": self.enqueued_at,
            "instance": self.instance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractionJob":
        # Schema-tolerance contract (per CLAUDE.md): filter to known
        # fields so a future schema bump can land alongside an in-flight
        # queue entry without crashing the worker.
        known = {
            k: v for k, v in data.items() if k in cls.__dataclass_fields__
        }
        # Coerce default types where missing.
        known.setdefault("cluster", None)
        known.setdefault("image_metadata", [])
        known.setdefault("chat_id", 0)
        known.setdefault("enqueued_at", "")
        known.setdefault("instance", "")
        return cls(**known)


def enqueue_job(queue_path: Path, job: ExtractionJob) -> None:
    """Append one job to the JSONL queue, atomically.

    Uses append-mode write so concurrent enqueuers don't race. Each
    line is a single JSON object terminated by ``\\n``. The worker
    reads the file in one shot (read-and-truncate model), so partial
    writes are tolerated only at line boundaries — which append-mode
    guarantees on POSIX for writes <PIPE_BUF (4KB). Bodies bigger than
    that are still safe because Python's text-mode write buffers
    flushed on file close, not on intermediate writes.
    """
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(job.to_dict(), ensure_ascii=False) + "\n"
    with open(queue_path, "a", encoding="utf-8") as f:
        f.write(line)
    log.info(
        "voice_train.queue.enqueued",
        kind=job.kind,
        job_id=job.job_id,
        raw_rel_path=job.raw_rel_path,
        cluster=job.cluster,
    )


def drain_queue(queue_path: Path) -> list[ExtractionJob]:
    """Read all pending jobs and truncate the queue file.

    Read-and-truncate model: the worker takes ownership of every job
    it reads, so a crash mid-extraction loses those jobs (the operator
    re-runs the slash command). Simpler than per-job ack/checkpoint
    state, and the volume is low (manual paste rate).

    Returns an empty list if the queue file doesn't exist or is empty.
    Malformed lines are logged + skipped (don't crash on bad JSON).
    """
    if not queue_path.exists():
        return []
    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        log.warning("voice_train.queue.read_failed", error=str(exc))
        return []
    if not content.strip():
        return []
    jobs: list[ExtractionJob] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning(
                "voice_train.queue.bad_line",
                line_preview=line[:100],
                error=str(exc),
            )
            continue
        try:
            jobs.append(ExtractionJob.from_dict(data))
        except (TypeError, KeyError) as exc:
            log.warning(
                "voice_train.queue.bad_job",
                error=str(exc),
                keys=list(data.keys()) if isinstance(data, dict) else None,
            )
            continue
    # Truncate AFTER successful read so an OSError above doesn't lose
    # jobs — they're still on disk for the next tick.
    try:
        queue_path.write_text("", encoding="utf-8")
    except OSError as exc:
        log.warning("voice_train.queue.truncate_failed", error=str(exc))
    return jobs


# ---------------------------------------------------------------------------
# Conversation history helper — "most-recent paste"
# ---------------------------------------------------------------------------


def find_most_recent_user_paste(
    transcript: list[dict[str, Any]],
    *,
    min_chars: int = 200,
) -> str:
    """Return the most-recent user message text >= ``min_chars`` chars.

    Used by the slash commands when the operator types just ``/train``
    or ``/method-source`` with no following text — they want the
    handler to classify the most-recent long paste in conversation
    memory.

    Walks the transcript in REVERSE so the most-recent qualifying user
    turn wins. Returns an empty string when no qualifying paste is
    found (the caller surfaces a "no recent paste" reply).

    Tool-result and image-content turns are skipped (we want plain
    text only). Anthropic-style ``content`` may be a string OR a list
    of content blocks; we extract text from both shapes.
    """
    if not transcript:
        return ""
    for turn in reversed(transcript):
        if turn.get("role") != "user":
            continue
        content = turn.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Pull together every text block. Image / tool_result
            # blocks contribute nothing.
            text_parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text") or ""))
            text = "\n\n".join(text_parts)
        text = text.strip()
        if len(text) >= min_chars:
            return text
    return ""


# ---------------------------------------------------------------------------
# Raw-record save (write-immediate path)
# ---------------------------------------------------------------------------


@dataclass
class SaveRawResult:
    """Outcome of a write-immediate raw-record save."""

    rel_path: str
    name: str
    slug: str
    success: bool
    error: str = ""


def save_raw_essay(
    vault_path: Path,
    *,
    text: str,
    cluster: str | None,
    scope: str,
    image_metadata: list[dict[str, Any]] | None = None,
) -> SaveRawResult:
    """Save the raw essay record, returning the relative path.

    Writes ``document/essay/<slug>.md`` with frontmatter:

        type: essay
        status: pending      # flipped to ``published`` by the worker
                             # post-extraction; ``failed`` on error
        extraction_status: pending
        cluster: <name>      # only when /train --cluster was passed
        author: Andrew Errant
        target_publication: substack    # default for the substack workflow
        source_kind: paste              # vs ``image`` / ``pdf``
        created: <today>

    Body is the raw essay text verbatim.

    ``scope`` should be the calling instance scope ("hypatia" /
    "talker" / etc.) — passed through to ``vault_create`` so the
    create-allowlist check matches the running instance.
    """
    title = title_from_text(text)
    slug = slug_from_text(text)
    # ``status`` tracks the essay-lifecycle (draft / published /
    # archived) — independent of the extraction worker's processing
    # state, which lives on the separate ``extraction_status`` field.
    # Operator can flip status to ``published`` after the fact via
    # ``vault edit`` once the essay actually ships. The slash-command
    # workflow most often runs on already-published essays (backfill
    # from a substack post), but defaulting to ``draft`` is the safe
    # write — the operator confirms publication explicitly rather
    # than the slash command guessing.
    fields: dict[str, Any] = {
        "status": "draft",
        "extraction_status": "pending",
        "author": "Andrew Errant",
        "source_kind": "paste",
    }
    if cluster:
        fields["cluster"] = cluster
    if image_metadata:
        fields["source_kind"] = "image"
        fields["source_images"] = list(image_metadata)
    try:
        result = ops.vault_create(
            vault_path,
            "essay",
            title,
            set_fields=fields,
            body=_raw_essay_body(text),
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "voice_train.raw_essay.save_failed",
            error=str(exc),
            slug=slug,
        )
        return SaveRawResult(
            rel_path="", name=title, slug=slug,
            success=False, error=str(exc),
        )
    log.info(
        "voice_train.raw_essay.saved",
        rel_path=result["path"],
        slug=slug,
        cluster=cluster,
    )
    return SaveRawResult(
        rel_path=result["path"], name=title, slug=slug, success=True,
    )


def save_raw_source(
    vault_path: Path,
    *,
    text: str,
    scope: str,
    image_metadata: list[dict[str, Any]] | None = None,
) -> SaveRawResult:
    """Save the raw method-source record at ``source/<slug>.md``.

    Frontmatter:

        type: source
        status: pending
        extraction_status: pending
        source_kind: paste | image | pdf
        created: <today>

    Body is the raw source text verbatim. When ``image_metadata`` is
    populated, the body also includes the image reference(s) in a
    ``## Images`` section so the operator can find the originals
    later.
    """
    title = title_from_text(text)
    slug = slug_from_text(text)
    fields: dict[str, Any] = {
        "status": "pending",
        "extraction_status": "pending",
        "source_kind": "paste",
    }
    if image_metadata:
        fields["source_kind"] = "image"
        fields["source_images"] = list(image_metadata)
    body = _raw_source_body(text, image_metadata)
    try:
        result = ops.vault_create(
            vault_path,
            "source",
            title,
            set_fields=fields,
            body=body,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "voice_train.raw_source.save_failed",
            error=str(exc),
            slug=slug,
        )
        return SaveRawResult(
            rel_path="", name=title, slug=slug,
            success=False, error=str(exc),
        )
    log.info(
        "voice_train.raw_source.saved",
        rel_path=result["path"],
        slug=slug,
    )
    return SaveRawResult(
        rel_path=result["path"], name=title, slug=slug, success=True,
    )


def _raw_essay_body(text: str) -> str:
    """Render the raw-essay body — the verbatim paste with a footer."""
    cleaned = text.strip()
    return cleaned + "\n"


def _raw_source_body(
    text: str, image_metadata: list[dict[str, Any]] | None,
) -> str:
    """Render the raw-source body, with an optional image-references section."""
    cleaned = text.strip()
    if not image_metadata:
        return cleaned + "\n"
    image_lines = ["", "## Images", ""]
    for meta in image_metadata:
        path = meta.get("path") if isinstance(meta, dict) else None
        if path:
            image_lines.append(f"- `{path}`")
    return cleaned + "\n" + "\n".join(image_lines) + "\n"


# ---------------------------------------------------------------------------
# Extraction prompts (Opus)
# ---------------------------------------------------------------------------


VOICE_EXTRACTION_PROMPT = """\
You are extracting a structured voice profile from a single piece of \
Andrew Errant's writing. The goal is a fixture future ghostwriting / \
draft-tuning calls can read to match Andrew's voice precisely on \
similar work.

You will be given the FULL essay text. Your output is a Markdown \
document with structured frontmatter + a brief prose summary in the \
body. The downstream consumer parses the frontmatter directly and \
reads the body for context.

## Evidence-anchoring rule (load-bearing)

Every label, list entry, or characterisation in this profile must be \
**quotable**. A profile that says ``comic_moves: [deadpan, escalation]`` \
without naming WHERE in the essay those moves appear is useless for \
calibration — it could describe almost any writer. For every label, \
you should be able to point to a verbatim ≤12-word quote from the \
essay that demonstrates it. Lists below specify a ``with: "<short \
verbatim quote>"`` per entry where applicable. Quote exactly — do not \
paraphrase, do not insert ellipses inside the quote.

## Required frontmatter fields

All strings unless noted. Use YAML inline syntax (e.g. \
``comic_moves: [deadpan, escalation]``) for short lists; use block \
syntax for the evidence-bearing lists below.

  register: formal | casual | intimate | declarative | conversational | \
academic | hybrid (1-3 hybrid labels OK, e.g. "casual-declarative")
  paragraph_rhythm: short-paragraphs | medium-paragraphs | \
long-paragraphs | mixed-rhythm
  single_sentence_paragraphs_frequency: rare | occasional | frequent | \
dominant
  comic_moves:                  # 2-5 entries, evidence-anchored
    - move: deadpan-after-technical-detail
      with: "Some arts and crafts with a map"
    - move: escalation
      with: "..."
  opening_style: 1-line description of the typical opening shape, \
followed by a verbatim ≤12-word quote of the actual opening
  closing_style: same shape — 1-line description + verbatim ≤12-word \
quote of the actual closing
  transition_style: 1-line description (linking phrases? section \
breaks? em-dashes mid-paragraph?) + 1 verbatim example transition
  footnote_conventions: present | absent | inline-asides-instead | \
parenthetical-heavy
  punctuation_tics:             # 2-5 entries, evidence-anchored
    - tic: em-dash-mid-paragraph
      with: "the navigator — and yes I mean the role"
    - tic: italics-for-emphasis
      with: "..."
  lexicon_tells:                # 4-8 verbatim phrases / sentence \
starters / framings; pull verbatim from the essay; NO paraphrase
    - "..."
    - "..."
  voice_signature: one descriptive sentence (≤30 words) capturing the \
voice; concrete, not generic

Body (after the frontmatter):

  - One paragraph (3-5 sentences) describing the overall voice in \
plain prose. Concrete, NOT generic. Cite 2-3 short verbatim phrases \
from the essay, each in quotes.
  - One paragraph describing what NOT to do — voice elements another \
draft might falsely add (e.g. "do not add corporate buzzwords; do not \
write headline subheadings within paragraphs"). Be specific to this \
essay's posture, not generic writing-advice.

## When the essay has no clear voice

If the input is a fragment, a rough draft, or otherwise too thin to \
profile (under ~400 words, or stylistically inconsistent in a way that \
suggests Andrew was just typing not crafting), DO NOT fabricate a \
voice. Instead, return the frontmatter with:

  status: insufficient-evidence
  insufficient_reason: "<one sentence on what's missing>"

and a body that says ``This input was insufficient to extract a voice \
profile. <reason>.`` Do NOT pad with generic descriptors to look \
useful — silent absence is worse than honest absence here per the \
``intentionally left blank`` rule.

Output only the Markdown document — no commentary, no code fence, \
nothing before the frontmatter and nothing after the body. The \
frontmatter starts with ``---`` on the first line.
"""


METHOD_EXTRACTION_PROMPT = """\
You are extracting a structured method/system profile from a single \
piece of source material Andrew shared. The source might be a \
methodology document, a system-design writeup, a framework article, \
a productivity technique, etc. The goal is a fixture Andrew can \
reference later when applying the method to a specific project.

You will be given the FULL source text. Your output is a Markdown \
document with structured frontmatter + a brief prose summary in the \
body.

## Source-anchoring rule

This is an extraction, not a re-phrasing. When the source uses \
specific phrasing for a principle (e.g. "Make the change easy then \
make the easy change"), preserve that phrasing verbatim — don't \
soften it into your own words. Andrew picked this source because of \
how IT articulates the method; your job is to make that articulation \
queryable, not replace it.

## Required frontmatter fields

  method_kind: framework | technique | system | process | rubric | \
heuristic | other
  domain: 1-line description (e.g. "writing process", "team rituals", \
"financial planning", "skill acquisition")
  source_attribution: <author or system name as the source identifies \
itself, or "unknown" if the source doesn't say>
  core_principles:              # 3-5 entries; preserve source \
phrasing verbatim where the source has named the principle
    - principle: "<verbatim or close paraphrase>"
      gloss: "<one short imperative sentence in plain language>"
  procedural: yes | no          # yes if the method has steps; no if \
it's principle-only
  failure_modes: list[str]      # 2-4 ways the method commonly fails \
or is misapplied (extracted from source if named, inferred only if not)
  application_contexts: list[str]   # 2-4 contexts where the method \
fits well (use Andrew's actual project domains where they appear in \
the source — RRTS, Substack, Alfred, Newtonium, Hypatia — or describe \
abstractly when not)

Body (after the frontmatter):

  - ## Procedure (only if ``procedural: yes``)
    Numbered steps, 5-12 max. Each step is one short imperative \
sentence. Optional sub-bullets for clarification. If the source \
explicitly names a step (e.g. "Step 3: Refactor"), preserve the name.

  - ## When to apply
    One short paragraph describing the criteria for picking THIS \
method over alternatives. Cite the source's own framing if it \
addresses this directly.

  - ## Failure modes
    Numbered list, one per failure mode. Each entry: 1 sentence \
describing the failure + 1 sentence describing the early-warning sign.

  - ## Application guidance
    One paragraph describing how to map this onto a typical Andrew \
project (use the application_contexts from frontmatter to anchor; \
don't recommend wholesale adoption — recommend the smallest viable \
adaptation).

## When the source isn't actually a method

Some inputs Andrew shares look method-shaped but are really opinion \
essays, anecdotes, or rambles that don't formalise into principles + \
procedure. If the source has fewer than 2 articulable principles, \
return:

  status: not-a-method
  not_a_method_reason: "<one sentence on what kind of source this \
actually is>"

with a body that says ``This source did not contain an extractable \
method. <reason>.`` Do NOT manufacture principles to fit the schema \
— a wrong method profile is worse than a clear "this isn't one."

Output only the Markdown document — no commentary, no code fence, \
nothing before the frontmatter and nothing after the body.
"""


VOICE_CLUSTER_PROMPT = """\
You are synthesizing a voice CLUSTER profile from multiple leaf voice \
profiles that share a cluster tag. The cluster represents a posture \
Andrew uses across several pieces (e.g. "veteran" for veteran-affairs \
writing, "technical" for systems writeups, "personal-essay" for \
substack drafts).

You will be given the leaf voice profiles in order. Each leaf already \
contains evidence-anchored frontmatter (comic_moves, punctuation_tics, \
lexicon_tells, etc.) with verbatim quotes attached. Your job is to \
**aggregate by COUNTING across leaves**, not to re-characterise from \
scratch. If 4 of 5 leaves list ``deadpan-after-technical-detail`` as \
a comic move, that's a signature move of this cluster. If only 1 of \
5 does, it's leaf-specific noise — drop it.

## Aggregation rules

  - **Union with frequency**: for each list field (comic_moves, \
punctuation_tics, lexicon_tells), count how many leaves include the \
entry. Sort descending by count. Drop entries that appear in only 1 \
leaf unless the cluster has only 2 leaves.
  - **Preserve evidence**: each retained entry should keep ONE \
representative verbatim quote from the leaves (pick the most \
characteristic one).
  - **Consolidated labels** (register, paragraph_rhythm): if the \
leaves disagree, name the disagreement (``register: \
casual-with-academic-asides``) rather than averaging.

## Required frontmatter fields

  cluster_name: <name>
  leaf_count: <n>
  leaf_titles: list[str]         # the file basenames or essay titles \
of the leaves used (so downstream readers can trace back)
  register: <consolidated label, may be hybrid; name disagreement if \
present>
  paragraph_rhythm: <consolidated>
  comic_moves:                   # ordered by leaf-frequency desc
    - move: <name>
      seen_in: <n_of_total>
      with: "<one representative ≤12-word quote>"
  punctuation_tics:              # same shape
    - tic: <name>
      seen_in: <n_of_total>
      with: "<quote>"
  lexicon_tells:                 # phrases in ≥2 leaves
    - "<verbatim phrase>"
  signature_moves: list[str]     # 3-6 moves present in ≥60% of \
leaves (the cluster's fingerprint — distinct from comic_moves; can \
include structural patterns like "opens with a scene then pivots")
  voice_signature: one descriptive sentence (≤30 words) capturing \
the cluster's posture; concrete

Body (after frontmatter):

  - ## What this cluster sounds like
    2-3 paragraphs in plain prose describing the cluster's voice. \
Concrete. Cite 2-3 short verbatim phrases drawn from the leaves, \
each in quotes, each tagged with the leaf it came from \
(``"…" — from <leaf-title>``).

  - ## What's distinctive about this posture
    1-2 paragraphs describing what makes this cluster recognisable \
on its own terms — the specific stance, audience-stance, register, \
or rhetorical move that defines it. (You don't have the other \
clusters in front of you; describe THIS cluster's defining shape \
without comparison, and trust that distinctiveness will emerge by \
contrast at the overall-profile stage.)

  - ## Worked example sketch
    A 3-5 sentence pseudo-paragraph in the cluster's voice, on a \
made-up topic Andrew has not actually written about, demonstrating \
the consolidated feel. CRITICAL: this must be a fresh demonstration \
of the cluster's signature_moves and lexicon — NOT a remix of any \
single leaf. Pick a topic clearly outside what's in the leaves \
(e.g. if the leaves are about veteran affairs and Substack process, \
write the sketch about train timetables or sourdough bread). Use \
≥2 of the signature_moves and ≥1 lexicon_tell visibly.

## When the cluster doesn't actually cohere

If the leaves don't share a recognisable voice (the cluster tag was \
likely wrong, or the leaves span genuinely different postures), \
return:

  status: incoherent-cluster
  incoherent_reason: "<one sentence on what doesn't fit>"

with a body section ``## Cluster does not cohere`` describing which \
leaves seem to belong together vs which seem misfiled. Don't \
manufacture a fake fingerprint to satisfy the schema.

Output only the Markdown document.
"""


VOICE_OVERALL_PROMPT = """\
You are synthesizing the OVERALL voice profile that aggregates \
multiple cluster summaries into a single ground-truth document about \
Andrew Errant's voice across all writing.

You will be given the cluster summaries. Your output is a Markdown \
document with frontmatter + a body that organises the postures.

## What this profile is FOR (and what it is NOT)

This profile is a calibration fixture for ghostwriting and copy-edit \
calls. It tells the next call: (a) what's invariant about Andrew's \
voice regardless of posture (the absolute fingerprint — must always \
be present), and (b) what AXES shift across postures (so the call \
can pick the right value for the piece in front of it). It is NOT \
a rehash of the cluster summaries — those exist already in the \
vault. Don't re-describe each cluster; that's wasted tokens. \
Cross-cluster invariants and the differential between postures are \
what only this profile can give.

## Required frontmatter fields

  cluster_count: <n>
  postures:                      # the cluster names, ordered by \
weight (number of leaves) descending
    - name: <cluster-name>
      leaf_count: <n>
  always_true:                   # 4-8 voice traits present across \
EVERY cluster (NOT "Andrew uses humor" — too vague. Try \
"sentence-level rhythm leans short→short→long, with the long \
sentence carrying the load.")
    - trait: "<concrete trait>"
      seen_in: "all <n> clusters"
      with: "<one short verbatim quote drawn from any cluster>"
  varies_by_posture:             # 3-6 dimensions where clusters \
DIFFER. Frame as axes, not values. e.g. "register: ranges from \
casual-confessional in personal-essay to dry-precise in technical"
    - axis: <name>
      range: "<value-A> in <cluster-X> → <value-B> in <cluster-Y>"

Body:

  - ## What stays constant
    One paragraph (4-6 sentences) describing the absolute fingerprint \
— what would tip a reader off that any of these pieces was written \
by Andrew, regardless of audience or topic. Cite 2-3 verbatim \
phrases (each tagged with the cluster it came from) that demonstrate \
the constants.

  - ## How postures differ (the differential)
    One paragraph (NOT one-per-cluster — a SINGLE paragraph) \
describing how the postures sit relative to each other along the \
varies_by_posture axes. Use the axes from the frontmatter to \
structure it. Example: "On register, technical sits formal-precise \
where personal-essay sits casual-intimate; on paragraph rhythm, both \
favour short paragraphs but technical breaks them with bulleted \
lists where personal-essay breaks them with single-sentence \
paragraphs that land like aphorisms."

  - ## How to pick the posture for a new piece
    1-2 paragraphs describing the decision criteria — audience, \
topic, intended reading-context, draft purpose. Be concrete: "if the \
piece is for veterans on Substack, default to <cluster>; if it's a \
systems writeup for engineers, default to <cluster>; the gray zone \
is X — when in doubt do Y."

  - ## Anti-patterns
    3-5 bullet points: things that would NEVER appear in any \
cluster, and would tip a reader off that a draft is NOT Andrew. \
Frame as "evidence of absence" — voice features common to other \
writers that are notably missing from every cluster (e.g. \
"corporate-stack openings like 'In today's fast-paced…' don't \
appear anywhere"; "tweet-style one-line paragraph chains don't \
appear, even in the casual cluster"). Concrete, falsifiable.

## When the clusters don't actually share invariants

If the cluster summaries genuinely diverge — no real always_true \
items emerge after honest comparison — return:

  status: no-overall-invariants
  no_overall_reason: "<one sentence on what's actually going on>"

with a body that says ``Andrew's clusters do not share a stable \
voice fingerprint. <reason>.`` and skips the constants section. \
Don't manufacture invariants from generic style-prose to fill the \
template — a thin "yes there are invariants" is worse than a clear \
"no there aren't, here's why" because the next ghostwriting call \
will trust the invariants and produce drift.

Output only the Markdown document.
"""


# ---------------------------------------------------------------------------
# Worker — async extraction
# ---------------------------------------------------------------------------


# Type alias for the operator-DM callable. Worker calls this on
# completion / failure. Callers in the daemon pass a closure that
# routes through PTB. The callable is async + returns nothing.
DmCallback = Callable[[int, str], Awaitable[None]]


async def run_worker(
    *,
    queue_path: Path,
    vault_path: Path,
    client: Any,
    model: str,
    scope: str,
    instance: str,
    poll_seconds: int,
    dm_callback: DmCallback | None,
    shutdown_event: asyncio.Event,
) -> None:
    """The async worker loop.

    Runs as an asyncio task started from the daemon lifecycle. Sleeps
    ``poll_seconds`` between ticks. On each tick: drains the queue,
    processes each job, DMs operator on completion / failure.

    Per ``feedback_intentionally_left_blank.md``: emits a periodic
    ``voice_train.worker.idle`` log when no jobs are pending, so an
    idle worker is distinguishable from a stuck one.

    Cancellation: the shutdown_event interrupts the sleep cycle.
    Mid-job cancellation is not a concern — jobs are short (one Opus
    call + one vault_create) and survive a daemon restart by virtue
    of being pulled off the queue (read-and-truncate model means a
    crashed worker loses in-flight jobs; operator re-runs the
    slash command — same failure model as queue truncation).
    """
    log.info(
        "voice_train.worker.starting",
        queue_path=str(queue_path),
        poll_seconds=poll_seconds,
        instance=instance,
    )
    idle_count = 0
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=poll_seconds,
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            jobs = drain_queue(queue_path)
        except Exception:  # noqa: BLE001
            log.exception("voice_train.worker.drain_failed")
            continue
        if not jobs:
            # Per intentionally-left-blank: emit an idle marker
            # periodically (every ~5 ticks) so the operator can grep
            # idle vs stuck. NOT every tick — that's log spam.
            idle_count += 1
            if idle_count % 5 == 0:
                log.info(
                    "voice_train.worker.idle",
                    ticks_since_last_job=idle_count,
                )
            continue
        idle_count = 0
        log.info("voice_train.worker.tick", job_count=len(jobs))
        for job in jobs:
            try:
                await _process_one_job(
                    job=job,
                    vault_path=vault_path,
                    client=client,
                    model=model,
                    scope=scope,
                    dm_callback=dm_callback,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "voice_train.worker.job_crashed",
                    job_id=job.job_id,
                    kind=job.kind,
                )
                # Best-effort failure marker on the raw record so the
                # operator can re-run.
                _mark_raw_failed(
                    vault_path, job.raw_rel_path, "worker_crash",
                )
                if dm_callback is not None:
                    try:
                        await dm_callback(
                            job.chat_id,
                            f"voice/method extraction crashed for "
                            f"{job.raw_name!r} — re-run the slash "
                            f"command to retry.",
                        )
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "voice_train.worker.dm_failed_after_crash",
                            chat_id=job.chat_id,
                        )


async def _process_one_job(
    *,
    job: ExtractionJob,
    vault_path: Path,
    client: Any,
    model: str,
    scope: str,
    dm_callback: DmCallback | None,
) -> None:
    """Process one extraction job end-to-end."""
    log.info(
        "voice_train.extraction_started",
        job_id=job.job_id,
        kind=job.kind,
        raw_rel_path=job.raw_rel_path,
        cluster=job.cluster,
    )
    if job.kind == "voice":
        result = await extract_voice_profile(
            client=client, model=model, raw_text=job.raw_body,
        )
    elif job.kind == "method":
        result = await extract_method_profile(
            client=client, model=model, raw_text=job.raw_body,
        )
    else:
        log.warning("voice_train.worker.unknown_kind", kind=job.kind)
        return

    if not result:
        _mark_raw_failed(
            vault_path, job.raw_rel_path, "llm_returned_empty",
        )
        log.warning(
            "voice_train.extraction_failed",
            job_id=job.job_id,
            kind=job.kind,
            reason="llm_returned_empty",
        )
        if dm_callback is not None and job.chat_id:
            try:
                await dm_callback(
                    job.chat_id,
                    f"{job.kind} extraction returned no content for "
                    f"{job.raw_name!r}. Re-run the slash command to retry.",
                )
            except Exception:  # noqa: BLE001
                log.exception("voice_train.worker.dm_failed")
        return

    # Parse the LLM output into frontmatter + body, then write the
    # structured record.
    try:
        write_result = _write_structured_record(
            vault_path=vault_path,
            kind=job.kind,
            raw_name=job.raw_name,
            raw_rel_path=job.raw_rel_path,
            cluster=job.cluster,
            llm_output=result,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "voice_train.extraction_failed",
            job_id=job.job_id,
            kind=job.kind,
            reason="vault_write",
            error=str(exc),
        )
        _mark_raw_failed(
            vault_path, job.raw_rel_path, f"vault_write: {exc}",
        )
        if dm_callback is not None and job.chat_id:
            try:
                await dm_callback(
                    job.chat_id,
                    f"{job.kind} extraction couldn't write the structured "
                    f"record for {job.raw_name!r}: {exc}. Re-run the "
                    f"slash command to retry.",
                )
            except Exception:  # noqa: BLE001
                log.exception("voice_train.worker.dm_failed")
        return

    # Flip raw record's extraction_status to "complete".
    _mark_raw_complete(vault_path, job.raw_rel_path)

    log.info(
        "voice_train.extraction_completed",
        job_id=job.job_id,
        kind=job.kind,
        structured_rel_path=write_result,
        cluster=job.cluster,
    )

    if dm_callback is not None and job.chat_id:
        try:
            await dm_callback(
                job.chat_id,
                f"{job.kind} profile ready: {write_result}",
            )
        except Exception:  # noqa: BLE001
            log.exception("voice_train.worker.dm_failed")

    # Trigger cluster-tier rebuild (voice only) when applicable.
    if job.kind == "voice" and job.cluster:
        try:
            await maybe_rebuild_cluster(
                vault_path=vault_path,
                client=client,
                model=model,
                scope=scope,
                cluster_name=job.cluster,
                dm_callback=dm_callback,
                chat_id=job.chat_id,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "voice_train.cluster_rebuild_crashed",
                cluster=job.cluster,
            )


def _mark_raw_failed(
    vault_path: Path, rel_path: str, reason: str,
) -> None:
    """Flip a raw record's ``extraction_status`` to ``failed``."""
    if not rel_path:
        return
    try:
        ops.vault_edit(
            vault_path,
            rel_path,
            set_fields={
                "extraction_status": "failed",
                "extraction_failure_reason": reason,
            },
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "voice_train.raw_record.mark_failed_failed",
            rel_path=rel_path,
        )


def _mark_raw_complete(vault_path: Path, rel_path: str) -> None:
    """Flip a raw record's ``extraction_status`` to ``complete``."""
    if not rel_path:
        return
    try:
        ops.vault_edit(
            vault_path,
            rel_path,
            set_fields={"extraction_status": "complete"},
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "voice_train.raw_record.mark_complete_failed",
            rel_path=rel_path,
        )


# ---------------------------------------------------------------------------
# Opus extraction calls
# ---------------------------------------------------------------------------


async def extract_voice_profile(
    *,
    client: Any,
    model: str,
    raw_text: str,
) -> str:
    """Run Opus to extract a voice profile from raw essay text.

    Returns the LLM's Markdown output (frontmatter + body). Empty
    string on any error — caller marks the raw record failed.
    """
    return await _call_opus(
        client=client,
        model=model,
        system_prompt=VOICE_EXTRACTION_PROMPT,
        user_message=f"Essay:\n---\n{raw_text}\n---\n\nProduce the structured voice profile.",
    )


async def extract_method_profile(
    *,
    client: Any,
    model: str,
    raw_text: str,
) -> str:
    """Run Opus to extract a method profile from raw source text."""
    return await _call_opus(
        client=client,
        model=model,
        system_prompt=METHOD_EXTRACTION_PROMPT,
        user_message=f"Source:\n---\n{raw_text}\n---\n\nProduce the structured method profile.",
    )


async def _call_opus(
    *,
    client: Any,
    model: str,
    system_prompt: str,
    user_message: str,
) -> str:
    """Make one Opus call. Returns the assembled text or empty string.

    Uses ``messages_create_kwargs`` so model-family quirks
    (temperature-strip on Opus 4.x, etc.) stay centralised per
    ``feedback_sdk_quirk_centralization.md``. Bumps max_tokens to
    8192 — voice and method profiles can run long.
    """
    try:
        response = await client.messages.create(**messages_create_kwargs(
            model=model,
            max_tokens=8192,
            temperature=0.3,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {"role": "user", "content": user_message},
            ],
        ))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "voice_train.opus_call_failed",
            error=str(exc),
            model=model,
        )
        return ""
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype == "text":
            text = getattr(block, "text", None) or (
                block.get("text") if isinstance(block, dict) else ""
            )
            if text:
                parts.append(str(text))
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Structured-record write
# ---------------------------------------------------------------------------


def _parse_llm_markdown(
    text: str,
) -> tuple[dict[str, Any], str]:
    """Parse Opus output into (frontmatter, body).

    The prompt instructs the model to return ``---`` frontmatter +
    Markdown body. We use python-frontmatter's loader on the raw
    string. On parse failure, falls back to ``({}, raw_text)`` so the
    record still lands with the prose body.
    """
    import frontmatter
    try:
        post = frontmatter.loads(text)
        return dict(post.metadata), post.content
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "voice_train.parse_llm_markdown_failed",
            error=str(exc),
        )
        return {}, text


def _write_structured_record(
    *,
    vault_path: Path,
    kind: Literal["voice", "method"],
    raw_name: str,
    raw_rel_path: str,
    cluster: str | None,
    llm_output: str,
    scope: str,
) -> str:
    """Write the structured record (voice or method) and return its path.

    Idempotency: if a record already exists at the target path, we
    use ``vault_edit body_replace`` instead of ``vault_create`` so
    re-extraction works. The record_type's body_replace allowlist
    must be opted in for the calling scope (see scope.py
    HYPATIA_CREATE_TYPES + the ``allow_body_replace`` matrix).
    """
    fm_extracted, body = _parse_llm_markdown(llm_output)

    record_type = "voice" if kind == "voice" else "method"
    fields: dict[str, Any] = {
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "source_record": f"[[{raw_rel_path}]]",
    }
    if cluster:
        fields["cluster"] = cluster
    # Status: the prompt-tuner's revisions added intentionally-left-
    # blank exits — when the input is too thin to extract a real
    # profile, the LLM emits ``status: insufficient-evidence`` (voice)
    # or ``status: not-a-method`` (method) so the empty-state signal
    # lands in vault rather than getting silently dropped behind a
    # default ``active``. Prefer the LLM-emitted status; default to
    # ``active`` only when absent. Both sentinels are pinned in
    # STATUS_BY_TYPE so vault_create's _validate_status accepts them.
    fields["status"] = str(fm_extracted.get("status") or "active")
    # Layer the LLM-extracted fields on top, EXCLUDING reserved keys
    # (``type``, ``name``, ``created`` are owned by the writer; status
    # was already taken above).
    for k, v in fm_extracted.items():
        if k in {"type", "name", "created", "status"}:
            continue
        fields[k] = v

    # Compute the target path. CRITICAL: ``vault_create`` uses ``raw_name``
    # VERBATIM as the filename (see ops.py ``vault_create`` →
    # ``rel_path = f"{directory}/{name}.md"``). The existence check MUST
    # use the same shape — using a slugged version (``slug_from_text``)
    # made the existence check look at ``voice/my-essay.md`` while
    # ``vault_create`` would actually write ``voice/My Essay.md``, so
    # re-extraction crashed with ``VaultError("File already exists")``
    # for any title where the slug differs from the name (apostrophes,
    # spaces, capitals — i.e. almost all real titles). Fixed by using
    # ``raw_name`` directly so check-and-write target the same path.
    target_dir = "voice" if kind == "voice" else "method"
    target_rel = f"{target_dir}/{raw_name}.md"
    target_full = vault_path / target_rel
    if target_full.exists():
        # Re-extraction path — body_replace via vault_edit. Per the
        # scope matrix, hypatia is allowed body_replace on voice +
        # method (see scope.py).
        ops.vault_edit(
            vault_path,
            target_rel,
            set_fields=fields,
            body_replace=body,
            scope=scope,
        )
        return target_rel

    result = ops.vault_create(
        vault_path,
        record_type,
        raw_name,
        set_fields=fields,
        body=body,
        scope=scope,
    )
    return result["path"]


# ---------------------------------------------------------------------------
# Cluster + overall builders
# ---------------------------------------------------------------------------


_CLUSTER_TIER_MIN_LEAVES = 2
_OVERALL_TIER_MIN_CLUSTERS = 2


def _list_voice_leaves_with_cluster(
    vault_path: Path, cluster_name: str,
) -> list[Path]:
    """Return voice/<slug>.md files whose frontmatter ``cluster`` matches.

    Walks ``voice/`` (NOT ``voice/cluster/``) and matches on the
    frontmatter ``cluster`` field. Returns an empty list when the
    voice/ directory doesn't exist yet.
    """
    voice_dir = vault_path / "voice"
    if not voice_dir.is_dir():
        return []
    import frontmatter
    matches: list[Path] = []
    for entry in voice_dir.iterdir():
        # Skip the cluster/ subdirectory and any non-md files.
        if entry.is_dir():
            continue
        if entry.suffix != ".md":
            continue
        try:
            post = frontmatter.load(str(entry))
        except Exception:  # noqa: BLE001
            continue
        if str(post.metadata.get("cluster") or "").strip() == cluster_name:
            matches.append(entry)
    return matches


def _list_voice_clusters(vault_path: Path) -> list[Path]:
    """Return voice/cluster/<slug>.md files."""
    cluster_dir = vault_path / "voice" / "cluster"
    if not cluster_dir.is_dir():
        return []
    return sorted(p for p in cluster_dir.iterdir() if p.suffix == ".md")


async def maybe_rebuild_cluster(
    *,
    vault_path: Path,
    client: Any,
    model: str,
    scope: str,
    cluster_name: str,
    dm_callback: DmCallback | None,
    chat_id: int,
) -> None:
    """If ≥2 leaves share ``cluster_name``, build / rebuild the cluster summary.

    Reads all leaf voice profiles with the cluster tag, runs Opus
    with VOICE_CLUSTER_PROMPT, writes ``voice/cluster/<name>.md``.

    Then maybe-rebuilds the overall profile (≥2 cluster summaries).
    """
    leaves = _list_voice_leaves_with_cluster(vault_path, cluster_name)
    if len(leaves) < _CLUSTER_TIER_MIN_LEAVES:
        log.info(
            "voice_train.cluster_rebuild.skipped",
            reason="below_threshold",
            cluster=cluster_name,
            leaf_count=len(leaves),
        )
        return

    leaf_texts: list[str] = []
    for leaf in leaves:
        try:
            leaf_texts.append(leaf.read_text(encoding="utf-8"))
        except OSError:
            continue
    user_msg = (
        f"Cluster name: {cluster_name}\n\n"
        f"Leaf count: {len(leaf_texts)}\n\n"
        f"=== Leaves ===\n\n"
        + "\n\n=== next leaf ===\n\n".join(leaf_texts)
        + "\n\n=== end ===\n\nProduce the cluster summary."
    )
    log.info(
        "voice_train.cluster_rebuild.started",
        cluster=cluster_name,
        leaf_count=len(leaves),
    )
    output = await _call_opus(
        client=client, model=model,
        system_prompt=VOICE_CLUSTER_PROMPT,
        user_message=user_msg,
    )
    if not output:
        log.warning(
            "voice_train.cluster_rebuild.failed",
            cluster=cluster_name, reason="llm_empty",
        )
        return

    fm_extracted, body = _parse_llm_markdown(output)
    fields: dict[str, Any] = {
        "cluster_name": cluster_name,
        "leaf_count": len(leaves),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    # Status: prefer the LLM-emitted status when present (so the
    # ``incoherent-cluster`` sentinel from the prompt's intentionally-
    # left-blank exit lands in vault). Default to ``active`` only when
    # the LLM didn't emit a status. ``type`` / ``name`` / ``created``
    # remain writer-owned.
    fields["status"] = str(fm_extracted.get("status") or "active")
    for k, v in fm_extracted.items():
        if k in {"type", "name", "created", "status"}:
            continue
        fields[k] = v

    # Path target uses ``cluster_name`` VERBATIM — same idempotency
    # contract as ``_write_structured_record`` above. ``vault_create``
    # writes ``voice/cluster/<cluster_name>.md``; the existence check
    # must look at the SAME path so re-extraction routes to body_replace
    # rather than crashing on "file already exists".
    target_rel = f"voice/cluster/{cluster_name}.md"
    target_full = vault_path / target_rel
    if target_full.exists():
        ops.vault_edit(
            vault_path, target_rel, set_fields=fields,
            body_replace=body, scope=scope,
        )
    else:
        ops.vault_create(
            vault_path, "voice-cluster", cluster_name,
            set_fields=fields, body=body, scope=scope,
        )

    log.info(
        "voice_train.cluster_rebuild.completed",
        cluster=cluster_name,
        target_rel=target_rel,
    )

    if dm_callback is not None and chat_id:
        try:
            await dm_callback(
                chat_id,
                f"voice cluster ready: {target_rel}",
            )
        except Exception:  # noqa: BLE001
            log.exception("voice_train.cluster_rebuild.dm_failed")

    # Maybe rebuild overall profile.
    try:
        await maybe_rebuild_overall(
            vault_path=vault_path, client=client, model=model,
            scope=scope, dm_callback=dm_callback, chat_id=chat_id,
        )
    except Exception:  # noqa: BLE001
        log.exception("voice_train.overall_rebuild.crashed")


async def maybe_rebuild_overall(
    *,
    vault_path: Path,
    client: Any,
    model: str,
    scope: str,
    dm_callback: DmCallback | None,
    chat_id: int,
) -> None:
    """If ≥2 cluster summaries exist, build / rebuild the overall profile."""
    clusters = _list_voice_clusters(vault_path)
    if len(clusters) < _OVERALL_TIER_MIN_CLUSTERS:
        log.info(
            "voice_train.overall_rebuild.skipped",
            reason="below_threshold",
            cluster_count=len(clusters),
        )
        return
    cluster_texts: list[str] = []
    for c in clusters:
        try:
            cluster_texts.append(c.read_text(encoding="utf-8"))
        except OSError:
            continue
    user_msg = (
        f"Cluster count: {len(cluster_texts)}\n\n"
        f"=== Clusters ===\n\n"
        + "\n\n=== next cluster ===\n\n".join(cluster_texts)
        + "\n\n=== end ===\n\nProduce the overall voice profile."
    )
    log.info(
        "voice_train.overall_rebuild.started",
        cluster_count=len(clusters),
    )
    output = await _call_opus(
        client=client, model=model,
        system_prompt=VOICE_OVERALL_PROMPT,
        user_message=user_msg,
    )
    if not output:
        log.warning(
            "voice_train.overall_rebuild.failed",
            reason="llm_empty",
        )
        return

    fm_extracted, body = _parse_llm_markdown(output)
    fields: dict[str, Any] = {
        "cluster_count": len(clusters),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "is_overall_profile": True,
    }
    # Status passthrough — same shape as cluster/leaf writers. The
    # ``no-overall-invariants`` sentinel from VOICE_OVERALL_PROMPT's
    # intentionally-left-blank exit lands in vault. ``Andrew Voice
    # Profile.md`` is a fixed string so no idempotency-path concern
    # here; existence check + write target are byte-identical.
    fields["status"] = str(fm_extracted.get("status") or "active")
    for k, v in fm_extracted.items():
        if k in {"type", "name", "created", "status"}:
            continue
        fields[k] = v

    target_rel = "voice/Andrew Voice Profile.md"
    target_full = vault_path / target_rel
    if target_full.exists():
        ops.vault_edit(
            vault_path, target_rel, set_fields=fields,
            body_replace=body, scope=scope,
        )
    else:
        ops.vault_create(
            vault_path, "voice", "Andrew Voice Profile",
            set_fields=fields, body=body, scope=scope,
        )
    log.info(
        "voice_train.overall_rebuild.completed",
        target_rel=target_rel,
    )
    if dm_callback is not None and chat_id:
        try:
            await dm_callback(
                chat_id,
                f"overall voice profile ready: {target_rel}",
            )
        except Exception:  # noqa: BLE001
            log.exception("voice_train.overall_rebuild.dm_failed")


# ---------------------------------------------------------------------------
# New-job factory (used by the slash-command handlers in bot.py)
# ---------------------------------------------------------------------------


def make_job(
    *,
    kind: Literal["voice", "method"],
    raw_rel_path: str,
    raw_name: str,
    raw_body: str,
    cluster: str | None = None,
    image_metadata: list[dict[str, Any]] | None = None,
    chat_id: int = 0,
    instance: str = "",
) -> ExtractionJob:
    """Build an :class:`ExtractionJob` with a fresh job_id + timestamp."""
    return ExtractionJob(
        job_id=str(uuid.uuid4()),
        kind=kind,
        raw_rel_path=raw_rel_path,
        raw_name=raw_name,
        raw_body=raw_body,
        cluster=cluster,
        image_metadata=list(image_metadata or []),
        chat_id=chat_id,
        enqueued_at=datetime.now(timezone.utc).isoformat(),
        instance=instance,
    )


# ---------------------------------------------------------------------------
# Slash-command argument parsing
# ---------------------------------------------------------------------------


def parse_train_args(
    raw_text: str, ctx_args: list[str] | None,
) -> tuple[str | None, str]:
    """Parse ``/train [--cluster <name>] [<text>]`` into (cluster, body).

    The slash prefix is already consumed by PTB. ``ctx.args`` is the
    list of whitespace-split tokens after the command. We support:

      /train                           → (None, "")
      /train some essay text...        → (None, "some essay text...")
      /train --cluster veteran         → ("veteran", "")
      /train --cluster veteran text..  → ("veteran", "text..")

    Empty body signals "use most-recent paste" — caller resolves that
    against the conversation transcript.
    """
    if not ctx_args:
        return None, ""
    cluster: str | None = None
    rest_tokens: list[str] = list(ctx_args)
    if rest_tokens and rest_tokens[0] == "--cluster":
        if len(rest_tokens) >= 2:
            cluster = rest_tokens[1].strip() or None
            rest_tokens = rest_tokens[2:]
        else:
            # ``/train --cluster`` with nothing after — treat as malformed.
            return None, ""
    body = " ".join(rest_tokens).strip()
    # PTB's whitespace-split eats long pastes; for newline-rich content
    # we want the raw_text post-slash. Strip the leading "/train" plus
    # optional --cluster header if present.
    if body and "\n" in raw_text:
        # Re-extract from the raw text for newline preservation.
        rebuilt = _strip_command_prefix(
            raw_text, command="train", cluster=cluster,
        )
        if rebuilt:
            body = rebuilt
    return cluster, body


def parse_method_source_args(
    raw_text: str, ctx_args: list[str] | None,
) -> str:
    """Parse ``/method-source [<text>]`` into body. Empty = use recent paste.

    Note on the command name: PTB rejects ``-`` in CommandHandler names
    (must be ``[a-z0-9_]``), so the bot registers ``CommandHandler(
    "method_source", on_method_source)``. That means real Telegram
    deliveries arrive as ``/method_source ...`` (underscore), NOT
    ``/method-source``. ``_strip_command_prefix`` matches against the
    raw text it receives, so the command name passed must reflect the
    delivered form. Earlier code passed ``"method-source"`` which
    silently failed to match — multi-line bodies fell back to the
    whitespace-joined ``ctx.args`` shape and lost paragraph breaks.
    Underscore form fixes the regex to match what PTB delivers.
    """
    if not ctx_args:
        return ""
    body = " ".join(ctx_args).strip()
    if body and "\n" in raw_text:
        rebuilt = _strip_command_prefix(
            raw_text, command="method_source", cluster=None,
        )
        if rebuilt:
            body = rebuilt
    return body


def _strip_command_prefix(
    raw_text: str, *, command: str, cluster: str | None,
) -> str:
    """Strip ``/command [@bot] [--cluster X]`` from the head of ``raw_text``.

    Used to preserve newlines / formatting when the operator pastes a
    multi-line essay directly after the slash command. PTB's
    ``ctx.args`` whitespace-splits, which destroys paragraph breaks.
    """
    # Find the first non-whitespace block that's the command.
    # Telegram bots may receive ``/train@HypatiaErrantBot ...`` in
    # group chats; handle that too.
    pattern = rf"^\s*/{re.escape(command)}(?:@[\w_]+)?\b"
    match = re.match(pattern, raw_text)
    if not match:
        return ""
    after = raw_text[match.end():]
    # Strip optional ``--cluster <name>`` header.
    if cluster:
        cluster_pattern = rf"^\s*--cluster\s+{re.escape(cluster)}\b"
        cm = re.match(cluster_pattern, after)
        if cm:
            after = after[cm.end():]
    return after.lstrip("\n").lstrip(" \t").rstrip()


__all__ = [
    "DmCallback",
    "ExtractionJob",
    "SaveRawResult",
    "VOICE_EXTRACTION_PROMPT",
    "METHOD_EXTRACTION_PROMPT",
    "VOICE_CLUSTER_PROMPT",
    "VOICE_OVERALL_PROMPT",
    "drain_queue",
    "enqueue_job",
    "extract_method_profile",
    "extract_voice_profile",
    "find_most_recent_user_paste",
    "make_job",
    "maybe_rebuild_cluster",
    "maybe_rebuild_overall",
    "parse_method_source_args",
    "parse_train_args",
    "run_worker",
    "save_raw_essay",
    "save_raw_source",
    "slug_from_text",
    "title_from_text",
]
