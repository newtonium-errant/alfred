"""c3 retroactive sweep — promote ``_source:`` annotations to
attribution markers.

Scans ``vault/person/*.md`` (the highest-stakes target — calibration
blocks) for bullets that already carry a soft ``_source: ...`` italic
annotation. Each such bullet is treated as a candidate for the formal
BEGIN_INFERRED / ``attribution_audit`` contract: the bullet's content
gets wrapped in a per-line marker pair, and one ``attribution_audit``
entry is appended to the record's frontmatter.

The c3 v1 scope is intentionally tight:

- Only ``vault/person/*.md`` (calibration blocks live here).
- Only bullets with an existing ``_source:`` italic annotation. The
  annotation IS the soft attribution; c3 promotes it to the structured
  marker convention.
- Records with empty body or no recognisable bullets are skipped.
- Idempotent: re-runs find no new candidates because the body wraps
  are detected and skipped.

The CLI in :mod:`alfred.audit.cli` wraps :func:`sweep_paths` with
``--dry-run`` (default) / ``--apply`` / ``--paths`` ergonomics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

import frontmatter
import structlog

from alfred.vault import attribution

log = structlog.get_logger(__name__)


# Bullet line with a trailing ``_source: <path>_`` italic. Matches the
# convention the calibration writer uses (one source per bullet,
# placed at the end inside underscores).
#
# Pattern parts:
#   ^(?P<indent>\s*)        leading whitespace
#   (?P<bullet>[-*]\s+)     bullet marker (- or *) plus space
#   (?P<text>.*?)           bullet body (non-greedy)
#   \s+_source:\s+
#   (?P<source>[^_]+?)      source path (no underscores)
#   _\s*$                   closing underscore + EOL
_BULLET_WITH_SOURCE_RE = re.compile(
    r"^(?P<indent>\s*)(?P<bullet>[-*]\s+)(?P<text>.*?)\s+_source:\s+(?P<source>[^_]+?)_\s*$"
)


# Section heading regex used to compute the section_title for the
# audit entry. Matches ``## Heading`` / ``### Subheading`` etc.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*$")


@dataclass
class InferMarkerCandidate:
    """One bullet flagged as agent-inferred during the sweep.

    ``record_path`` is vault-relative. ``line_number`` is 1-indexed
    (matches what an editor shows). ``section_title`` is the most
    recent ``##``/``###`` heading above the bullet, or the file stem
    when the bullet sits above any heading.
    """

    record_path: str
    line_number: int
    bullet_text: str
    section_title: str
    source: str
    agent: str
    reason: str


@dataclass
class InferMarkerResult:
    """Aggregated outcome of a sweep run.

    ``marked``: bullets newly wrapped + audit_entry appended.
    ``skipped_already_marked``: bullets sitting inside an existing
    BEGIN_INFERRED block (idempotent re-run).
    ``skipped_no_source``: records scanned but containing no
    ``_source:`` annotated bullets — the v1 scope only acts on
    soft-attributed content.
    ``errors``: per-record (path, error_message).
    ``elapsed_seconds``: wall-clock duration of the sweep.
    """

    marked: int = 0
    skipped_already_marked: int = 0
    skipped_no_source: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    candidates: list[InferMarkerCandidate] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def summary_line(self) -> str:
        """One-line summary suitable for ``alfred audit infer-marker`` stdout."""
        err_count = len(self.errors)
        return (
            f"marked={self.marked} "
            f"skipped_already_marked={self.skipped_already_marked} "
            f"skipped_no_source={self.skipped_no_source} "
            f"errors={err_count} "
            f"elapsed={self.elapsed_seconds:.2f}s"
        )


def _agent_from_source(source: str, default_slug: str = "salem") -> str:
    """Map a ``_source:`` annotation path to an agent slug.

    The convention today: ``session/...`` paths are voice / talker
    sessions (the running instance — Salem on the Salem bot, Hypatia
    on the Hypatia bot). ``memory/...`` paths are imported memory
    entries (also classify under the running instance since the
    inference happened in its turn). Anything else falls back to the
    same slug — the calibration writer is the dominant author of these
    annotations and the source path doesn't reliably carry an agent
    identity.

    ``default_slug`` is the running instance's slug (lowercased
    ``config.instance.name``). Defaults to ``"salem"`` so legacy
    callers that don't thread a slug through preserve their behaviour.
    Pass ``audit.agent_slug_for(config)`` from the call site to honour
    multi-instance attribution.
    """
    # All branches use the same slug today — the source path doesn't
    # disambiguate which agent inferred the bullet. Kept as a single
    # function so a future heuristic (e.g. distinguishing curator
    # imports from talker turns) lands here in one place.
    return default_slug


def _section_title_for(lines: list[str], idx: int, fallback: str) -> str:
    """Walk backward from ``idx`` to find the nearest heading above it."""
    for i in range(idx - 1, -1, -1):
        m = _HEADING_RE.match(lines[i])
        if m:
            return m.group("title").strip()
    return fallback


def _line_is_inside_existing_marker(
    lines: list[str], idx: int, existing_spans: list[tuple[int, int]]
) -> bool:
    """Return True when line ``idx`` (0-indexed) is inside any existing
    ``BEGIN_INFERRED`` / ``END_INFERRED`` span."""
    for begin, end in existing_spans:
        if begin <= idx <= end:
            return True
    return False


def _existing_marker_spans(body: str) -> list[tuple[int, int]]:
    """Return a list of (begin_line_idx, end_line_idx) for every existing
    BEGIN/END marker pair in ``body``. 0-indexed, inclusive."""
    spans: list[tuple[int, int]] = []
    lines = body.splitlines()
    begin_idx: int | None = None
    for i, line in enumerate(lines):
        if "BEGIN_INFERRED" in line:
            begin_idx = i
        elif "END_INFERRED" in line and begin_idx is not None:
            spans.append((begin_idx, i))
            begin_idx = None
    return spans


def _scan_record(
    rel_path: str,
    full_path: Path,
    *,
    agent_slug: str = "salem",
) -> tuple[
    list[InferMarkerCandidate], int, str | None
]:
    """Read one record + return (candidates, already_marked_count, error).

    ``error`` is None on success. Returns an empty candidate list when
    the record has no ``_source:`` bullets — caller buckets it as
    ``skipped_no_source``.

    ``agent_slug`` is forwarded to :func:`_agent_from_source` so each
    candidate's ``agent`` field carries the running instance's slug
    instead of a hardcoded ``"salem"``.
    """
    try:
        post = frontmatter.load(str(full_path))
    except Exception as exc:  # noqa: BLE001
        return [], 0, f"read failed: {exc}"

    body = post.content or ""
    if not body.strip():
        return [], 0, None

    lines = body.splitlines()
    existing_spans = _existing_marker_spans(body)

    candidates: list[InferMarkerCandidate] = []
    already_marked = 0
    file_stem = Path(rel_path).stem
    for i, line in enumerate(lines):
        m = _BULLET_WITH_SOURCE_RE.match(line)
        if not m:
            continue
        if _line_is_inside_existing_marker(lines, i, existing_spans):
            already_marked += 1
            continue
        text = m.group("text").strip()
        source = m.group("source").strip()
        section_title = _section_title_for(lines, i, file_stem)
        agent = _agent_from_source(source, default_slug=agent_slug)
        candidates.append(
            InferMarkerCandidate(
                record_path=rel_path,
                line_number=i + 1,
                bullet_text=text,
                section_title=section_title,
                source=source,
                agent=agent,
                reason=f"from {source}",
            )
        )
    return candidates, already_marked, None


def _apply_candidate(
    body: str, fm: dict, candidate: InferMarkerCandidate, line_idx: int
) -> tuple[str, dict, bool]:
    """Wrap one bullet line in BEGIN/END markers + append the audit entry.

    Returns ``(new_body, new_fm, did_apply)``. ``did_apply`` is False
    when the line at ``line_idx`` no longer matches the candidate (the
    body shifted between scan and apply because an earlier candidate
    wrapping pushed lines down). Caller should re-scan to recover from
    that case; for v1 we just skip.
    """
    lines = body.splitlines()
    if line_idx >= len(lines):
        return body, fm, False
    line = lines[line_idx]
    if not _BULLET_WITH_SOURCE_RE.match(line):
        return body, fm, False

    wrapped, audit_entry = attribution.with_inferred_marker(
        line,
        section_title=candidate.section_title,
        agent=candidate.agent,
        reason=candidate.reason,
    )
    # ``with_inferred_marker`` returns a multi-line wrapped block.
    # Splice it in by replacing the single source line with the wrap.
    new_lines = lines[:line_idx] + wrapped.splitlines() + lines[line_idx + 1 :]
    new_body = "\n".join(new_lines)
    if body.endswith("\n"):
        new_body += "\n"
    attribution.append_audit_entry(fm, audit_entry)
    return new_body, fm, True


def sweep_paths(
    vault_path: Path,
    rel_paths: list[str],
    *,
    apply: bool = False,
    agent_slug: str = "salem",
) -> InferMarkerResult:
    """Scan ``rel_paths`` under ``vault_path`` and (optionally) apply
    attribution markers to every soft-attributed bullet.

    ``rel_paths`` is the list of vault-relative file paths to inspect;
    the CLI defaults this to every ``person/*.md``. Pass ``apply=True``
    to write changes; the default is dry-run (collect candidates only).

    ``agent_slug`` is the running instance's slug (lowercased
    ``config.instance.name``); each generated audit entry's ``agent``
    field carries this. Defaults to ``"salem"`` so legacy callers that
    don't thread a slug through preserve current behaviour. The CLI
    handler reads this from the loaded config — see
    :func:`alfred.audit.cli.cmd_infer_marker`.

    Idempotent: bullets already inside a ``BEGIN_INFERRED`` block are
    counted under ``skipped_already_marked`` and not re-wrapped.
    Records with no ``_source:`` annotated bullets are bucketed under
    ``skipped_no_source``.
    """
    started = perf_counter()
    result = InferMarkerResult()

    for rel in rel_paths:
        full = vault_path / rel
        if not full.exists():
            result.errors.append((rel, "file not found"))
            continue

        candidates, already_marked, err = _scan_record(
            rel, full, agent_slug=agent_slug,
        )
        if err is not None:
            result.errors.append((rel, err))
            continue

        result.skipped_already_marked += already_marked

        if not candidates:
            result.skipped_no_source += 1
            continue

        result.candidates.extend(candidates)

        if not apply:
            # Dry-run: counts come from len(candidates) — we only
            # bump ``marked`` on actual writes below.
            continue

        # Apply path. Re-load the record once per call (we need both
        # the parsed frontmatter and the current body string), splice
        # each candidate in, then write the merged result back atomically.
        try:
            post = frontmatter.load(str(full))
        except Exception as exc:  # noqa: BLE001
            result.errors.append((rel, f"reload failed: {exc}"))
            continue

        body = post.content or ""
        fm = post.metadata or {}

        # Apply candidates from BOTTOM to TOP so earlier wrappings
        # don't shift the line numbers of later ones.
        for candidate in sorted(
            candidates, key=lambda c: c.line_number, reverse=True,
        ):
            new_body, new_fm, ok = _apply_candidate(
                body, fm, candidate, candidate.line_number - 1,
            )
            if not ok:
                result.errors.append(
                    (rel, f"line {candidate.line_number} drifted; skipped")
                )
                continue
            body = new_body
            fm = new_fm
            result.marked += 1

        post.metadata = fm
        post.content = body
        try:
            full.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
        except OSError as exc:
            result.errors.append((rel, f"write failed: {exc}"))

    result.elapsed_seconds = perf_counter() - started
    log.info(
        "audit.sweep.complete",
        marked=result.marked,
        skipped_already_marked=result.skipped_already_marked,
        skipped_no_source=result.skipped_no_source,
        errors=len(result.errors),
        elapsed_seconds=result.elapsed_seconds,
    )
    return result


__all__ = [
    "InferMarkerCandidate",
    "InferMarkerResult",
    "sweep_paths",
]
