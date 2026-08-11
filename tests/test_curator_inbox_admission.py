"""Curator inbox admission filter (#88).

The bug this closes was a measured spend-burner, not a theoretical one: the
inbox admitted every non-hidden file, and three attachment savers write
BINARIES into that same watched directory (``screenshot-*``, ``document-*``,
``audio-*``). Every screenshot, PDF and voice note the operator sent was
picked up and pushed through ``claude -p`` a second time.

Two properties carry the fix: BOTH admission paths use the SAME predicate
(filtering only one of them leaves the production path open), and a skipped
file is reported ONCE per filename rather than once per sweep.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.curator.config import WatcherConfig
from alfred.curator.watcher import (
    DEFAULT_INGESTABLE_EXTENSIONS,
    InboxHandler,
    InboxWatcher,
    SkipLatch,
    is_ingestable,
)

# The three attachment prefixes that actually land in the watched inbox —
# named from the production writers so this list can't drift into fiction.
ATTACHMENT_FILES = [
    "screenshot-20260811T140302Z-a1b2c3d4.jpg",   # telegram.vision
    "document-20260811T140302Z-a1b2c3d4.pdf",     # attachments (document)
    "audio-20260811T140302Z-a1b2c3d4.bin",        # attachments (audio)
]


@pytest.fixture
def inbox(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    return d


def write(inbox, name: str, text: str = "# note\n") -> None:
    (inbox / name).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["note.md", "note.markdown", "note.txt"])
def test_the_default_set_admits_what_the_legitimate_writers_produce(name, inbox):
    """The mail webhook and fetcher both write ``email-*.md``; an operator's
    hand-dropped note is markdown or plain text."""
    assert is_ingestable(inbox / name, DEFAULT_INGESTABLE_EXTENSIONS) is True


@pytest.mark.parametrize("name", ATTACHMENT_FILES)
def test_the_default_set_refuses_every_attachment_shape(name, inbox):
    assert is_ingestable(inbox / name, DEFAULT_INGESTABLE_EXTENSIONS) is False


def test_the_predicate_is_case_insensitive(inbox):
    """A phone-sourced ``.JPG`` must not slip past a lowercase comparison."""
    assert is_ingestable(inbox / "a.JPG", DEFAULT_INGESTABLE_EXTENSIONS) is False
    assert is_ingestable(inbox / "a.MD", DEFAULT_INGESTABLE_EXTENSIONS) is True


def test_an_extensionless_file_is_refused(inbox):
    assert is_ingestable(inbox / "README", DEFAULT_INGESTABLE_EXTENSIONS) is False


def test_an_empty_set_admits_NOTHING(inbox):
    """FAIL-CLOSED. A config that resolved to an empty list should stall
    visibly, never silently restore the admit-everything behaviour that cost
    real money."""
    assert is_ingestable(inbox / "note.md", []) is False


def test_a_widened_set_admits_the_new_type(inbox):
    assert is_ingestable(inbox / "data.csv", [".md", ".csv"]) is True


# ---------------------------------------------------------------------------
# full_scan
# ---------------------------------------------------------------------------


def test_full_scan_admits_markdown_and_refuses_attachments(inbox):
    write(inbox, "email-work-20260811-hello.md")
    for name in ATTACHMENT_FILES:
        (inbox / name).write_bytes(b"\xff\xd8\xff\xe0binary")

    watcher = InboxWatcher(inbox_path=inbox)
    found = watcher.full_scan()

    assert [p.name for p in found] == ["email-work-20260811-hello.md"]


def test_full_scan_reports_the_skipped_count(inbox):
    """The COUNT rides every sweep even though the per-file lines are
    latched — it is how an operator sees a pile accumulating without
    re-reading sixty identical lines."""
    write(inbox, "note.md")
    for name in ATTACHMENT_FILES:
        (inbox / name).write_bytes(b"binary")

    watcher = InboxWatcher(inbox_path=inbox)
    with structlog.testing.capture_logs() as captured:
        watcher.full_scan()

    events = [c for c in captured if c.get("event") == "watcher.full_scan"]
    assert len(events) == 1
    assert events[0]["found"] == 1
    assert events[0]["skipped_not_ingestable"] == 3


def test_a_skipped_file_is_logged_ONCE_across_repeated_sweeps(inbox):
    """The rescan loop revisits the inbox every minute. An unlatched line
    would print the same skip sixty times an hour forever, and the operator
    would filter it out — which is the same as not logging it."""
    (inbox / ATTACHMENT_FILES[0]).write_bytes(b"binary")
    watcher = InboxWatcher(inbox_path=inbox)

    with structlog.testing.capture_logs() as captured:
        for _ in range(5):
            watcher.full_scan()

    skips = [c for c in captured
             if c.get("event") == "watcher.skipped_not_ingestable"]
    assert len(skips) == 1
    assert skips[0]["suffix"] == ".jpg"
    assert skips[0]["source"] == "scan"
    assert ".md" in skips[0]["allowed"]


def test_the_skip_line_says_what_to_do_about_it(inbox):
    (inbox / ATTACHMENT_FILES[1]).write_bytes(b"%PDF-1.4")
    watcher = InboxWatcher(inbox_path=inbox)
    with structlog.testing.capture_logs() as captured:
        watcher.full_scan()
    skip = next(c for c in captured
                if c.get("event") == "watcher.skipped_not_ingestable")
    assert "ingestable_extensions" in skip["detail"]
    assert "did NOT process it" in skip["detail"]


def test_two_different_skipped_files_each_get_a_line(inbox):
    """Latched per FILENAME, not globally — otherwise the second problem
    file is invisible."""
    for name in ATTACHMENT_FILES:
        (inbox / name).write_bytes(b"binary")
    watcher = InboxWatcher(inbox_path=inbox)
    with structlog.testing.capture_logs() as captured:
        watcher.full_scan()
        watcher.full_scan()
    skips = [c for c in captured
             if c.get("event") == "watcher.skipped_not_ingestable"]
    assert len(skips) == 3


def test_a_binary_is_never_parsed_as_frontmatter(inbox):
    """The filter runs BEFORE the frontmatter read, which would otherwise
    try to parse a JPEG as YAML on every sweep."""
    (inbox / ATTACHMENT_FILES[0]).write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    watcher = InboxWatcher(inbox_path=inbox)

    calls: list[str] = []
    import alfred.curator.watcher as watcher_mod
    original = watcher_mod.frontmatter.load

    def spy(path, *a, **kw):
        calls.append(str(path))
        return original(path, *a, **kw)

    watcher_mod.frontmatter.load = spy
    try:
        watcher.full_scan()
    finally:
        watcher_mod.frontmatter.load = original
    assert calls == []


def test_a_widened_config_admits_the_new_type_through_full_scan(inbox):
    write(inbox, "data.csv", "a,b\n1,2\n")
    watcher = InboxWatcher(
        inbox_path=inbox, ingestable_extensions=[".md", ".csv"],
    )
    assert [p.name for p in watcher.full_scan()] == ["data.csv"]


def test_lock_sidecars_and_dotfiles_are_still_skipped(inbox):
    """The pre-existing exclusions must survive the new filter — the .lock
    rule closed a duplicate-generation bug on 2026-04-15."""
    write(inbox, "note.md")
    write(inbox, "note.md.lock", "")
    write(inbox, ".hidden.md", "")
    write(inbox, ".DS_Store", "")

    watcher = InboxWatcher(inbox_path=inbox)
    assert [p.name for p in watcher.full_scan()] == ["note.md"]


def test_a_processed_file_is_still_skipped(inbox):
    write(inbox, "done.md", "---\nstatus: processed\n---\nbody\n")
    write(inbox, "todo.md")
    watcher = InboxWatcher(inbox_path=inbox)
    assert [p.name for p in watcher.full_scan()] == ["todo.md"]


# ---------------------------------------------------------------------------
# The live event path — the one that actually runs in production
# ---------------------------------------------------------------------------


def test_the_live_handler_refuses_an_attachment_event(inbox):
    """Filtering only ``full_scan`` would leave the production path open: an
    attachment save fires a live CREATE event, and the daemon is already
    running when it lands."""
    handler = InboxHandler(debounce_seconds=0.0)
    path = inbox / ATTACHMENT_FILES[0]
    path.write_bytes(b"binary")

    handler._handle(str(path))
    assert handler.collect_ready() == []


def test_the_live_handler_admits_markdown(inbox):
    handler = InboxHandler(debounce_seconds=0.0)
    path = inbox / "note.md"
    write(inbox, "note.md")

    handler._handle(str(path))
    assert [p.name for p in handler.collect_ready()] == ["note.md"]


def test_the_live_handler_logs_its_skip_with_the_event_source(inbox):
    handler = InboxHandler(debounce_seconds=0.0)
    path = inbox / ATTACHMENT_FILES[0]
    path.write_bytes(b"binary")

    with structlog.testing.capture_logs() as captured:
        handler._handle(str(path))
    skip = next(c for c in captured
                if c.get("event") == "watcher.skipped_not_ingestable")
    assert skip["source"] == "event"


def test_handler_and_scan_share_ONE_latch_so_a_file_reports_once(inbox):
    """A file that arrives as a live event and is still there at the next
    rescan is one skipped file, and it should read as one line."""
    path = inbox / ATTACHMENT_FILES[0]
    path.write_bytes(b"binary")
    watcher = InboxWatcher(inbox_path=inbox)

    with structlog.testing.capture_logs() as captured:
        watcher.handler._handle(str(path))
        watcher.full_scan()

    skips = [c for c in captured
             if c.get("event") == "watcher.skipped_not_ingestable"]
    assert len(skips) == 1


# ---------------------------------------------------------------------------
# The latch itself
# ---------------------------------------------------------------------------


def test_the_latch_reports_a_name_once():
    latch = SkipLatch()
    assert latch.should_log("a.jpg") is True
    assert latch.should_log("a.jpg") is False
    assert latch.should_log("b.jpg") is True


def test_forget_lets_a_re_dropped_filename_report_again():
    latch = SkipLatch()
    latch.should_log("a.jpg")
    latch.forget("a.jpg")
    assert latch.should_log("a.jpg") is True


def test_the_latch_is_bounded_and_says_when_it_resets():
    """Unbounded growth in a long-running daemon is a slow leak; the reset
    is announced because repeated lines will follow it."""
    latch = SkipLatch(max_entries=3)
    for i in range(3):
        latch.should_log(f"f{i}.jpg")
    with structlog.testing.capture_logs() as captured:
        assert latch.should_log("f3.jpg") is True
    events = [c for c in captured if c.get("event") == "watcher.skip_latch_reset"]
    assert len(events) == 1
    assert events[0]["max_entries"] == 3
    # ...and after the reset an earlier name reports once more, as promised.
    assert latch.should_log("f0.jpg") is True


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_the_config_default_matches_the_module_default():
    """DRIFT PIN. The config default is the operator-facing one and the
    module constant is the direct-construction one; they must agree or the
    daemon and a bare InboxWatcher admit different things."""
    assert tuple(WatcherConfig().ingestable_extensions) == \
        DEFAULT_INGESTABLE_EXTENSIONS


@pytest.mark.parametrize("raw,expected", [
    (["md"], [".md"]),
    ([".MD"], [".md"]),
    ([" .txt "], [".txt"]),
    (["md", ".md"], [".md"]),
    (["md", "", None], [".md"]),
])
def test_config_values_are_normalized(raw, expected):
    """``[md, .TXT]`` must work — normalizing at the dataclass rather than
    the call site means a future caller cannot forget to do it."""
    assert WatcherConfig(ingestable_extensions=raw).ingestable_extensions == \
        expected


def test_an_operator_can_widen_the_set_via_config():
    cfg = WatcherConfig(ingestable_extensions=[".md", ".csv", "json"])
    assert cfg.ingestable_extensions == [".md", ".csv", ".json"]
