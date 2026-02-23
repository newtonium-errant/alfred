"""Live TUI dashboard for ``alfred up --live``."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WorkerInfo:
    name: str
    status: str = "pending"     # pending | running | stopped | restarting
    pid: int | None = None
    restart_count: int = 0
    exit_code: int | None = None


@dataclass
class ActivityEntry:
    timestamp: str
    tool: str
    level: str
    event: str
    detail: str = ""


@dataclass
class MutationEntry:
    timestamp: str
    tool: str
    op: str
    path: str


@dataclass
class ToolStats:
    curator_processed: int = 0
    curator_last_run: str = ""
    janitor_tracked: int = 0
    janitor_issues: int = 0
    janitor_sweeps: int = 0
    distiller_sources: int = 0
    distiller_learnings: int = 0
    distiller_runs: int = 0
    surveyor_tracked: int = 0
    surveyor_clusters: int = 0
    surveyor_last_run: str = ""


@dataclass
class DashboardData:
    workers: dict[str, WorkerInfo] = field(default_factory=dict)
    activity: deque[ActivityEntry] = field(default_factory=lambda: deque(maxlen=100))
    mutations: deque[MutationEntry] = field(default_factory=lambda: deque(maxlen=50))
    stats: ToolStats = field(default_factory=ToolStats)
    start_time: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)


# ---------------------------------------------------------------------------
# ANSI stripping
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Log line parser — structlog ConsoleRenderer format
# ---------------------------------------------------------------------------

# Example: "2024-06-01T15:05:32 [info     ] daemon.starting        tool=curator"
_LOG_RE = re.compile(
    r"^(\S+)\s+"           # timestamp (ISO or HH:MM:SS)
    r"\[(\w+)\s*\]\s+"     # [level]
    r"(\S+)"               # event name
    r"(.*)$"               # rest (detail)
)


def _parse_log_line(line: str, tool: str) -> ActivityEntry | None:
    line = _strip_ansi(line).strip()
    if not line:
        return None
    m = _LOG_RE.match(line)
    if not m:
        return None
    ts_raw, level, event, detail = m.groups()
    # Extract just HH:MM:SS from the timestamp
    ts = ts_raw
    if "T" in ts_raw:
        ts = ts_raw.split("T", 1)[1][:8]
    return ActivityEntry(
        timestamp=ts,
        tool=tool,
        level=level.strip(),
        event=event.strip(),
        detail=detail.strip(),
    )


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------

class LogTailThread(threading.Thread):
    """Tails data/{tool}.log files and feeds parsed entries into DashboardData."""

    def __init__(self, data: DashboardData, log_dir: Path, tools: list[str]):
        super().__init__(daemon=True)
        self._data = data
        self._log_dir = log_dir
        self._tools = tools
        self._positions: dict[str, int] = {}  # tool -> file offset
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            for tool in self._tools:
                path = self._log_dir / f"{tool}.log"
                if not path.exists():
                    continue
                try:
                    size = path.stat().st_size
                    pos = self._positions.get(tool, 0)
                    # Handle file truncation (log rotation)
                    if size < pos:
                        pos = 0
                    if size == pos:
                        continue
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        new_text = f.read()
                        self._positions[tool] = f.tell()
                    for line in new_text.splitlines():
                        entry = _parse_log_line(line, tool)
                        if entry:
                            with self._data.lock:
                                self._data.activity.appendleft(entry)
                except OSError:
                    continue
            self._stop.wait(0.5)

    def stop(self) -> None:
        self._stop.set()


class AuditTailThread(threading.Thread):
    """Tails data/vault_audit.log and feeds MutationEntry into DashboardData."""

    def __init__(self, data: DashboardData, audit_path: Path):
        super().__init__(daemon=True)
        self._data = data
        self._audit_path = audit_path
        self._position: int = 0
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            if self._audit_path.exists():
                try:
                    size = self._audit_path.stat().st_size
                    if size < self._position:
                        self._position = 0
                    if size > self._position:
                        with open(self._audit_path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(self._position)
                            new_text = f.read()
                            self._position = f.tell()
                        for line in new_text.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            ts_raw = entry.get("ts", "")
                            ts = ts_raw
                            if "T" in ts_raw:
                                ts = ts_raw.split("T", 1)[1][:8]
                            me = MutationEntry(
                                timestamp=ts,
                                tool=entry.get("tool", "?"),
                                op=entry.get("op", "?"),
                                path=entry.get("path", ""),
                            )
                            with self._data.lock:
                                self._data.mutations.appendleft(me)
                except OSError:
                    pass
            self._stop.wait(2.0)

    def stop(self) -> None:
        self._stop.set()


class StatReaderThread(threading.Thread):
    """Reads data/*_state.json periodically and updates ToolStats."""

    def __init__(self, data: DashboardData, state_dir: Path):
        super().__init__(daemon=True)
        self._data = data
        self._state_dir = state_dir
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            self._read_all()
            self._stop.wait(10.0)

    def _read_all(self) -> None:
        s = self._data.stats

        # Curator state
        self._read_curator(s)
        self._read_janitor(s)
        self._read_distiller(s)
        self._read_surveyor(s)

    def _load_json(self, name: str) -> dict[str, Any] | None:
        path = self._state_dir / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _read_curator(self, s: ToolStats) -> None:
        data = self._load_json("curator_state.json")
        if not data:
            return
        with self._data.lock:
            processed = data.get("processed", {})
            s.curator_processed = len(processed)
            s.curator_last_run = data.get("last_run", "") or ""

    def _read_janitor(self, s: ToolStats) -> None:
        data = self._load_json("janitor_state.json")
        if not data:
            return
        with self._data.lock:
            files = data.get("files", {})
            s.janitor_tracked = len(files)
            s.janitor_issues = sum(
                1 for f in files.values()
                if isinstance(f, dict) and f.get("open_issues")
            )
            s.janitor_sweeps = len(data.get("sweeps", []))

    def _read_distiller(self, s: ToolStats) -> None:
        data = self._load_json("distiller_state.json")
        if not data:
            return
        with self._data.lock:
            files = data.get("files", {})
            s.distiller_sources = len(files)
            s.distiller_learnings = sum(
                len(f.get("learn_records_created", []))
                for f in files.values()
                if isinstance(f, dict)
            )
            s.distiller_runs = len(data.get("runs", []))

    def _read_surveyor(self, s: ToolStats) -> None:
        data = self._load_json("surveyor_state.json")
        if not data:
            return
        with self._data.lock:
            s.surveyor_tracked = len(data.get("files", {}))
            s.surveyor_clusters = len(data.get("clusters", {}))
            s.surveyor_last_run = data.get("last_run", "") or ""

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

TOOL_COLORS = {
    "curator": "cyan",
    "janitor": "yellow",
    "distiller": "magenta",
    "surveyor": "green",
}

STATUS_STYLE = {
    "running":    ("bold green",  "\u25cf"),   # ● green
    "pending":    ("dim",         "\u25cb"),    # ○ dim
    "stopped":    ("bold red",    "\u25cf"),    # ● red
    "restarting": ("bold yellow", "\u25cf"),    # ● yellow
}


def render_workers(data: DashboardData, tools: list[str]) -> Panel:
    table = Table(expand=True, box=None, pad_edge=False)
    table.add_column("Tool", style="bold", min_width=10)
    table.add_column("Status", min_width=14)
    table.add_column("PID", justify="right", min_width=6)
    table.add_column("Retry", justify="right", min_width=5)

    with data.lock:
        for tool in tools:
            w = data.workers.get(tool)
            if not w:
                continue
            style, symbol = STATUS_STYLE.get(w.status, ("dim", "?"))
            status_text = Text(f"{symbol} {w.status}", style=style)
            pid_str = str(w.pid) if w.pid else "--"
            table.add_row(tool, status_text, pid_str, str(w.restart_count))

    return Panel(table, title="Workers", border_style="blue")


def render_stats(data: DashboardData, tools: list[str]) -> Panel:
    table = Table(expand=True, box=None, pad_edge=False)
    table.add_column("Tool", style="bold", min_width=10)
    table.add_column("Metrics", min_width=30)

    with data.lock:
        s = data.stats
        if "curator" in tools:
            last = _short_ago(s.curator_last_run) if s.curator_last_run else "never"
            table.add_row(
                Text("curator", style="cyan"),
                f"{s.curator_processed} processed  last: {last}",
            )
        if "janitor" in tools:
            table.add_row(
                Text("janitor", style="yellow"),
                f"{s.janitor_tracked} tracked  {s.janitor_issues} issues  {s.janitor_sweeps} sweeps",
            )
        if "distiller" in tools:
            table.add_row(
                Text("distiller", style="magenta"),
                f"{s.distiller_sources} sources  {s.distiller_learnings} learnings  {s.distiller_runs} runs",
            )
        if "surveyor" in tools:
            last = _short_ago(s.surveyor_last_run) if s.surveyor_last_run else "never"
            table.add_row(
                Text("surveyor", style="green"),
                f"{s.surveyor_tracked} tracked  {s.surveyor_clusters} clusters  last: {last}",
            )

    return Panel(table, title="Stats", border_style="blue")


def render_activity(data: DashboardData, max_lines: int = 20) -> Panel:
    text = Text()
    with data.lock:
        entries = list(data.activity)[:max_lines]
    for i, e in enumerate(entries):
        if i > 0:
            text.append("\n")
        color = TOOL_COLORS.get(e.tool, "white")
        text.append(f"{e.timestamp} ", style="dim")
        text.append(f"{e.tool:<10}", style=color)
        text.append(f"{e.event}", style="bold")
        if e.detail:
            text.append(f"  {e.detail}", style="dim")
    if not entries:
        text.append("Waiting for activity...", style="dim italic")
    return Panel(text, title="Activity", border_style="blue")


def render_mutations(data: DashboardData, max_lines: int = 20) -> Panel:
    text = Text()
    OP_SYMBOLS = {"create": "+", "modify": "~", "delete": "-"}
    OP_STYLES = {"create": "green", "modify": "yellow", "delete": "red"}
    with data.lock:
        entries = list(data.mutations)[:max_lines]
    for i, m in enumerate(entries):
        if i > 0:
            text.append("\n")
        sym = OP_SYMBOLS.get(m.op, "?")
        style = OP_STYLES.get(m.op, "white")
        text.append(f"{m.timestamp} ", style="dim")
        text.append(f"{sym} ", style=f"bold {style}")
        text.append(m.path, style=style)
    if not entries:
        text.append("No vault mutations yet...", style="dim italic")
    return Panel(text, title="Vault Mutations", border_style="blue")


def render_footer(data: DashboardData) -> Text:
    elapsed = time.time() - data.start_time
    mins, secs = divmod(int(elapsed), 60)
    hours, mins = divmod(mins, 60)
    if hours:
        uptime = f"{hours}h {mins:02d}m {secs:02d}s"
    else:
        uptime = f"{mins}m {secs:02d}s"

    with data.lock:
        active = sum(1 for w in data.workers.values() if w.status == "running")
        total = len(data.workers)

    footer = Text()
    footer.append(f" Uptime: {uptime}", style="bold")
    footer.append(f"  |  {active}/{total} workers active", style="bold")
    footer.append("  |  Ctrl+C to stop", style="dim")
    return footer


def _short_ago(iso_ts: str) -> str:
    """Convert an ISO timestamp to a short 'Xm ago' string."""
    if not iso_ts:
        return "never"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        elif secs < 3600:
            return f"{secs // 60}m ago"
        else:
            return f"{secs // 3600}h ago"
    except (ValueError, TypeError):
        return iso_ts[:19]


def build_layout(data: DashboardData, tools: list[str]) -> Layout:
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="top", size=3 + len(tools) + 1),   # panel border + rows + padding
        Layout(name="bottom"),
        Layout(name="footer", size=1),
    )
    layout["top"].split_row(
        Layout(render_workers(data, tools), name="workers"),
        Layout(render_stats(data, tools), name="stats"),
    )
    layout["bottom"].split_row(
        Layout(render_activity(data), name="activity", ratio=3),
        Layout(render_mutations(data), name="mutations", ratio=2),
    )
    layout["footer"].update(render_footer(data))
    return layout


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_live_dashboard(
    tools: list[str],
    processes: dict[str, Any],          # multiprocessing.Process objects
    restart_counts: dict[str, int],
    start_process: Callable[[str], Any],
    sentinel_path: Path | None,
    log_dir: Path,
    state_dir: Path,
    max_restarts: int = 5,
    missing_deps_exit: int = 78,
) -> None:
    """Run the Rich Live dashboard until Ctrl+C or sentinel file."""
    import multiprocessing  # noqa: for type hints in this scope

    data = DashboardData()
    data.start_time = time.time()

    # Initialize worker info
    for tool in tools:
        p = processes.get(tool)
        data.workers[tool] = WorkerInfo(
            name=tool,
            status="running" if (p and p.is_alive()) else "pending",
            pid=p.pid if p else None,
            restart_count=restart_counts.get(tool, 0),
        )

    # Start background threads
    log_tail = LogTailThread(data, log_dir, tools)
    audit_tail = AuditTailThread(data, log_dir / "vault_audit.log")
    stat_reader = StatReaderThread(data, state_dir)
    log_tail.start()
    audit_tail.start()
    stat_reader.start()

    active_tools = list(tools)

    try:
        with Live(build_layout(data, active_tools), screen=True, refresh_per_second=4) as live:
            while True:
                # Check sentinel file (alfred down)
                if sentinel_path and sentinel_path.exists():
                    break

                # Update worker health
                with data.lock:
                    for tool in list(active_tools):
                        p = processes.get(tool)
                        if not p:
                            continue
                        w = data.workers[tool]
                        if p.is_alive():
                            w.status = "running"
                            w.pid = p.pid
                        else:
                            exit_code = p.exitcode
                            w.exit_code = exit_code
                            w.pid = None

                            if exit_code == missing_deps_exit:
                                w.status = "stopped"
                                active_tools = [t for t in active_tools if t != tool]
                                continue

                            restart_counts[tool] = restart_counts.get(tool, 0) + 1
                            w.restart_count = restart_counts[tool]

                            if restart_counts[tool] <= max_restarts:
                                w.status = "restarting"
                            else:
                                w.status = "stopped"
                                continue

                # Restart dead workers (outside the lock)
                for tool in list(active_tools):
                    w = data.workers.get(tool)
                    if w and w.status == "restarting":
                        new_p = start_process(tool)
                        processes[tool] = new_p
                        with data.lock:
                            w.status = "running"
                            w.pid = new_p.pid

                live.update(build_layout(data, active_tools))
                time.sleep(0.25)

    except KeyboardInterrupt:
        pass
    finally:
        log_tail.stop()
        audit_tail.stop()
        stat_reader.stop()
