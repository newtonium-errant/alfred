"""Build the FROZEN link001 work-list (#50) — the file #44b's campaign requires.

``Link001Campaign`` takes pre-built ``worklist_items`` and the wiring reads them
from a frozen file. Nothing generated that file, so the campaign could not
drain. This is the generator.

## Why a file, and why the branch is frozen HERE

D4a: each item's annotate-vs-remove decision is made ONCE, at list-build, and
travels inside the item id. Re-deriving it per run would let the same link
annotate on Monday and delete on Tuesday depending on whether a ``learn/``
record happened to exist in between — a campaign whose behaviour is a function
of when it ran rather than of its input. The two branches have opposite failure
modes: a wrongly-annotated link is noise, a wrongly-removed one is unrecoverable
data loss. The removal branch is what earns the freeze.

## Which side decides the branch — the CITER, not the target

D2, verbatim: *"deletion-time treatment of inbound links FROM learn-records is
NOT bare removal … so the learning stays groundable after its source is gone.
Bare removal is fine for non-learning CITERS."* Both phrases name the record
that HOLDS the link. The scenario is a learning record citing a spam note that
was later deleted: we annotate so the LEARNING keeps its provenance. The target
is the deleted note and is emphatically not a learn record — indeed a broken
target does not exist at all, so its type can only be guessed from its path.

``Link001Campaign.build_item``'s parameter is spelled ``is_learn_target``, which
reads as the opposite. The value is used only as "annotate if True", so the
spelling is a misnomer rather than a behavioural disagreement — but it is a
misnomer that would silently invert this campaign if someone later "fixed" the
call site to match the name. Flagged in the ship report; this module passes the
CITER's classification and says so at the call.

## The #49 coupling

Link references quoted inside a ``janitor_note`` are annotation prose, not
references — before #49 they generated phantom LINK001s, and a work-list built
from them would have "repaired" links that exist only inside explanatory
sentences. This builder consumes the janitor scanner, so it inherits that
exclusion rather than re-implementing it. That inheritance is pinned, not
assumed: a work-list built over a record whose note quotes a link must not
contain an item for the quote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

#: The scanner's LINK001 message shape, which this module parses to recover the
#: broken target. ``Issue`` carries the target only inside its human-readable
#: message, so this is a CROSS-MODULE CONTRACT: a reworded scanner message would
#: silently yield an empty work-list. Pinned against the real scanner output
#: rather than against a hand-written string, so the pin fails when the scanner
#: changes rather than when someone edits the fixture. Precedent:
#: ``autofix.flag_unresolved_links`` parses the same message the same way.
LINK001_MESSAGE_RE = re.compile(r"Broken wikilink: \[\[(?P<target>.+)\]\]\s*$")

#: The item-id field separator, fixed by ``Link001Campaign.ITEM_RE``.
ITEM_SEP = "::"


@dataclass
class WorklistBuild:
    """The build's outcome — what to write, and what it decided."""

    items: list[str] = field(default_factory=list)
    annotate: int = 0
    remove: int = 0
    #: (rel_path, target, reason) for references the builder refused to freeze.
    skipped: list[tuple[str, str, str]] = field(default_factory=list)
    #: Distinct records that contributed at least one item.
    files: int = 0

    @property
    def total(self) -> int:
        return len(self.items)


def _citer_is_learn_record(vault_path: Path, rel_path: str, learn_types: set[str]) -> bool:
    """Is the record HOLDING the broken link a learn record?

    Frontmatter ``type`` is authoritative; the directory is the fallback for a
    record whose type is missing or unreadable. Directory-only would misjudge a
    record filed in the wrong place — which is exactly the population DIR001
    exists to report, so it cannot be assumed away.
    """
    from alfred.janitor.parser import parse_file

    try:
        record = parse_file(vault_path, rel_path)
    except (OSError, UnicodeDecodeError, ValueError):
        rec_type = ""
    else:
        rec_type = str(record.record_type or "")

    if rec_type:
        return rec_type in learn_types
    first_dir = rel_path.replace("\\", "/").split("/")[0]
    return first_dir in learn_types


def build_link001_worklist(config, state) -> WorklistBuild:
    """Scan ONCE and freeze one item per (record, broken target).

    ``config`` is a ``JanitorConfig`` — the scanner's own config, so the
    work-list is built from exactly the issue set the janitor reports, #49's
    annotation exclusion included.
    """
    from alfred.janitor.issues import IssueCode
    from alfred.janitor.scanner import run_structural_scan
    from alfred.vault.schema import LEARN_TYPES

    from .campaigns import Link001Campaign

    vault_path = Path(config.vault.vault_path)
    if not vault_path.is_dir():
        raise FileNotFoundError(
            f"vault path does not exist: {vault_path} — refusing to build a "
            "work-list against a missing vault"
        )

    learn_types = set(LEARN_TYPES)
    issues = run_structural_scan(config, state)
    link001 = [i for i in issues if i.code is IssueCode.BROKEN_WIKILINK]

    build = WorklistBuild()
    seen: set[tuple[str, str]] = set()
    learn_cache: dict[str, bool] = {}

    for issue in link001:
        match = LINK001_MESSAGE_RE.search(issue.message or "")
        if not match:
            build.skipped.append(
                (issue.file, "", "could not parse the target from the scanner "
                                 "message — the LINK001 message format changed")
            )
            continue
        target = match.group("target").strip()
        rel_path = issue.file

        # One item per (record, target). ``work()`` replaces EVERY occurrence in
        # the file, so a target linked twice in one record is one unit of work;
        # emitting it twice would make the second attempt a verified no-op and
        # inflate the campaign's denominator.
        key = (rel_path, target)
        if key in seen:
            continue
        seen.add(key)

        # The separator is structural. A path or target containing it would
        # produce an item the campaign's own regex parses into the wrong
        # fields — and the wrong field here can mean deleting the wrong link.
        if ITEM_SEP in rel_path or ITEM_SEP in target:
            build.skipped.append(
                (rel_path, target,
                 f"contains the {ITEM_SEP!r} item separator — cannot be encoded "
                 "unambiguously")
            )
            continue

        if rel_path not in learn_cache:
            learn_cache[rel_path] = _citer_is_learn_record(
                vault_path, rel_path, learn_types,
            )
        citer_is_learn = learn_cache[rel_path]

        # NOTE THE ARGUMENT NAME. ``is_learn_target`` is a misnomer in the
        # campaign (see this module's docstring); the value it wants is whether
        # the CITER is a learn record, which is what D2 rules on.
        build.items.append(
            Link001Campaign.build_item(
                rel_path, target, is_learn_target=citer_is_learn,
            )
        )
        if citer_is_learn:
            build.annotate += 1
        else:
            build.remove += 1

    build.files = len({k[0] for k in seen})

    log.info(
        "drip.worklist.built",
        campaign="link001_repair",
        total=build.total,
        annotate=build.annotate,
        remove=build.remove,
        files=build.files,
        skipped=len(build.skipped),
        link001_scanned=len(link001),
    )
    return build


def render_worklist(build: WorklistBuild, *, source: str) -> str:
    """The frozen file's text: a provenance header, then one item per line.

    The header is comment lines, which ``load_worklist_file`` already ignores.
    It exists because this file drives irreversible edits weeks after it is
    written, and "where did this come from" must be answerable from the artifact
    itself rather than from whoever remembers running the command.
    """
    lines = [
        "# link001_repair work-list — FROZEN at build time (D4a).",
        "# Each item's annotate|remove branch was decided ONCE, here. Do not",
        "# regenerate to 'refresh' it: re-deriving lets the same link annotate",
        "# one week and delete the next, and removal is unrecoverable.",
        f"# source: {source}",
        f"# items: {build.total} ({build.annotate} annotate, {build.remove} remove)"
        f" across {build.files} record(s)",
        "",
    ]
    lines.extend(build.items)
    return "\n".join(lines) + "\n"


__all__ = [
    "ITEM_SEP",
    "LINK001_MESSAGE_RE",
    "WorklistBuild",
    "build_link001_worklist",
    "render_worklist",
]
