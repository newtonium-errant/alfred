"""Layer 3 triage queue — advisory surface for ambiguous janitor findings.

Background
----------
The janitor's structural scanner detects a class of issues that cannot be
auto-fixed safely because they require human semantic judgment — the clearest
example is a DUP001 dedup candidate pair where two entity records (e.g.
``org/Acme Corp`` and ``org/Acme Corporation``) might legitimately describe
the same thing, or might not. Auto-merging either way risks silent data loss.

Layer 3 surfaces these decisions to the user as **triage tasks**: regular
``task/`` records with a distinctive frontmatter flag. A human reads the
task, inspects the candidates, and decides. There is NO auto-merge loop and
no auto-resolve — this module is advisory-only.

Agent-writes-directly
---------------------
Per the architectural contract, the janitor agent creates triage task records
via ``alfred vault create task ...`` using its janitor scope. Python code
does NOT create triage records directly; Python's job here is:

1. Define the deterministic ID scheme (`compute_triage_id`) so the same
   candidate set always produces the same id regardless of discovery order.
2. Expose existing open triage tasks to the agent via prompt context
   (see ``janitor.backends.build_sweep_prompt``) so the agent can skip
   already-queued items.
3. Track surfaced ids in janitor state (``triage_ids_seen``) as a hard
   idempotency layer behind the soft prompt-side check.

Triage record format (contract for prompt-tuner / janitor SKILL.md)
-------------------------------------------------------------------
**Path:** ``task/Triage - <short human description>.md``

**Frontmatter:**

.. code-block:: yaml

    type: task
    name: "Triage - <candidate-summary>"
    status: todo
    created: <ISO date>
    alfred_triage: true
    alfred_triage_kind: dedup        # future kinds: orphan, broken_link, etc.
    alfred_triage_id: dedup-a7f3c2b1d8e4
    candidates:
      - "[[org/Acme Corp]]"
      - "[[org/Acme Corporation]]"
    priority: normal

The ``alfred_triage`` flag is load-bearing: the janitor scope
(``vault.scope.check_scope``) only permits janitor to create task records
when this flag is present, so the agent cannot accidentally create regular
tasks.

Triage ID derivation
--------------------
``compute_triage_id(kind, candidates)`` returns a deterministic string of
the form ``<kind>-<12hexchars>``. The hash input is the ``kind`` followed
by the sorted list of candidate paths joined by ``|``, so the same two
candidates produce the same id regardless of argument order. This stable
id is what prevents spam across successive sweeps when the same DUP001
pair reappears.

Example::

    >>> compute_triage_id("dedup", ["org/Acme Corp", "org/Acme Corporation"])
    'dedup-<12 hex>'
    >>> compute_triage_id("dedup", ["org/Acme Corporation", "org/Acme Corp"])
    'dedup-<same 12 hex>'

What this module does NOT do
----------------------------
- No auto-merge, auto-resolve, or auto-edit of candidate records.
- No task creation — the agent writes the task via ``alfred vault``.
- No status polling — the janitor daemon does not watch triage tasks.

The human is the authority; the janitor just surfaces.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

import frontmatter

from .utils import get_logger

log = get_logger(__name__)


# Known triage kinds. Keep in sync with the janitor SKILL.md.
# Only 'dedup' is implemented for Layer 3; the rest are reserved names.
TRIAGE_KINDS: set[str] = {
    "dedup",
    "orphan",
    "broken_link",
    "ambiguous_type",
}


def compute_triage_id(kind: str, candidates: list[str]) -> str:
    """Return a deterministic triage id for a set of candidate paths.

    The id is order-independent: the same candidate list in any permutation
    yields the same id. This is the hard idempotency key — it lives in
    janitor state as ``triage_ids_seen`` and in the task frontmatter as
    ``alfred_triage_id``.

    Args:
        kind: One of ``TRIAGE_KINDS``. The kind is prefixed onto the id so
            that the same two paths flagged for two different reasons
            (e.g. dedup vs. ambiguous_type) produce distinct ids.
        candidates: List of candidate path strings. Wikilinks
            (``[[type/Name]]``) and bare relative paths
            (``type/Name.md``) are both accepted; they are normalised for
            hashing by stripping wikilink brackets and the ``.md`` suffix.

    Returns:
        A string of the form ``<kind>-<12 hex chars>``.

    Raises:
        ValueError: If ``kind`` is empty or ``candidates`` is empty.
    """
    if not kind:
        raise ValueError("compute_triage_id: kind must be non-empty")
    if not candidates:
        raise ValueError("compute_triage_id: candidates must be non-empty")

    normalised = sorted({_normalise_candidate(c) for c in candidates})
    payload = f"{kind}|" + "|".join(normalised)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{digest}"


def _normalise_candidate(raw: str) -> str:
    """Normalise a candidate reference to its canonical relative path.

    Accepts: ``[[type/Name]]``, ``[[type/Name|alias]]``, ``type/Name``,
    ``type/Name.md``. Returns ``type/Name``.
    """
    s = raw.strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2]
    # strip alias portion: [[type/Name|alias]] -> type/Name
    if "|" in s:
        s = s.split("|", 1)[0]
    if s.endswith(".md"):
        s = s[:-3]
    return s.strip()


def collect_open_triage_tasks(vault_path: Path) -> list[dict]:
    """Walk ``vault/task/`` and return all open triage tasks.

    Returns a list of dicts with keys: ``path`` (stem, no .md), ``name``,
    ``triage_id``, ``triage_kind``, ``candidates``, ``created``. Sorted by
    triage_id for stable prompt output.

    Tasks without ``alfred_triage: true`` or without ``status: todo`` are
    filtered out. Parse failures are skipped with a log entry.
    """
    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        return []

    out: list[dict] = []
    for md_file in task_dir.glob("*.md"):
        try:
            post = frontmatter.load(str(md_file))
        except Exception as exc:  # noqa: BLE001 — skip unreadable files
            log.warning(
                "triage.parse_failed",
                path=str(md_file),
                error=str(exc)[:200],
            )
            continue

        fm = post.metadata or {}
        if not fm.get("alfred_triage"):
            continue
        if str(fm.get("status", "")).lower() not in ("todo", "active"):
            continue

        rel_stem = str(md_file.relative_to(vault_path)).replace("\\", "/")
        if rel_stem.endswith(".md"):
            rel_stem = rel_stem[:-3]

        candidates_raw = fm.get("candidates") or []
        if isinstance(candidates_raw, str):
            candidates_list: list[str] = [candidates_raw]
        elif isinstance(candidates_raw, Iterable):
            candidates_list = [str(c) for c in candidates_raw]
        else:
            candidates_list = []

        out.append(
            {
                "path": rel_stem,
                "name": str(fm.get("name", md_file.stem)),
                "triage_id": str(fm.get("alfred_triage_id", "")),
                "triage_kind": str(fm.get("alfred_triage_kind", "")),
                "candidates": candidates_list,
                "created": str(fm.get("created", "")),
            }
        )

    out.sort(key=lambda t: (t["triage_id"], t["path"]))
    return out


def format_open_triage_block(
    tasks: list[dict],
    seen_ids: set[str] | None = None,
) -> str:
    """Format the list of open triage tasks as a prompt context block.

    This is injected into the janitor agent prompt as the
    ``## Existing Open Triage Tasks`` section so the agent can check
    whether a candidate set has already been surfaced before creating a
    new triage task.

    Args:
        tasks: List of open triage task dicts from ``collect_open_triage_tasks``.
        seen_ids: Optional set of triage IDs that have been surfaced in past
            sweeps. IDs in this set but NOT in the current open-tasks list
            (i.e. the human closed or deleted them) are rendered as a second
            ``## Triage IDs Already Surfaced`` block so the agent does not
            re-create them. The decision has already been made.
    """
    if not tasks:
        primary = (
            "## Existing Open Triage Tasks\n\n"
            "(none — all clear)\n"
        )
    else:
        lines: list[str] = ["## Existing Open Triage Tasks", ""]
        for t in tasks:
            lines.append(f"- **{t['path']}**")
            if t["triage_id"]:
                lines.append(f"  - alfred_triage_id: `{t['triage_id']}`")
            if t["triage_kind"]:
                lines.append(f"  - alfred_triage_kind: {t['triage_kind']}")
            if t["created"]:
                lines.append(f"  - created: {t['created']}")
            if t["candidates"]:
                lines.append("  - candidates:")
                for c in t["candidates"]:
                    lines.append(f"    - {c}")
            lines.append("")
        primary = "\n".join(lines)

    if not seen_ids:
        return primary

    open_ids = {t["triage_id"] for t in tasks if t.get("triage_id")}
    historical_only = sorted(seen_ids - open_ids)
    if not historical_only:
        return primary

    suppressed_lines: list[str] = [
        "",
        "## Triage IDs Already Surfaced (do not re-create)",
        "",
        "(These triage IDs have already been surfaced in past sweeps. Even",
        "if no matching open task is listed above — the human may have closed",
        "or deleted it — do NOT create a new triage task with any of these",
        "IDs. The decision was already made.)",
        "",
    ]
    for tid in historical_only:
        suppressed_lines.append(f"- {tid}")
    suppressed_lines.append("")

    return primary.rstrip("\n") + "\n" + "\n".join(suppressed_lines)
