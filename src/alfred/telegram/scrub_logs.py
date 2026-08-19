"""``alfred telegram scrub-logs`` — scrub historical bot tokens from log files.

Telegram retirement hygiene (T4 C6, 2026-08-19). Every BotFather token was
REVOKED on 2026-08-18, so this is hygiene, not secrecy: the strings left in
historical log files are cryptographically dead, but dead credentials in
plaintext are still the kind of debris that trips scanners, gets copy-pasted
into bug reports, and teaches the wrong habit. One pass rewrites them to a
fixed placeholder.

What it matches
---------------
The Telegram bot-token wire shape: ``<bot_id>:<secret>`` where ``bot_id`` is
6-12 digits and ``secret`` is 30-50 chars of ``[A-Za-z0-9_-]``. That covers
the bare token and the URL-embedded form (``api.telegram.org/bot<token>/…``)
with one pattern. The floor of 30 secret chars keeps timestamps (``12:34``)
and dict reprs out of the blast radius. Operators can additionally pass
``--token`` literals for exact-match scrubbing (repeatable).

How it writes
-------------
Atomic per file: stream lines to ``<name>.tmp`` in the same directory, then
``os.replace`` — the same .tmp → rename discipline as every state writer in
this repo. Files with no matches are left untouched (no rewrite, no mtime
churn). ``--dry-run`` reports what WOULD change and writes nothing.

LIVE-LOG WARNING — run ``alfred down`` first. ``os.replace`` swaps the
directory entry, not the file a running daemon already holds open: a daemon
whose FileHandler points at the pre-scrub inode keeps writing to that now
UNLINKED inode, so every log line it emits after the scrub is LOST until
its next rotation or restart — not just a line that races the rewrite
window. On a 24/7-daemon machine (the box), stop the daemons before
scrubbing, or knowingly accept losing the live file's tail until restart.

Encoding caveat: files are read with ``errors="replace"``, so a NON-UTF8
log that contains a token gets rewritten with every undecodable byte in
the whole file replaced by U+FFFD — the collateral extends beyond the
token itself. Clean-scan files are never rewritten, so only token-bearing
non-UTF8 files pay this; the per-file report line is your record that a
rewrite happened.

Intentionally-left-blank: the scan reports every file it looked at, including
"0 tokens" files, and emits an explicit "nothing to scrub" summary when the
whole scan is clean — a clean run must be distinguishable from a run that
scanned nothing.

Compressed rotations (``*.gz``) are NOT rewritten — they are reported as
skipped so their existence is visible; an operator who wants them scrubbed
can decompress and re-run. (Silent absence is the failure mode this module
refuses to have.)

The BOX run is an operator-visible step: this machine's logs are dev-side;
production logs live on the box and are scrubbed there by the operator
running the same command.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .utils import get_logger

log = get_logger(__name__)

# The wire shape of a Telegram bot token. See module docstring for the
# bounds rationale. Compiled once; used for both scan and substitution.
# Anchors are digit/secret-charset lookarounds, NOT ``\b``: the most common
# historical leak shape is the URL-embedded ``/bot<digits>:<secret>/``, where
# the digit run follows the letter "t" — a word boundary never fires between
# two word characters, so a ``\b``-anchored pattern misses exactly the form
# this command exists to scrub (caught by this test file's positive control
# on first run).
TOKEN_PATTERN = re.compile(
    r"(?<![0-9])\d{6,12}:[A-Za-z0-9_-]{30,50}(?![A-Za-z0-9_-])"
)

# What a scrubbed token is rewritten to. Deliberately grep-able.
PLACEHOLDER = "[SCRUBBED-TELEGRAM-BOT-TOKEN]"


@dataclass
class FileResult:
    path: str
    matches: int
    rewritten: bool
    skipped_reason: str = ""


@dataclass
class ScrubReport:
    files: list[FileResult] = field(default_factory=list)

    @property
    def total_matches(self) -> int:
        return sum(f.matches for f in self.files)

    @property
    def scanned(self) -> int:
        return sum(1 for f in self.files if not f.skipped_reason)


def _iter_log_files(log_dir: Path) -> list[Path]:
    """Every plain file under ``log_dir`` whose name contains ``.log``.

    Catches rotated forms (``talker.log.1``, ``talker.log.2026-08-01``)
    as well as the live files. Sorted for deterministic reports.
    """
    if not log_dir.is_dir():
        return []
    return sorted(
        p for p in log_dir.iterdir()
        if p.is_file() and ".log" in p.name
    )


def scrub_file(
    path: Path,
    *,
    extra_tokens: tuple[str, ...] = (),
    dry_run: bool = False,
) -> FileResult:
    """Scrub one file. Returns the per-file result; never raises on content."""
    if path.suffix == ".gz":
        return FileResult(
            path=str(path), matches=0, rewritten=False,
            skipped_reason="compressed (decompress and re-run to scrub)",
        )
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return FileResult(
            path=str(path), matches=0, rewritten=False,
            skipped_reason=f"unreadable: {exc}",
        )

    matches = len(TOKEN_PATTERN.findall(text))
    new_text = TOKEN_PATTERN.sub(PLACEHOLDER, text)
    for literal in extra_tokens:
        if literal and literal in new_text:
            matches += new_text.count(literal)
            new_text = new_text.replace(literal, PLACEHOLDER)

    if matches == 0:
        return FileResult(path=str(path), matches=0, rewritten=False)

    if dry_run:
        return FileResult(path=str(path), matches=matches, rewritten=False)

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return FileResult(path=str(path), matches=matches, rewritten=True)


def scrub_logs(
    log_dir: Path,
    *,
    extra_paths: tuple[Path, ...] = (),
    extra_tokens: tuple[str, ...] = (),
    dry_run: bool = False,
) -> ScrubReport:
    """Scrub every log file in ``log_dir`` plus any ``extra_paths``."""
    report = ScrubReport()
    targets = list(_iter_log_files(log_dir)) + [
        p for p in extra_paths if p.is_file()
    ]
    for path in targets:
        result = scrub_file(
            path, extra_tokens=extra_tokens, dry_run=dry_run,
        )
        report.files.append(result)
        log.info(
            "telegram.scrub_logs.file",
            path=result.path,
            matches=result.matches,
            rewritten=result.rewritten,
            skipped=result.skipped_reason or "",
            dry_run=dry_run,
        )
    if report.total_matches == 0:
        # Intentionally-left-blank: a clean scan says so out loud.
        log.info(
            "telegram.scrub_logs.nothing_to_scrub",
            scanned=report.scanned,
            log_dir=str(log_dir),
        )
    return report
