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
# Substack-export pastes start with a YAML frontmatter block delimited
# by ``---`` lines. The original first-non-empty-line slug derivation
# would happily consume ``---`` as the title, producing
# ``document/essay/---.md`` (Bug #57, 2026-05-08). This regex matches
# a leading ``---\n…\n---\n`` block (CRLF-tolerant, optional trailing
# whitespace) so we can parse + strip it before the title-line scan.
_LEADING_FRONTMATTER_RE = re.compile(
    r"^\s*---\s*\r?\n(?P<body>.*?)\r?\n---\s*\r?\n",
    re.DOTALL,
)


def _split_leading_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Detect + parse a leading YAML frontmatter block.

    Returns ``(metadata_dict, remaining_text)``. When no frontmatter is
    present (or parsing fails), returns ``({}, text)`` so the caller
    falls back to the existing first-non-empty-line behaviour.

    Used by both :func:`slug_from_text` and :func:`title_from_text` so
    the two derivations agree on what counts as the "title source" —
    if they disagree, the slug variable in logs diverges from the
    actual filename written by ``vault_create`` (Bug #57's
    "untitled" log vs ``---.md`` filename divergence signature).
    """
    if not isinstance(text, str):
        return {}, ""
    match = _LEADING_FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    fm_body = match.group("body")
    rest = text[match.end():]
    try:
        import yaml
        loaded = yaml.safe_load(fm_body)
    except Exception:  # noqa: BLE001
        return {}, rest
    if isinstance(loaded, dict):
        return loaded, rest
    return {}, rest


def _is_yaml_marker(line: str) -> bool:
    """True if ``line`` is a bare ``---`` (YAML doc separator).

    Defensive guard against ever returning ``---`` as a title or slug.
    Used when the frontmatter parser fails or when somebody pastes a
    ``---``-only line in an unexpected position.
    """
    return line.strip() == "---"


def slug_from_text(text: str) -> str:
    """Derive a filename-safe slug from a longer text or title.

    Resolution order:

      1. Detect a leading YAML frontmatter block (Substack-export
         shape). If present and parseable, prefer the ``title:`` field.
      2. Otherwise, scan post-frontmatter (or original) text for the
         first non-empty line. Strip leading Markdown heading markers
         (``# Title`` / ``## Title``). Skip any bare ``---`` lines
         (defensive — never produce ``---`` as a slug).

    NFKD-normalize + strip combining marks (so ``café`` → ``cafe``),
    lowercase, collapse whitespace to hyphens, drop non-alphanumerics,
    collapse runs of hyphens, strip edges, cap at 80 chars.

    Empty / all-punctuation input returns ``"untitled"`` so the caller
    always lands at a real path.
    """
    if not isinstance(text, str):
        return _DEFAULT_SLUG
    title_line = _resolve_title_line(text)
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


def _resolve_title_line(text: str) -> str:
    """Shared title-source resolver for slug + title derivation.

    Resolution order (matches :func:`slug_from_text` docstring):

      1. Leading YAML frontmatter ``title:`` field (Substack export).
      2. First non-empty line of post-frontmatter (or original) text,
         with leading Markdown heading markers stripped.
      3. Defensive guard: never return a bare ``---`` line — keep
         scanning past it. Bug #57's load-bearing fix.

    Returns an empty string when no title source is found; callers
    apply their own fallback (``"untitled"`` for slug,
    ``"Untitled — <today>"`` for title).

    Centralised so :func:`slug_from_text` and :func:`title_from_text`
    cannot drift — the divergence between the two was the surface
    signature of Bug #57 (slug var said ``"untitled"``, filename
    written said ``---.md``).
    """
    metadata, rest = _split_leading_frontmatter(text)
    fm_title = metadata.get("title") if isinstance(metadata, dict) else None
    if isinstance(fm_title, str) and fm_title.strip():
        # The frontmatter title may be quoted in the YAML source;
        # ``yaml.safe_load`` already strips outer quotes.
        return fm_title.strip()
    # Fall through: scan post-frontmatter text (which may equal
    # original ``text`` when no frontmatter was present).
    for line in rest.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _is_yaml_marker(stripped):
            # Defensive: a stray ``---`` from a malformed frontmatter
            # block must not become the title. Keep scanning.
            continue
        m = re.match(r"^#+\s*(.+)$", stripped)
        return m.group(1).strip() if m else stripped
    return ""


def title_from_text(text: str) -> str:
    """Derive a human-readable title from input text.

    Same resolution order as :func:`slug_from_text` — leading YAML
    frontmatter ``title:`` field, then first non-empty line with
    Markdown heading markers stripped, with bare ``---`` lines skipped
    defensively. Falls back to a date-stamped placeholder when no
    title source resolves.

    Distinct from ``slug_from_text`` because ``vault_create`` wants
    BOTH (the ``name`` arg is human-readable; the path is slugged
    separately). The two helpers share :func:`_resolve_title_line` so
    the slug and the filename can never diverge — the divergence
    between them was Bug #57's surface signature.
    """
    if not isinstance(text, str):
        return f"Untitled — {_date.today().isoformat()}"
    title = _resolve_title_line(text)
    if not title:
        return f"Untitled — {_date.today().isoformat()}"
    # Cap at a reasonable length to keep filenames manageable.
    # Filesystems handle long names fine but operators don't read
    # 200-char filenames.
    if len(title) > 100:
        title = title[:100].rstrip()
    return title


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
#
# The three voice prompts (extraction / cluster / overall) are externalised
# under ``src/alfred/_bundled/skills/vault-hypatia/prompts/`` so prompt-tuner
# can iterate on them without touching this Python module. Loaded fresh per
# call (no module-import caching) — edits to the .md files take effect on
# the NEXT extraction without needing a daemon restart.
#
# ``METHOD_EXTRACTION_PROMPT`` stays inline below; it is intentionally not
# part of this externalisation pass.


def _load_voice_prompt(prompt_file: str) -> str:
    """Load a voice prompt from the bundled vault-hypatia/prompts/ directory.

    Mirrors ``alfred.distiller.pipeline._load_stage_prompt``: importlib.resources
    locator, fresh read per call, warning + empty string on missing file.
    """
    from alfred._data import get_skills_dir

    prompt_path = (
        get_skills_dir() / "vault-hypatia" / "prompts" / prompt_file
    )
    if not prompt_path.exists():
        log.warning(
            "voice_train.prompt_not_found",
            path=str(prompt_path),
            prompt_file=prompt_file,
            stdout_tail="",
        )
        return ""
    return prompt_path.read_text(encoding="utf-8")


def get_voice_extraction_prompt() -> str:
    """Return the leaf voice extraction prompt (read fresh per call)."""
    return _load_voice_prompt("voice_extraction.md")


def get_voice_cluster_prompt() -> str:
    """Return the cluster-tier voice synthesis prompt (read fresh per call)."""
    return _load_voice_prompt("voice_cluster.md")


def get_voice_overall_prompt() -> str:
    """Return the overall-tier voice synthesis prompt (read fresh per call)."""
    return _load_voice_prompt("voice_overall.md")


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
                # Skip DM when chat_id=0 (backfill-initiated job) — Ticket
                # #79 (2026-05-07). Matches the guard pattern at the
                # other three DM sites (lines ~1193, 1227, 1250). Without
                # this guard, a crashed backfill job tries to send to
                # chat_id=0, the API raises, and the inner except logs a
                # confusing voice_train.worker.dm_failed_after_crash.
                if dm_callback is not None and job.chat_id:
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
        system_prompt=get_voice_extraction_prompt(),
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
    with the voice-cluster prompt (``get_voice_cluster_prompt()``),
    writes ``voice/cluster/<name>.md``.

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
        system_prompt=get_voice_cluster_prompt(),
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
        system_prompt=get_voice_overall_prompt(),
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
    # ``no-overall-invariants`` sentinel from the voice-overall prompt's
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
# Ticket #59 (2026-05-08) — backfill helper for un-extracted raw records
# ---------------------------------------------------------------------------
#
# Recovery path for raw essay/source records that landed at the right
# vault path but never went through the extraction worker. Cases:
#
#   - /train partially succeeded (raw saved, enqueue failed silently)
#   - operator created an essay record outside the slash command
#   - operator/team-lead patched essay content directly (no slash
#     command involved → no enqueue)
#   - extraction failed earlier (extraction_status: failed) and the
#     operator wants to retry after fixing the cause
#
# The original pre-CLI workaround was a one-off Python script that
# wrote jobs straight to ``extraction_queue.jsonl``. This helper turns
# that into a proper, scope-respecting CLI surface that:
#
#   1. Walks ``document/essay/`` + ``source/`` directories.
#   2. Skips records that are already extracted (``extraction_status:
#      complete`` AND the structured companion file exists).
#   3. Builds an :class:`ExtractionJob` for each remaining record.
#   4. Returns the (jobs, skip_count) tuple so the caller (CLI) can
#      decide whether to dry-run or actually write to the queue.
#
# The helper does NOT touch the queue file itself — that's the
# caller's responsibility (CLI uses :func:`enqueue_job` per-job, same
# as the bot path).


def _read_raw_record(
    path: Path,
) -> tuple[dict[str, Any], str] | None:
    """Read a raw record file, returning (frontmatter, body) or None on error.

    Uses the ``frontmatter`` library so YAML parsing matches the rest
    of the codebase (curator, distiller, brief). Returns ``None`` when
    the file can't be read or parsed — the caller logs + skips so a
    single corrupt file doesn't abort the whole backfill.
    """
    try:
        import frontmatter
    except ImportError:  # pragma: no cover — frontmatter is a base dep
        return None
    try:
        post = frontmatter.load(path)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "voice_train.backfill.read_failed",
            path=str(path),
            error=str(exc),
        )
        return None
    return dict(post.metadata or {}), post.content or ""


def _structured_companion_exists(
    *, vault_path: Path, kind: Literal["voice", "method"], raw_name: str,
) -> bool:
    """True iff the structured companion file for this raw record exists.

    The structured-record writer uses the raw record's ``name`` (not
    a slug) as the filename — see :func:`_write_structured_record`'s
    "CRITICAL" comment. The backfill must mirror that lookup shape so
    it correctly detects already-extracted records and doesn't
    re-enqueue them.
    """
    target_dir = "voice" if kind == "voice" else "method"
    companion = vault_path / target_dir / f"{raw_name}.md"
    return companion.exists()


def collect_backfill_jobs(
    *,
    vault_path: Path,
    instance: str,
) -> tuple[list[ExtractionJob], int, int]:
    """Walk the vault and build extraction jobs for un-extracted raw records.

    Returns ``(jobs, skipped_voice, skipped_method)``:

      * ``jobs`` — list of :class:`ExtractionJob` ready to enqueue.
      * ``skipped_voice`` / ``skipped_method`` — count of raw records
        that were skipped because their structured companion already
        exists AND ``extraction_status: complete``.

    Skip rule (per Ticket #59 brief):
      * Skip iff ``extraction_status == "complete"`` AND the structured
        companion exists. Either condition alone is insufficient — a
        ``complete`` status without companion file means the operator
        deleted the structured record manually and wants it rebuilt.
      * Records with ``extraction_status: failed`` ARE re-enqueued (this
        is the recovery path after fixing the failure cause).

    The helper is pure (reads vault, returns data) so it tests
    cleanly without touching the JSONL queue. The CLI decides whether
    to ``enqueue_job`` for each returned job or just print them
    (``--dry-run``).

    ``instance`` is stamped on every produced job so the worker /
    audit logs show which instance owns the backfilled work.
    """
    jobs: list[ExtractionJob] = []
    skipped_voice = 0
    skipped_method = 0

    # Voice (essay → voice profile).
    essay_dir = vault_path / "document" / "essay"
    if essay_dir.is_dir():
        for path in sorted(essay_dir.glob("*.md")):
            parsed = _read_raw_record(path)
            if parsed is None:
                continue
            metadata, body = parsed
            raw_name = path.stem  # the filename IS the name (vault_create
            # uses ``raw_name`` verbatim per _write_structured_record).
            extraction_status = str(metadata.get("extraction_status") or "")
            companion_exists = _structured_companion_exists(
                vault_path=vault_path, kind="voice", raw_name=raw_name,
            )
            if extraction_status == "complete" and companion_exists:
                skipped_voice += 1
                continue
            cluster_val = metadata.get("cluster")
            cluster_str = str(cluster_val) if cluster_val else None
            jobs.append(make_job(
                kind="voice",
                raw_rel_path=f"document/essay/{path.name}",
                raw_name=raw_name,
                raw_body=body,
                cluster=cluster_str,
                chat_id=0,  # No DM target — operator-initiated
                              # backfill, not a slash-command flush.
                instance=instance,
            ))

    # Method (source → method profile).
    source_dir = vault_path / "source"
    if source_dir.is_dir():
        for path in sorted(source_dir.glob("*.md")):
            parsed = _read_raw_record(path)
            if parsed is None:
                continue
            metadata, body = parsed
            raw_name = path.stem
            extraction_status = str(metadata.get("extraction_status") or "")
            companion_exists = _structured_companion_exists(
                vault_path=vault_path, kind="method", raw_name=raw_name,
            )
            if extraction_status == "complete" and companion_exists:
                skipped_method += 1
                continue
            jobs.append(make_job(
                kind="method",
                raw_rel_path=f"source/{path.name}",
                raw_name=raw_name,
                raw_body=body,
                chat_id=0,
                instance=instance,
            ))

    log.info(
        "voice_train.backfill.collected",
        voice_jobs=sum(1 for j in jobs if j.kind == "voice"),
        method_jobs=sum(1 for j in jobs if j.kind == "method"),
        skipped_voice=skipped_voice,
        skipped_method=skipped_method,
    )
    return jobs, skipped_voice, skipped_method


# ---------------------------------------------------------------------------
# Slash-command argument parsing
# ---------------------------------------------------------------------------


# Ticket #67 (2026-05-07) — iOS auto-corrects ``--`` to ``—`` (em-dash,
# U+2014) on paste, and some clients emit ``–`` (en-dash, U+2013). When
# the operator types ``/train --cluster personal`` on iOS, the bot
# receives ``/train —cluster personal``; the strict ``--cluster`` token
# match in :func:`parse_train_args` then misses, the en/em-dash token
# falls into the body, and the slug derivation builds a broken filename
# from ``—cluster personal``. Normalize the flag-prefix variants to the
# canonical ASCII form once at the top of the parse path so downstream
# token-comparison stays simple. Only the leading dashes of the
# flag-marker are normalized — em/en-dashes inside essay BODY text
# (after the flag) are preserved as-is.
#
# Ticket #74 (2026-05-07) — tightened to a flag-pattern allowlist so
# that body text starting with an em-dash-led token (``/train —Some
# opening line``) is NOT silently re-flowed into ``--Some opening
# line``. The original implementation normalized any leading dash-run
# followed by a letter, which caused operator-pasted essays opening
# with an em-dash phrase to lose their leading typography. Now we only
# normalize when the token matches a known flag-name shape
# (``cluster`` today; future flags get added to ``_KNOWN_FLAG_NAMES``).
# Anything else passes through unchanged so essay body em-dashes are
# preserved verbatim — matching the docstring contract on the parse
# helpers below.

# Known --flag names that the dash-normalizer is allowed to fire on.
# Add new flags here when adding /train (or /method-source) flags so
# the iOS-safe normalization extends to them too.
_KNOWN_FLAG_NAMES: tuple[str, ...] = ("cluster",)


def _normalize_flag_prefix_dashes(token: str) -> str:
    """Convert leading em/en-dash variants to ``--`` for KNOWN flag names.

    Idempotent + cheap. Applied only to the FIRST flag token (i.e. the
    one that should be ``--cluster``); body text passes through unchanged.

    Ticket #74 (2026-05-07): only fires when the post-dash token matches
    a name in ``_KNOWN_FLAG_NAMES`` (currently just ``cluster``). A
    naked em-dash followed by arbitrary text — e.g. ``—Some opening
    line`` from an operator pasting an em-dash-led essay — passes
    through unchanged. Without this allowlist guard the normalizer
    would silently rewrite the essay's leading em-dash to ASCII
    double-hyphen and the body would be saved as ``--Some opening
    line``.
    """
    if not token:
        return token
    # Allowlist match: leading dash-run + one of the known flag names +
    # word boundary. Word boundary matters so ``—clustertypo`` (a typo,
    # not the cluster flag) doesn't get rewritten either. Matches:
    # ``—cluster``, ``–cluster``, ``-cluster``, ``--cluster`` (the
    # canonical ASCII form is also accepted so downstream comparison
    # stays simple even when no normalization is needed).
    flag_alt = "|".join(re.escape(name) for name in _KNOWN_FLAG_NAMES)
    pattern = rf"^[–—\-]+(?P<flag>(?:{flag_alt}))\b"
    match = re.match(pattern, token)
    if not match:
        return token
    # Reconstruct as ``--<flag>`` + whatever followed the matched
    # flag-name in the original token. Preserves any trailing text on
    # the same token (rare — usually the flag value is a separate
    # token — but cheap to keep).
    suffix = token[match.end():]
    return f"--{match.group('flag')}{suffix}"


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

    iOS / smart-quote variants of ``--`` (em-dash ``—`` U+2014, en-dash
    ``–`` U+2013) at the leading flag-token position are normalized to
    ``--`` BEFORE flag detection (Ticket #67). Body text after the flag
    keeps its original Unicode dashes.

    Empty body signals "use most-recent paste" — caller resolves that
    against the conversation transcript.
    """
    if not ctx_args:
        return None, ""
    cluster: str | None = None
    rest_tokens: list[str] = list(ctx_args)
    # Normalize ONLY the first token's leading dashes — the candidate
    # flag marker. Body tokens pass through unchanged so essays that
    # happen to start with an em-dashed phrase keep their formatting.
    if rest_tokens:
        rest_tokens[0] = _normalize_flag_prefix_dashes(rest_tokens[0])
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

    /method-source has no flags today, but Ticket #67's en/em-dash
    normalization is applied to the first token defensively — a future
    ``--<flag>`` extension on this path inherits the same iOS-safe
    parsing without re-derivation.
    """
    if not ctx_args:
        return ""
    rest_tokens: list[str] = list(ctx_args)
    if rest_tokens:
        rest_tokens[0] = _normalize_flag_prefix_dashes(rest_tokens[0])
    body = " ".join(rest_tokens).strip()
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

    Ticket #67 (2026-05-07): the cluster-header regex accepts the
    em-dash (``—``) and en-dash (``–``) variants of ``--`` so iOS
    auto-corrected pastes of multi-line essays still strip the flag
    cleanly. Without this, ``parse_train_args`` would correctly parse
    the cluster from ctx_args (token-level normalization) but then
    fail to strip the flag header from the multi-line body, leaving
    ``—cluster personal`` glued to the front of the saved essay.
    """
    # Find the first non-whitespace block that's the command.
    # Telegram bots may receive ``/train@HypatiaErrantBot ...`` in
    # group chats; handle that too.
    pattern = rf"^\s*/{re.escape(command)}(?:@[\w_]+)?\b"
    match = re.match(pattern, raw_text)
    if not match:
        return ""
    after = raw_text[match.end():]
    # Strip optional ``--cluster <name>`` header. Accept ASCII
    # double-hyphen + Unicode em/en-dash variants (Ticket #67).
    if cluster:
        cluster_pattern = (
            rf"^\s*(?:--|—|–)cluster\s+{re.escape(cluster)}\b"
        )
        cm = re.match(cluster_pattern, after)
        if cm:
            after = after[cm.end():]
    return after.lstrip("\n").lstrip(" \t").rstrip()


# ---------------------------------------------------------------------------
# Multi-message paste buffer (Bug #58 — Telegram 4096-char chunking)
# ---------------------------------------------------------------------------
#
# Telegram caps each message at ~4096 chars. Long Substack essays
# (5000-15000 chars) get split into 2-4 chunks by the client. Only the
# FIRST chunk carries the ``/train`` prefix; subsequent chunks land
# as plain text and fall through to Hypatia's natural-language path,
# producing a TRUNCATED voice profile and a contaminated conversation
# transcript. (Andrew, 2026-05-08: pasted 3 essays, each chunked into
# 2-3 messages; voice extraction returned ``closing_style: "Incomplete
# — essay cuts off mid-sentence at 'I was'"``.)
#
# Fix: the bot-layer slash handler opens a per-chat-id buffer when it
# fires. Subsequent plain-text messages within ``debounce_seconds``
# append to the buffer and reset the timer. The flush callback runs
# the existing save_raw + enqueue path on the FULL accumulated text.
#
# Flush triggers (any one fires):
#   1. Silence past ``debounce_seconds`` (default 5s) — the common
#      case; user finishes pasting.
#   2. ``max_buffer_seconds`` ceiling (default 60s) — safety stop so
#      a buffer can't sit open indefinitely if the operator wanders
#      off mid-paste with no closing silence.
#   3. Operator sends another command — the on_train / on_method_source
#      handler closes the prior buffer before opening a new one.

# Default debounce / ceiling. Both are config-overridable via
# ``VoiceTrainConfig.debounce_seconds`` / ``VoiceTrainConfig.max_buffer_seconds``.
#
# Ticket #70 (2026-05-07) bumped the default debounce from 5s → 10s
# after Telegram client auto-split inter-chunk delays were observed at
# 7-12s in the wild. 10s captures the long-tail at the cost of slower
# ack on single-message /train. Companion mitigations:
#   - end-marker detection in :func:`buffer_has_end_marker` flushes
#     complete-essay pastes immediately on the next idle tick;
#   - rapid-arrival detection at the bot layer treats sub-3s
#     follow-ups as continuation regardless of debounce expiry.
_DEFAULT_DEBOUNCE_SECONDS = 10
_DEFAULT_MAX_BUFFER_SECONDS = 60


# Ticket #70 — Substack-export pattern end-of-essay markers.
#
# When the assembled buffer contains BOTH the head of a YAML/Markdown
# essay and a recognized closing marker, we know the operator pasted a
# complete essay (the chunker hit the end of the source). Flush
# immediately on the next idle tick rather than waiting the full
# debounce window — saves ~10s per essay paste. Markers are checked
# in priority order so the most-distinctive one (footnote-tail) wins
# when multiple are present. Match is case-sensitive on purpose:
# Substack-exported text preserves the original casing.
_VOICE_TRAIN_END_MARKERS: tuple[str, ...] = (
    # Substack footnote-tail. The first footnote definition opens with
    # ``\n[^1]: `` — a near-perfect end-of-body signal.
    "\n[^1]: ",
    # Substack closing block emitted by the export-to-clipboard flow.
    "\nSubscribed\n",
    # Recurring sign-off across Andrew's published voice corpus.
    "Would you like to know more?",
    # Author bio block opener — comes after the body but before the
    # subscriber CTA in the export.
    "I write about ",
)


def buffer_has_end_marker(text: str) -> bool:
    """Return True when ``text`` contains a Substack-export end marker.

    Used by :func:`bot._schedule_voice_train_flush` to decide whether
    to flush early on the next debounce tick.

    Two-gate detection:

      1. **Minimum-body gate** — the marker must appear AFTER at least
         200 chars of body content. Stops a chat that happens to
         mention ``"Subscribed"`` two messages in from false-positive
         flushing an in-progress paste.
      2. **End-anchor gate (Ticket #73, 2026-05-07)** — the marker must
         appear in the LAST 500 chars of the buffer (or the last 25%,
         whichever is larger). End-of-essay markers are by definition a
         tail signal — anchoring detection to end-of-content position
         stops mid-body false-positives where a sign-off phrase like
         ``"Would you like to know more?"`` appears in the middle of an
         essay (rhetorical question, embedded quote, etc.) and races
         ahead of the actual closing chunk.

    The "last 25%" alternative is structural: long essays (8-15k chars)
    naturally have proportionally more closing-block real estate and
    Substack's exact closing-block placement varies with footnote
    count. ``max(500, len(text)//4)`` keeps the threshold conservative
    on short pastes and proportional on long ones.
    """
    if not text:
        return False
    text_len = len(text)
    # End-anchor window: last 500 chars or last 25%, whichever is larger.
    anchor_window = max(500, text_len // 4)
    anchor_threshold = text_len - anchor_window
    if anchor_threshold < 0:
        anchor_threshold = 0
    for marker in _VOICE_TRAIN_END_MARKERS:
        # Use rfind() so the LAST occurrence wins (Ticket #78, 2026-05-07).
        # find() would return the first occurrence; if a marker appears
        # both mid-body (false positive) AND at the actual end, find()
        # returns the early position and the end-anchor gate rejects the
        # valid late occurrence. rfind() is semantically correct for an
        # end-anchor detector.
        idx = text.rfind(marker)
        if idx < 0:
            continue
        # Minimum-body gate (200-char floor) AND end-anchor gate.
        if idx >= 200 and idx >= anchor_threshold:
            return True
    return False


@dataclass
class PendingPaste:
    """One open paste buffer for a single chat_id.

    Held in the bot's ``bot_data`` dict (keyed by chat_id) so the
    on_train / on_method_source / on_text handlers all see the same
    state. Single-event-loop concurrency: PTB runs all handlers in
    one asyncio loop, so the dict access doesn't need locking.
    """

    chat_id: int
    kind: Literal["voice", "method"]
    cluster: str | None
    chunks: list[str] = field(default_factory=list)
    image_metadata: list[dict[str, Any]] = field(default_factory=list)
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Ticket #70 — wall-clock of the most recent chunk append. Updated
    # by :func:`append_paste_chunk`. Currently observability-only; the
    # rapid-arrival continuation heuristic is implicit in the
    # debounce-reset on every append (each append cancels the prior
    # flush task). Kept on the dataclass for future use + log
    # correlation when triaging buffer-timing incidents.
    last_chunk_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # The asyncio.TimerHandle (or Task) currently scheduled to flush.
    # Cancelled + replaced on each new chunk so the debounce window
    # restarts. ``None`` while a flush is actively running.
    flush_task: Any = None
    # Tracks whether the flush has fired (so a late text append after
    # flush starts doesn't double-process). Set to True at the top of
    # the flush callback.
    flushed: bool = False

    def assembled_text(self) -> str:
        """Join chunks with double-newline (paragraph) separators.

        Telegram's chunking sometimes splits on a paragraph boundary
        and sometimes mid-line; ``\n\n`` is the safest re-join because
        it preserves the visual break between chunks without
        introducing artifacts when the chunker happened to break
        cleanly between paragraphs (the duplicated newlines collapse
        to a single paragraph break in any sensible Markdown reader).

        EDGE-CASE / KNOWN ASSUMPTION (Ticket #65 doc): the join is
        position-blind — it does NOT inspect chunk content for
        structural markers. If Telegram's chunker happens to split a
        message INSIDE a YAML frontmatter block (the ``---`` delimited
        head used by Substack pastes), the assembled text will contain
        a stray ``\n\n`` mid-frontmatter, which any YAML parser will
        choke on (the frontmatter delimiter requires the closing
        ``---`` to follow the key/value lines without a paragraph
        break). Observed failure mode: ``slug_from_text`` reads
        garbled YAML, falls back to the H1 / first-line heuristic,
        and the saved record gets an unexpected slug.

        Mitigation when this surfaces: detect the ``---`` head in the
        FIRST chunk, then re-join differently when the closing ``---``
        lands in a later chunk (e.g. join chunks 1..N with single
        newlines until we see the closing fence, then double-newline
        afterwards). Not implemented today — we have not observed
        Telegram chunking inside frontmatter in the wild as of
        2026-05-08, and the slug-fallback path is reasonable when it
        does happen. Future builders: this comment is the breadcrumb.
        """
        return "\n\n".join(c.strip() for c in self.chunks if c.strip())


def append_paste_chunk(
    pending: PendingPaste, chunk: str,
) -> None:
    """Append a non-empty chunk to a pending buffer.

    Idempotent on empty chunks (drops them). Caller is responsible
    for resetting the debounce timer after this returns — kept
    separate so test code can drive append + flush deterministically
    without scheduling.

    Updates ``pending.last_chunk_at`` on every successful append
    (Ticket #70) so the bot-layer scheduler can detect rapid-arrival
    bursts.
    """
    text = (chunk or "").strip()
    if not text:
        return
    pending.chunks.append(text)
    pending.last_chunk_at = datetime.now(timezone.utc)


__all__ = [
    "DmCallback",
    "ExtractionJob",
    "PendingPaste",
    "SaveRawResult",
    "METHOD_EXTRACTION_PROMPT",
    "append_paste_chunk",
    "buffer_has_end_marker",
    "drain_queue",
    "enqueue_job",
    "extract_method_profile",
    "extract_voice_profile",
    "find_most_recent_user_paste",
    "get_voice_cluster_prompt",
    "get_voice_extraction_prompt",
    "get_voice_overall_prompt",
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
