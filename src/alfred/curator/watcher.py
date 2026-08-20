"""Watch inbox/ for new files with debounce.

ADMISSION IS FILTERED (#88). Both entry points — the live watchdog events
and the startup ``full_scan`` — run every candidate through the SAME
predicate (:func:`is_ingestable`), because the two paths admitting different
things is precisely how a file gets processed by one and not the other.

The filter exists for a measured reason. Three attachment savers wrote
BINARIES into this watched directory: ``telegram.vision.save_image_to_inbox``
(``screenshot-*``, still live via the web image path) plus the retired
bot-side ``save_document_to_inbox`` (``document-*``) and
``save_audio_to_inbox`` (``audio-*``) from ``telegram/attachments.py``
(deleted with the Telegram retirement, T5 2026-08-19; their files persist
in inboxes). With no extension filter, every screenshot, PDF and voice
note the operator sent was picked up here and pushed through ``claude -p``
a SECOND time — paid LLM spend producing junk records from a file the
talker had already handled.
(#83's batch lane already dodged this by siting its scans OUTSIDE the vault;
``batch/paths.py`` documents the same hazard.)

A non-admitted file is skipped **and logged once per filename**
(:class:`SkipLatch`) — not once per sweep. The rescan loop revisits the
inbox every minute, so an unlatched line would print the same skip sixty
times an hour forever and the operator would filter it out, which is the
same as not logging it. Once-per-filename keeps "why is that file just
sitting there?" answerable without the noise.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import frontmatter
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .utils import get_logger

log = get_logger(__name__)

#: Fallback admission set, used when no config-derived list is supplied.
#: Kept in lockstep with ``curator.config.WatcherConfig.ingestable_extensions``
#: — the config default is the operator-facing one and this is the
#: direct-construction default; a drift pin asserts they match.
DEFAULT_INGESTABLE_EXTENSIONS: tuple[str, ...] = (".md", ".markdown", ".txt")

#: Names that are never inbox entries regardless of extension.
SKIP_NAMES: frozenset[str] = frozenset({
    ".DS_Store", ".gitkeep", "Thumbs.db", ".gitignore",
})

#: Ceiling on remembered skip filenames. Unbounded growth in a long-running
#: daemon is a slow leak; at the cap the latch resets and says so, which is
#: honest about the repeated lines that will follow.
_SKIP_LATCH_MAX = 1000


def is_ingestable(path: Path, extensions: "tuple[str, ...] | list[str]") -> bool:
    """Whether the curator should ADMIT ``path`` from the inbox.

    The single admission predicate — consumed by the live handler and by
    ``full_scan``, never re-localized. An empty ``extensions`` admits
    NOTHING (fail-closed): a config that somehow resolved to an empty list
    should stall visibly rather than silently restore the old
    admit-everything behaviour that cost real money.
    """
    return path.suffix.lower() in set(extensions)


class SkipLatch:
    """Remembers which filenames have already been reported as skipped.

    Shared by the handler and the scanner so one file produces ONE line
    between them, not one each. Thread-safe because watchdog calls the
    handler from its own observer thread while the daemon's loop scans.
    """

    def __init__(self, max_entries: int = _SKIP_LATCH_MAX) -> None:
        self._seen: set[str] = set()
        self._max = max_entries
        self._lock = threading.Lock()

    def should_log(self, name: str) -> bool:
        """True the FIRST time ``name`` is offered; False thereafter."""
        with self._lock:
            if name in self._seen:
                return False
            if len(self._seen) >= self._max:
                self._seen.clear()
                log.info(
                    "watcher.skip_latch_reset",
                    max_entries=self._max,
                    detail="the skipped-filename latch hit its ceiling and "
                           "was cleared; previously-reported skips may be "
                           "logged once more.",
                )
            self._seen.add(name)
            return True

    def forget(self, name: str) -> None:
        """Drop ``name`` so a re-drop of the same filename reports again."""
        with self._lock:
            self._seen.discard(name)


def _log_skip(
    path: Path,
    extensions: "tuple[str, ...] | list[str]",
    latch: SkipLatch,
    source: str,
) -> None:
    """Emit the once-per-filename not-ingestable line."""
    if not latch.should_log(path.name):
        return
    log.info(
        "watcher.skipped_not_ingestable",
        path=str(path),
        suffix=path.suffix.lower() or "(none)",
        allowed=list(extensions),
        source=source,
        detail="not an ingestable type, so the curator did NOT process it. "
               "Attachment saves (screenshot-/document-/audio-) land here by "
               "design and are handled by the talker; widen "
               "curator.watcher.ingestable_extensions to admit a new type.",
    )


class InboxHandler(FileSystemEventHandler):
    """Collect file events from inbox/ with debounce."""

    def __init__(
        self,
        debounce_seconds: float = 10.0,
        ingestable_extensions: "tuple[str, ...] | list[str] | None" = None,
        skip_latch: SkipLatch | None = None,
    ) -> None:
        super().__init__()
        self.debounce_seconds = debounce_seconds
        self.ingestable_extensions = tuple(
            ingestable_extensions
            if ingestable_extensions is not None
            else DEFAULT_INGESTABLE_EXTENSIONS
        )
        self.skip_latch = skip_latch if skip_latch is not None else SkipLatch()
        self._pending: dict[str, float] = {}  # path -> last_event_time
        self._lock = threading.Lock()

    def on_created(self, event: FileCreatedEvent) -> None:
        self._handle(event.src_path)

    def on_modified(self, event: FileModifiedEvent) -> None:
        self._handle(event.src_path)

    def _handle(self, src_path: str) -> None:
        path = Path(src_path)
        # Skip directories, processed/ subdirectory, and dotfiles
        if path.is_dir():
            return
        if "processed" in path.parts:
            return
        if path.name.startswith("."):
            return
        # Skip curator's own lock sidecars (see daemon._claim_file) — these
        # were re-triggering the pipeline on themselves and producing duplicates.
        if path.suffix == ".lock":
            return
        if path.name in SKIP_NAMES:
            return
        # ADMISSION FILTER (#88) — the same predicate full_scan uses. Checked
        # here as well as there because an attachment save fires a live
        # CREATE event: filtering only at startup would still let every
        # screenshot through on the path that actually runs in production.
        if not is_ingestable(path, self.ingestable_extensions):
            _log_skip(path, self.ingestable_extensions, self.skip_latch, "event")
            return
        with self._lock:
            self._pending[str(path)] = time.time()
            log.debug("watcher.event", path=str(path))

    def collect_ready(self) -> list[Path]:
        """Return paths that have been stable past the debounce window."""
        now = time.time()
        ready: list[Path] = []
        with self._lock:
            still_pending: dict[str, float] = {}
            for path_str, last_time in self._pending.items():
                if now - last_time >= self.debounce_seconds:
                    ready.append(Path(path_str))
                else:
                    still_pending[path_str] = last_time
            self._pending = still_pending
        return ready


class InboxWatcher:
    """Manages the watchdog observer for the inbox directory."""

    def __init__(
        self,
        inbox_path: Path,
        debounce_seconds: float = 10.0,
        ingestable_extensions: "tuple[str, ...] | list[str] | None" = None,
    ) -> None:
        self.inbox_path = inbox_path
        self.ingestable_extensions = tuple(
            ingestable_extensions
            if ingestable_extensions is not None
            else DEFAULT_INGESTABLE_EXTENSIONS
        )
        # ONE latch shared with the handler: a file that arrives as a live
        # event and is still there at the next rescan is one skipped file,
        # and it should read as one line.
        self.skip_latch = SkipLatch()
        self.handler = InboxHandler(
            debounce_seconds=debounce_seconds,
            ingestable_extensions=self.ingestable_extensions,
            skip_latch=self.skip_latch,
        )
        self._observer: Observer | None = None

    def start(self) -> None:
        self.inbox_path.mkdir(parents=True, exist_ok=True)
        self._observer = Observer()
        self._observer.schedule(self.handler, str(self.inbox_path), recursive=False)
        self._observer.daemon = True
        self._observer.start()
        log.info("watcher.started", path=str(self.inbox_path))

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            log.info("watcher.stopped")

    def collect_ready(self) -> list[Path]:
        return self.handler.collect_ready()

    def full_scan(self, state_processed: set[str] | None = None) -> list[Path]:
        """Scan inbox for unprocessed files (startup catch-up).

        Note: state_processed is intentionally NOT used to skip files.
        A file re-uploaded with the same name should be reprocessed.
        Properly processed files are moved to processed/ and won't appear here.
        """
        unprocessed: list[Path] = []
        skipped = 0

        for md_file in self.inbox_path.iterdir():
            if not md_file.is_file():
                continue
            if md_file.name.startswith(".") or md_file.name in SKIP_NAMES:
                continue
            # Skip curator's own lock sidecars (see daemon._claim_file). These
            # are created in-place next to the inbox file during processing and
            # must never be treated as a fresh inbox entry — doing so caused
            # duplicate record generation on 2026-04-15.
            if md_file.suffix == ".lock":
                continue
            # ADMISSION FILTER (#88) — the same predicate the live handler
            # uses. Checked BEFORE the frontmatter read, which would
            # otherwise try to parse a JPEG as YAML on every sweep.
            if not is_ingestable(md_file, self.ingestable_extensions):
                _log_skip(
                    md_file, self.ingestable_extensions, self.skip_latch, "scan",
                )
                skipped += 1
                continue
            # Check frontmatter status (handles edge case where move failed)
            try:
                post = frontmatter.load(str(md_file))
                if post.metadata.get("status") == "processed":
                    continue
            except Exception:
                pass
            unprocessed.append(md_file)

        # ``skipped`` rides the per-sweep summary even though the per-file
        # lines are latched: the COUNT is how an operator sees that a pile is
        # accumulating without re-reading sixty identical lines.
        log.info(
            "watcher.full_scan",
            found=len(unprocessed),
            skipped_not_ingestable=skipped,
        )
        return unprocessed
