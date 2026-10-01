"""
phase_timer.py -- Deterministic phase-and-token instrument for analysis sessions.

Purpose: measure WHICH phase of an EBPM analysis session costs time and tokens,
and the interval structure between phases.

Two independent measurement paths live here:

1. Timing (the original tool). Events are appended to a JSON Lines log as the
   session runs; `report` summarises them.
2. Token usage (`usage`). Read AFTER a session, from the Claude Code transcript
   that the harness already writes to
   ``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` plus the per-spawn
   files under ``<session-id>/subagents/*.jsonl``. Every assistant turn in those
   files carries a ``message.usage`` object, so real billed token counts are
   recoverable without instrumenting the agents themselves.

Both paths are deterministic given their inputs (file contents + clock); they
make no LLM calls and no network access.

Events are appended to a JSON Lines log (one JSON object per line). Each event
carries an ISO 8601 UTC timestamp. The tool is fully deterministic given its
inputs (file contents + clock); it makes no LLM calls and no network access.

Log file (default):
    <analysis layer root>/output/.phase_timing.jsonl
computed relative to this module (tools/ -> ../output/...). Every
subcommand accepts --file <path> to override the location (used by tests so the
real output directory is never touched). Parent directories are created if
missing.

Subcommands:
    start       --phase "<name>" [--note "<text>"]
    end         --phase "<name>" [--note "<text>"]
    task-start  --task  "<name>" [--note "<text>"]
    task-end    --task  "<name>" [--note "<text>"]
    gate-open   --gate  "<id>"   [--note "<text>"]
    gate-close  --gate  "<id>"   [--note "<text>"]
    mark        --label "<text>"
    report      [--file <path>] [--project-dir <path>] [--session <id>]
    reset       [--file <path>]
    usage       [--file <path>] [--project-dir <path>] [--session <id>] [--json]

Phase names, task names, and gate ids are arbitrary strings; no specific set is
hardcoded.

Per-task events (task_start / task_end) live in a dedicated event namespace so
that the per-phase summary is untouched. They let a reader determine whether
the independent tasks of a phase ran concurrently (parallel) or sequentially
(serial): `report` shows a per-task table with an overlap indicator and a
"max observed task concurrency" verdict.

Those two events are recorded by hand, so they can be recorded at the wrong
moment -- both at the end, once the work is already done, which measures the
gap between two CLI calls instead of the task. Intervals under
MIN_PLAUSIBLE_TASK_SEC are therefore marked suspect rather than presented as
durations, and `report` prints the same work measured from the transcript
(per-spawn wall-clock), which no agent has to remember to record.

Gate events (gate_open / gate_close) measure human-approval intervals: the PM
brackets each human gate with a gate_open right before presenting it and a
gate_close the instant the human responds. The interval between is human
thinking time, so `report` can separate agent compute time from human gate time
(agent compute = overall wall-clock minus the human-gate total).

A gate is not the only way a session stops. A human can also interrupt a phase
part-way and come back later, and nothing in the timing log records that. Given
`--project-dir`/`--session`, `report` reads those spans out of the transcript
instead -- an interruption record, or a stretch with no event at all for longer
than IDLE_GAP_THRESHOLD_SEC -- and reports, for every phase and for the session,
both the wall clock (`elapsed_sec`) and the working time left after deducting
gates and idle spans (`active_sec`). Neither figure replaces the other.

Exit codes:
    0  Success.
    1  Reserved for violations / generic errors.
    2  report encountered a corrupted log line (invalid JSON).
    3  usage could not locate a transcript to read.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default log file, relative to this module (tools/ -> <layer>/output/).
DEFAULT_LOG_PATH = Path(__file__).parent.parent / "output" / ".phase_timing.jsonl"

#: Trailing note printed by `report`, pointing at the token-usage subcommand.
_TOKEN_NOTE = (
    "Note: token usage is not captured by these events. Run "
    "`phase_timer.py usage` after the session for real billed token counts."
)

#: Where Claude Code writes per-project session transcripts.
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Prompt-caching price multipliers, relative to the model's base input price.
# A 5-minute cache write costs 1.25x base input, a 1-hour write 2x, and a
# cache read 0.1x. These are exact ratios, so the "billable input" figure they
# produce is model-agnostic; only the USD conversion below needs a price table.
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_READ_MULTIPLIER = 0.1

#: USD per million tokens, as (input, output), keyed by model id.
#: A model missing from this table is never counted as $0 -- its calls are
#: tallied separately and reported as unpriced, so the total can never be
#: silently understated by a model id we have not looked up.
#:
#: Rates below were read from the Anthropic model/pricing documentation
#: (platform.claude.com "Models overview") and confirmed on 2026-08-11.
#: Add a model only after confirming its rate there; never guess.
#: A task interval shorter than this is not a fast task; it is a missing
#: measurement, and is reported as such rather than as a duration.
#:
#: In 260810_01 two analysis tasks were logged as 0.11 s and 0.25 s. The
#: transcripts show why: in both cases the sub-agent had not recorded a
#: task-start when the work began, then issued task-start and task-end in a
#: single shell command once the work was already finished. What got measured
#: was the gap between two CLI invocations, not the task -- the spawns
#: themselves ran 10 and 13 minutes. No task in this system does real work in
#: under five seconds, so anything below that is the same mistake.
MIN_PLAUSIBLE_TASK_SEC = 5.0

#: A stretch of transcript with no event at all for longer than this is counted
#: as idle -- the session was waiting on a human, not computing.
#:
#: Value chosen from the two baseline runs. Measuring every gap between
#: consecutive transcript events (main thread and sub-agent files merged), the
#: longest gap that was genuinely one agent turn was about 6 minutes: a
#: sub-agent thinking through a single long reply writes nothing while it works.
#: The human-away gaps in the same runs were 25 and 113 minutes. Ten minutes
#: sits in the empty band between the two, so the rule never eats real compute
#: and still catches the interruptions it exists for.
IDLE_GAP_THRESHOLD_SEC = 600.0

#: Prefix of the record Claude Code writes when the human interrupts a run
#: (both "[Request interrupted by user]" and the "... for tool use]" variant).
#: Everything from that record until the next event of any kind is idle,
#: whatever its length: the session stopped because a human stopped it.
INTERRUPT_MARKER = "[Request interrupted by user"

#: Characters this tool prints (and, more often, characters that arrive inside a
#: sub-agent description read from a transcript) are not all encodable in the
#: terminal's code page. On a Japanese Windows console (cp932) an em dash alone
#: was enough to raise UnicodeEncodeError part-way through the `usage` tables,
#: so the run died before the Total line. :func:`_make_stdio_safe` disarms that.
_STDIO_ERROR_HANDLER = "replace"

MODEL_PRICING_USD_PER_MTOK = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stdio_safe() -> None:
    """Make stdout/stderr incapable of dying on an unencodable character.

    Only the error handler is changed; the stream keeps the terminal's own
    encoding. Forcing UTF-8 instead would be worse here: the sub-agent
    descriptions this tool prints are Japanese, and cp932 renders them
    correctly, so re-encoding them as UTF-8 would turn readable text into
    mojibake on the very console the fix is for. With ``errors="replace"`` the
    Japanese survives and only the handful of characters cp932 lacks (an em
    dash, an arrow) degrade to '?'.

    Doing this in the tool removes the need to set PYTHONIOENCODING around
    every call site.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - stream already replaced
            continue
        try:
            reconfigure(errors=_STDIO_ERROR_HANDLER)
        except (ValueError, OSError):  # pragma: no cover - detached stream
            continue


def _now_iso() -> str:
    """Return the current time as an ISO 8601 string in UTC (seconds + offset)."""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp produced by :func:`_now_iso`.

    Args:
        ts: ISO 8601 timestamp string.

    Returns:
        A timezone-aware ``datetime``.
    """
    return datetime.fromisoformat(ts)


def _resolve_path(file_arg: str | None) -> Path:
    """Return the active log path (``--file`` override or default)."""
    if file_arg:
        return Path(file_arg)
    return DEFAULT_LOG_PATH


def _append_event(path: Path, event: dict[str, object]) -> None:
    """Append a single event object as one JSON line, creating parents.

    Args:
        path: Destination log file.
        event: Event object to serialise.

    Raises:
        SystemExit(1): If the file cannot be written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"Failed to write log file '{path}': {exc}", file=sys.stderr)
        sys.exit(1)


def _read_events(path: Path) -> list[dict[str, object]]:
    """Read and parse all events from the log file.

    Args:
        path: Log file path.

    Returns:
        A list of event objects in file order. An empty list if the file does
        not exist.

    Raises:
        SystemExit(2): If any non-blank line is not valid JSON.
    """
    if not path.exists():
        return []

    events: list[dict[str, object]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        print(f"Failed to read log file '{path}': {exc}", file=sys.stderr)
        sys.exit(1)

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            print(
                f"Corrupted log file '{path}' at line {lineno}: invalid JSON "
                f"({exc.msg}).",
                file=sys.stderr,
            )
            sys.exit(2)
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _elapsed_for_end(events: list[dict[str, object]], phase: str) -> float | None:
    """Compute elapsed seconds for an `end` against the most recent open `start`.

    The matching start is the latest `start` event for *phase* that does not yet
    have a corresponding `end`. If no such start exists, returns ``None``.

    Args:
        events: All events recorded so far (file order).
        phase: Phase name to match.

    Returns:
        Elapsed seconds (>= 0.0) as a float, or ``None`` if unmatched.
    """
    open_start_ts: str | None = None
    for ev in events:
        if ev.get("phase") != phase:
            continue
        if ev.get("event") == "start":
            open_start_ts = ev.get("ts")  # type: ignore[assignment]
        elif ev.get("event") == "end":
            open_start_ts = None  # this start is now closed
    if not open_start_ts:
        return None
    delta = (_now_iso_dt() - _parse_iso(open_start_ts)).total_seconds()
    return max(0.0, delta)


def _elapsed_for_task_end(events: list[dict[str, object]], task: str) -> float | None:
    """Compute elapsed seconds for a `task_end` against its most recent open start.

    Mirrors :func:`_elapsed_for_end` but is keyed on the ``task`` field and the
    ``task_start`` / ``task_end`` event pair. The matching start is the latest
    ``task_start`` for *task* that does not yet have a corresponding
    ``task_end``. If no such start exists, returns ``None``.

    Args:
        events: All events recorded so far (file order).
        task: Task name to match.

    Returns:
        Elapsed seconds (>= 0.0) as a float, or ``None`` if unmatched.
    """
    open_start_ts: str | None = None
    for ev in events:
        if ev.get("task") != task:
            continue
        if ev.get("event") == "task_start":
            open_start_ts = ev.get("ts")  # type: ignore[assignment]
        elif ev.get("event") == "task_end":
            open_start_ts = None  # this start is now closed
    if not open_start_ts:
        return None
    delta = (_now_iso_dt() - _parse_iso(open_start_ts)).total_seconds()
    return max(0.0, delta)


def _elapsed_for_gate_close(events: list[dict[str, object]], gate: str) -> float | None:
    """Compute elapsed seconds for a `gate_close` against its most recent open.

    Mirrors :func:`_elapsed_for_end` but is keyed on the ``gate`` field and the
    ``gate_open`` / ``gate_close`` event pair. The matching open is the latest
    ``gate_open`` for *gate* that does not yet have a corresponding
    ``gate_close``. If no such open exists, returns ``None``.

    Args:
        events: All events recorded so far (file order).
        gate: Gate id to match.

    Returns:
        Elapsed seconds (>= 0.0) as a float, or ``None`` if unmatched.
    """
    open_start_ts: str | None = None
    for ev in events:
        if ev.get("gate") != gate:
            continue
        if ev.get("event") == "gate_open":
            open_start_ts = ev.get("ts")  # type: ignore[assignment]
        elif ev.get("event") == "gate_close":
            open_start_ts = None  # this open is now closed
    if not open_start_ts:
        return None
    delta = (_now_iso_dt() - _parse_iso(open_start_ts)).total_seconds()
    return max(0.0, delta)


def _now_iso_dt() -> datetime:
    """Return the current UTC time as a ``datetime`` (paired with _now_iso)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_start(path: Path, phase: str, note: str | None) -> int:
    """Record a `start` event for *phase*."""
    event = {
        "event": "start",
        "phase": phase,
        "note": note,
        "ts": _now_iso(),
    }
    _append_event(path, event)
    return 0


def cmd_end(path: Path, phase: str, note: str | None) -> int:
    """Record an `end` event for *phase* with computed elapsed seconds.

    Elapsed is measured against the most recent open `start` of the same phase.
    If no matching start exists, ``elapsed_sec`` is ``None`` and the event is
    still written (no crash).
    """
    prior = _read_events(path)
    elapsed = _elapsed_for_end(prior, phase)
    event = {
        "event": "end",
        "phase": phase,
        "note": note,
        "ts": _now_iso(),
        "elapsed_sec": elapsed,
    }
    _append_event(path, event)
    return 0


def cmd_task_start(path: Path, task: str, note: str | None) -> int:
    """Record a `task_start` event for *task* in the per-task namespace."""
    event = {
        "event": "task_start",
        "task": task,
        "note": note,
        "ts": _now_iso(),
    }
    _append_event(path, event)
    return 0


def cmd_task_end(path: Path, task: str, note: str | None) -> int:
    """Record a `task_end` event for *task* with computed elapsed seconds.

    Elapsed is measured against the most recent open `task_start` of the same
    task. If no matching start exists, ``elapsed_sec`` is ``None`` and the event
    is still written (no crash) -- same non-blocking contract as `end`.

    An interval under :data:`MIN_PLAUSIBLE_TASK_SEC` is marked ``suspect`` in
    the event and called out on stderr. Writing it as a plain number would hand
    the reader a duration that looks measured and is not; the operator needs to
    know, at the moment it happens, that the start was never recorded when the
    work began. The exit code stays 0 either way -- measurement never blocks
    the analysis.
    """
    prior = _read_events(path)
    elapsed = _elapsed_for_task_end(prior, task)
    event: dict[str, object] = {
        "event": "task_end",
        "task": task,
        "note": note,
        "ts": _now_iso(),
        "elapsed_sec": elapsed,
    }
    if elapsed is None:
        print(
            f"Warning: task-end '{task}' has no open task-start in '{path}', so "
            "its duration is unknown. Record task-start when the work begins, "
            "and pass the same --file to both calls.",
            file=sys.stderr,
        )
    elif elapsed < MIN_PLAUSIBLE_TASK_SEC:
        event["suspect"] = "interval_below_min_plausible"
        print(
            f"Warning: task '{task}' measured {elapsed:.3f} sec, under the "
            f"{MIN_PLAUSIBLE_TASK_SEC:.0f} sec floor. This is what recording "
            "task-start and task-end together, after the work, looks like -- "
            "the number is the gap between two CLI calls, not the task. It is "
            "marked 'suspect' in the log and in `report`.",
            file=sys.stderr,
        )
    _append_event(path, event)
    return 0


def cmd_gate_open(path: Path, gate: str, note: str | None) -> int:
    """Record a `gate_open` event for *gate* (human-approval interval start)."""
    event = {
        "event": "gate_open",
        "gate": gate,
        "note": note,
        "ts": _now_iso(),
    }
    _append_event(path, event)
    return 0


def cmd_gate_close(path: Path, gate: str, note: str | None) -> int:
    """Record a `gate_close` event for *gate* with computed elapsed seconds.

    Elapsed is measured against the most recent open `gate_open` of the same
    gate id. If no matching open exists, ``elapsed_sec`` is ``None`` and the
    event is still written (no crash) -- same non-blocking contract as `end`.
    """
    prior = _read_events(path)
    elapsed = _elapsed_for_gate_close(prior, gate)
    event = {
        "event": "gate_close",
        "gate": gate,
        "note": note,
        "ts": _now_iso(),
        "elapsed_sec": elapsed,
    }
    _append_event(path, event)
    return 0


def cmd_mark(path: Path, label: str) -> int:
    """Record a `mark` event with a free-text label."""
    event = {
        "event": "mark",
        "label": label,
        "ts": _now_iso(),
    }
    _append_event(path, event)
    return 0


def cmd_reset(path: Path) -> int:
    """Truncate/initialise the log file to empty (create parents if missing)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("")
    except OSError as exc:
        print(f"Failed to reset log file '{path}': {exc}", file=sys.stderr)
        sys.exit(1)
    return 0


def _summarise(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Build a per-phase summary, pairing each `start` with its next `end`.

    Phases are reported in the order their `start` events first appear. A phase
    may appear multiple times only if it was started/ended multiple times; each
    open start is paired with the next end of the same phase.

    Args:
        events: All parsed events in file order.

    Returns:
        A list of dicts: ``{"phase", "start_ts", "end_ts", "elapsed_sec"}``.
    """
    summary: list[dict[str, object]] = []
    open_index: dict[str, int] = {}  # phase -> index into summary of open row

    for ev in events:
        etype = ev.get("event")
        phase = ev.get("phase")
        if etype == "start" and isinstance(phase, str):
            summary.append(
                {
                    "phase": phase,
                    "start_ts": ev.get("ts"),
                    "end_ts": None,
                    "elapsed_sec": None,
                }
            )
            open_index[phase] = len(summary) - 1
        elif etype == "end" and isinstance(phase, str):
            idx = open_index.pop(phase, None)
            if idx is not None:
                summary[idx]["end_ts"] = ev.get("ts")
                summary[idx]["elapsed_sec"] = ev.get("elapsed_sec")
            else:
                # End with no open start: record a standalone row.
                summary.append(
                    {
                        "phase": phase,
                        "start_ts": None,
                        "end_ts": ev.get("ts"),
                        "elapsed_sec": ev.get("elapsed_sec"),
                    }
                )
    return summary


def _summarise_tasks(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Build a per-task summary, pairing each `task_start` with its next end.

    Tasks are reported in the order their `task_start` events first appear. Each
    open ``task_start`` is paired with the next ``task_end`` of the same task. A
    ``task_end`` with no open start is recorded as a standalone (open) row.

    Args:
        events: All parsed events in file order.

    Returns:
        A list of dicts: ``{"task", "start_ts", "end_ts", "elapsed_sec"}``.
    """
    summary: list[dict[str, object]] = []
    open_index: dict[str, int] = {}  # task -> index into summary of open row

    for ev in events:
        etype = ev.get("event")
        task = ev.get("task")
        if etype == "task_start" and isinstance(task, str):
            summary.append(
                {
                    "task": task,
                    "start_ts": ev.get("ts"),
                    "end_ts": None,
                    "elapsed_sec": None,
                }
            )
            open_index[task] = len(summary) - 1
        elif etype == "task_end" and isinstance(task, str):
            idx = open_index.pop(task, None)
            if idx is not None:
                summary[idx]["end_ts"] = ev.get("ts")
                summary[idx]["elapsed_sec"] = ev.get("elapsed_sec")
            else:
                # End with no open start: standalone (open) row.
                summary.append(
                    {
                        "task": task,
                        "start_ts": None,
                        "end_ts": ev.get("ts"),
                        "elapsed_sec": ev.get("elapsed_sec"),
                    }
                )
    return summary


def _summarise_gates(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """Build a per-gate summary, pairing each `gate_open` with its next close.

    Gates are reported in the order their `gate_open` events first appear. Each
    open ``gate_open`` is paired with the next ``gate_close`` of the same gate
    id. A ``gate_close`` with no open is recorded as a standalone (open) row.

    Args:
        events: All parsed events in file order.

    Returns:
        A list of dicts: ``{"gate", "open_ts", "close_ts", "elapsed_sec"}``.
    """
    summary: list[dict[str, object]] = []
    open_index: dict[str, int] = {}  # gate -> index into summary of open row

    for ev in events:
        etype = ev.get("event")
        gate = ev.get("gate")
        if etype == "gate_open" and isinstance(gate, str):
            summary.append(
                {
                    "gate": gate,
                    "open_ts": ev.get("ts"),
                    "close_ts": None,
                    "elapsed_sec": None,
                }
            )
            open_index[gate] = len(summary) - 1
        elif etype == "gate_close" and isinstance(gate, str):
            idx = open_index.pop(gate, None)
            if idx is not None:
                summary[idx]["close_ts"] = ev.get("ts")
                summary[idx]["elapsed_sec"] = ev.get("elapsed_sec")
            else:
                # Close with no open: standalone (open) row.
                summary.append(
                    {
                        "gate": gate,
                        "open_ts": None,
                        "close_ts": ev.get("ts"),
                        "elapsed_sec": ev.get("elapsed_sec"),
                    }
                )
    return summary


def _closed_interval(
    row: dict[str, object],
    start_key: str = "start_ts",
    end_key: str = "end_ts",
) -> tuple[datetime, datetime] | None:
    """Return a (start, end) datetime interval for a closed summary row, else None.

    Used for phase rows and task rows (``start_ts``/``end_ts``) and for gate rows
    (``open_ts``/``close_ts``). Rows with a missing or unparseable timestamp at
    either end (open intervals) return ``None`` so they are excluded from
    overlap/concurrency math.
    """
    start_ts = row.get(start_key)
    end_ts = row.get(end_key)
    if not isinstance(start_ts, str) or not isinstance(end_ts, str):
        return None
    try:
        start = _parse_iso(start_ts)
        end = _parse_iso(end_ts)
    except ValueError:
        return None
    return (start, end)


def _task_overlaps(
    intervals: list[tuple[datetime, datetime] | None],
) -> list[bool]:
    """Per-row flag: does this closed interval overlap any *other* closed one.

    Two intervals [a_s, a_e] and [b_s, b_e] overlap when ``a_s < b_e`` and
    ``b_s < a_e`` (touching endpoints alone do not count as overlap). ``None``
    entries (open intervals) are never flagged.
    """
    flags = [False] * len(intervals)
    for i, a in enumerate(intervals):
        if a is None:
            continue
        for j, b in enumerate(intervals):
            if i == j or b is None:
                continue
            if a[0] < b[1] and b[0] < a[1]:
                flags[i] = True
                break
    return flags


def _max_concurrency(
    intervals: list[tuple[datetime, datetime] | None],
) -> int:
    """Return the peak number of closed task intervals open at one instant.

    Uses a sweep over start/end events. ``None`` entries (open intervals) are
    ignored. End events are processed before start events at an identical
    timestamp so that adjacent (non-overlapping) intervals do not inflate the
    count. Returns 0 when there are no closed intervals.
    """
    points: list[tuple[datetime, int]] = []
    for itv in intervals:
        if itv is None:
            continue
        # delta -1 (end) sorts before +1 (start) at the same timestamp.
        points.append((itv[1], -1))
        points.append((itv[0], +1))
    points.sort(key=lambda p: (p[0], p[1]))

    current = 0
    peak = 0
    for _, delta in points:
        current += delta
        peak = max(peak, current)
    return peak


def _all_timestamps(events: list[dict[str, object]]) -> list[datetime]:
    """Extract all parseable timestamps from events, in no particular order."""
    out: list[datetime] = []
    for ev in events:
        ts = ev.get("ts")
        if isinstance(ts, str):
            try:
                out.append(_parse_iso(ts))
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------------------
# Span algebra: separating wall-clock time from working time
#
# Two kinds of interval are not working time: a human gate (already recorded
# explicitly by gate_open/gate_close) and an idle span read out of the
# transcript. They can overlap each other -- an interruption during a gate is
# the same lost minute counted twice -- so they are always unioned, never
# summed, before being deducted.
# ---------------------------------------------------------------------------


Span = tuple[datetime, datetime]


def _merge_spans(spans: list[Span]) -> list[Span]:
    """Return *spans* as a sorted, non-overlapping union."""
    ordered = sorted(span for span in spans if span[1] > span[0])
    merged: list[Span] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _overlap_seconds(spans: list[Span], window: Span) -> float:
    """Return how many seconds of *spans* fall inside *window*.

    *spans* must already be a union (see :func:`_merge_spans`), otherwise
    overlapping entries are double-counted.
    """
    total = 0.0
    for start, end in spans:
        lo = max(start, window[0])
        hi = min(end, window[1])
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def _idle_spans(stamps: list[datetime], interrupts: list[datetime]) -> list[Span]:
    """Return the idle spans implied by a transcript's event timestamps.

    Two mechanical rules, no judgement:

    1. Any gap between consecutive events longer than
       :data:`IDLE_GAP_THRESHOLD_SEC` is idle.
    2. Any interruption record starts an idle span that runs until the next
       event of any kind, however short.

    Args:
        stamps: Every event timestamp in the session, sorted ascending.
        interrupts: Timestamps of the interruption records, sorted ascending.

    Returns:
        The union of the spans found by both rules.
    """
    spans: list[Span] = []
    for earlier, later in zip(stamps, stamps[1:]):
        if (later - earlier).total_seconds() > IDLE_GAP_THRESHOLD_SEC:
            spans.append((earlier, later))

    for moment in interrupts:
        resumed = next((stamp for stamp in stamps if stamp > moment), None)
        if resumed is not None:
            spans.append((moment, resumed))

    return _merge_spans(spans)


# ---------------------------------------------------------------------------
# Token usage: reading Claude Code transcripts
# ---------------------------------------------------------------------------


def _encode_project_dir(cwd: Path) -> str:
    """Return the Claude Code directory name for a working directory.

    Claude Code flattens the absolute cwd into a single directory name by
    replacing each path separator, underscore, and dot with a hyphen. For
    example ``/Users/x/dev/EBPM_Claude/src`` becomes
    ``-Users-x-dev-EBPM-Claude-src``.
    """
    text = str(cwd.resolve())
    for char in ("/", "_", "."):
        text = text.replace(char, "-")
    return text


def _parse_transcript_ts(ts: str) -> datetime:
    """Parse a transcript timestamp, which uses a trailing 'Z' for UTC."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _resolve_transcript_dir(project_dir: str | None) -> Path:
    """Return the project transcript directory (explicit or derived from cwd).

    Raises:
        SystemExit(3): If the directory does not exist.
    """
    if project_dir:
        path = Path(project_dir)
    else:
        path = CLAUDE_PROJECTS_DIR / _encode_project_dir(Path.cwd())

    if not path.is_dir():
        print(
            f"No Claude Code transcript directory at '{path}'. Pass "
            f"--project-dir to point at one explicitly.",
            file=sys.stderr,
        )
        sys.exit(3)
    return path


def _find_session_transcript(transcript_dir: Path, session: str | None) -> Path:
    """Return the session transcript file (named session, else most recent).

    Raises:
        SystemExit(3): If no matching transcript exists.
    """
    if session:
        path = transcript_dir / f"{session}.jsonl"
        if not path.is_file():
            print(f"No transcript for session '{session}' in '{transcript_dir}'.", file=sys.stderr)
            sys.exit(3)
        return path

    candidates = sorted(
        transcript_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        print(f"No session transcripts found in '{transcript_dir}'.", file=sys.stderr)
        sys.exit(3)
    return candidates[0]


def _transcript_files(session_transcript: Path) -> list[Path]:
    """Return the session transcript plus every sub-agent transcript under it.

    A sub-agent writes to its own file, so the main thread looks silent while a
    spawn is working. Idle detection has to see all of them at once or a busy
    sub-agent reads as an interruption.
    """
    files = [session_transcript]
    subagents_dir = session_transcript.with_suffix("") / "subagents"
    files.extend(sorted(subagents_dir.glob("*.jsonl")))
    return files


def _transcript_activity(
    session_transcript: Path,
) -> tuple[list[datetime], list[datetime]]:
    """Return (all event timestamps, interruption timestamps), both sorted.

    Corrupted or timestamp-less lines are skipped rather than fatal: the
    transcript is an external artefact this tool only reads.
    """
    stamps: list[datetime] = []
    interrupts: list[datetime] = []

    for path in _transcript_files(session_transcript):
        try:
            handle = open(path, encoding="utf-8")
        except OSError:
            continue
        with handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                ts = obj.get("timestamp")
                if not isinstance(ts, str):
                    continue
                try:
                    moment = _parse_transcript_ts(ts)
                except ValueError:
                    continue
                stamps.append(moment)
                # Matching the raw line keeps this independent of where the
                # marker sits in the record's shape, which differs between a
                # plain interruption and one that cancelled a tool call.
                if INTERRUPT_MARKER in stripped:
                    interrupts.append(moment)

    stamps.sort()
    interrupts.sort()
    return stamps, interrupts


def _spawn_windows(session_transcript: Path) -> list[dict[str, object]]:
    """Return the real wall-clock window of every sub-agent spawn.

    This is the measurement the per-task events are trying to capture, taken
    from a source no agent can get wrong: a spawn's transcript exists from the
    moment it is launched until its last record, whether or not anyone
    remembered to run `task-start`. When a task row looks degenerate, this table
    is where the true duration is.

    Returns:
        One dict per spawn with ``agent_type``, ``description``, ``start_ts``,
        ``end_ts`` and ``elapsed_sec``, ordered by start. Spawns whose
        transcript holds no usable timestamp are skipped.
    """
    rows: list[dict[str, object]] = []
    subagents_dir = session_transcript.with_suffix("") / "subagents"
    for meta_path in sorted(subagents_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        spawn_id = meta_path.name[: -len(".meta.json")]
        stamps, _ = _transcript_activity(meta_path.with_name(f"{spawn_id}.jsonl"))
        if not stamps:
            continue
        rows.append(
            {
                "agent_type": str(meta.get("agentType") or "unknown"),
                "description": str(meta.get("description") or spawn_id),
                "start_ts": stamps[0].isoformat(),
                "end_ts": stamps[-1].isoformat(),
                "elapsed_sec": (stamps[-1] - stamps[0]).total_seconds(),
            }
        )
    rows.sort(key=lambda row: str(row["start_ts"]))
    return rows


def _read_usage_records(
    path: Path, agent_type: str, spawn_id: str | None, description: str | None
) -> list[dict[str, object]]:
    """Extract one record per API call from a transcript file.

    A single assistant message is written to the transcript several times as it
    streams, each time under the same ``(requestId, message.id)`` pair, with
    ``output_tokens`` growing toward its final value. Only the record with the
    largest ``output_tokens`` in each group is the complete one -- taking the
    first would undercount output badly (a partial can be 14 tokens where the
    finished message is 10,280). Input-side counts are constant within a group.

    Corrupted lines are skipped rather than fatal: a transcript is an external
    artefact this tool only reads, and a truncated tail is normal for a session
    that is still running.
    """
    if not path.is_file():
        return []

    groups: dict[tuple[object, object], dict[str, object]] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue

            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue

            # "<synthetic>" marks a harness-generated notice (e.g. a session
            # limit message), not an API call. Its usage is all zeros, so
            # counting it would inflate the call count with a non-request.
            if message.get("model") == "<synthetic>":
                continue

            key = (obj.get("requestId"), message.get("id"))
            output = int(usage.get("output_tokens") or 0)
            existing = groups.get(key)
            if existing is not None and int(existing["output_tokens"]) >= output:
                continue

            creation = usage.get("cache_creation")
            if isinstance(creation, dict):
                write_5m = int(creation.get("ephemeral_5m_input_tokens") or 0)
                write_1h = int(creation.get("ephemeral_1h_input_tokens") or 0)
            else:
                # Older transcripts report only the total; assume the 5m tier.
                write_5m = int(usage.get("cache_creation_input_tokens") or 0)
                write_1h = 0

            groups[key] = {
                "ts": obj.get("timestamp"),
                "model": message.get("model"),
                "agent_type": agent_type,
                "spawn_id": spawn_id,
                "description": description,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "cache_write_5m": write_5m,
                "cache_write_1h": write_1h,
                "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                "output_tokens": output,
            }

    return list(groups.values())


def _collect_api_calls(session_transcript: Path) -> list[dict[str, object]]:
    """Return every API call in a session: the main thread plus each spawn.

    Sub-agent spawns live in their own transcript files under
    ``<session-id>/subagents/``, each paired with a ``.meta.json`` naming the
    agent type. That pairing is what makes per-agent attribution possible.
    """
    calls = _read_usage_records(session_transcript, agent_type="main", spawn_id=None, description=None)

    subagents_dir = session_transcript.with_suffix("") / "subagents"
    for meta_path in sorted(subagents_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        spawn_id = meta_path.name[: -len(".meta.json")]
        calls.extend(
            _read_usage_records(
                meta_path.with_name(f"{spawn_id}.jsonl"),
                agent_type=str(meta.get("agentType") or "unknown"),
                spawn_id=spawn_id,
                description=meta.get("description"),
            )
        )

    calls.sort(key=lambda call: str(call.get("ts") or ""))
    return calls


def _billable_input(call: dict[str, object]) -> float:
    """Return input tokens weighted by their cache multipliers.

    Cache writes and reads are not priced like fresh input, so a raw token sum
    misrepresents cost. Weighting by the exact published ratios keeps the
    figure model-agnostic while remaining comparable across phases.
    """
    return (
        int(call["input_tokens"])
        + int(call["cache_write_5m"]) * CACHE_WRITE_5M_MULTIPLIER
        + int(call["cache_write_1h"]) * CACHE_WRITE_1H_MULTIPLIER
        + int(call["cache_read"]) * CACHE_READ_MULTIPLIER
    )


def _usd_cost(call: dict[str, object]) -> float | None:
    """Return the USD cost of one call, or None when the model is unpriced."""
    pricing = MODEL_PRICING_USD_PER_MTOK.get(str(call.get("model")))
    if pricing is None:
        return None
    input_price, output_price = pricing
    return (
        _billable_input(call) * input_price + int(call["output_tokens"]) * output_price
    ) / 1_000_000


def _phase_windows(events: list[dict[str, object]]) -> list[tuple[str, datetime, datetime]]:
    """Return closed (phase, start, end) windows from the timing log."""
    open_starts: dict[str, datetime] = {}
    windows: list[tuple[str, datetime, datetime]] = []
    for event in events:
        phase = event.get("phase")
        ts = event.get("ts")
        if not isinstance(phase, str) or not isinstance(ts, str):
            continue
        if event.get("event") == "start":
            open_starts[phase] = _parse_iso(ts)
        elif event.get("event") == "end" and phase in open_starts:
            windows.append((phase, open_starts.pop(phase), _parse_iso(ts)))
    return windows


#: Bucket label for calls that fall outside every measured phase window.
_UNATTRIBUTED = "(outside measured phases)"


def _phase_interval_counts(
    windows: list[tuple[str, datetime, datetime]],
) -> dict[str, int]:
    """Return how many closed windows each phase name has."""
    counts: dict[str, int] = {}
    for phase, _, _ in windows:
        counts[phase] = counts.get(phase, 0) + 1
    return counts


def _phase_display_label(phase: str, counts: dict[str, int]) -> str:
    """Return the table label for *phase*, noting a phase that ran more than once.

    The usage tables bucket by phase name, so a phase entered twice adds up into
    one row and the re-run disappears. The suffix keeps it visible without
    splitting the totals, which are still wanted whole.
    """
    count = counts.get(phase, 0)
    return f"{phase} ({count}区間)" if count > 1 else phase


def _attribute_phase(
    call: dict[str, object], windows: list[tuple[str, datetime, datetime]]
) -> str:
    """Return the phase whose window contains this call's timestamp."""
    ts = call.get("ts")
    if not isinstance(ts, str):
        return _UNATTRIBUTED
    try:
        moment = _parse_transcript_ts(ts)
    except ValueError:
        return _UNATTRIBUTED
    for phase, start, end in windows:
        if start <= moment <= end:
            return phase
    return _UNATTRIBUTED


def _blank_totals() -> dict[str, float]:
    """Return a zeroed accumulator for the usage tables."""
    return {
        "calls": 0.0,
        "input_tokens": 0.0,
        "cache_write_5m": 0.0,
        "cache_write_1h": 0.0,
        "cache_read": 0.0,
        "output_tokens": 0.0,
        "billable_input": 0.0,
        "usd": 0.0,
        "unpriced_calls": 0.0,
        "unpriced_output_tokens": 0.0,
    }


def _accumulate(totals: dict[str, float], call: dict[str, object]) -> None:
    """Fold one API call into an accumulator produced by :func:`_blank_totals`.

    A call whose model is absent from the price table adds nothing to ``usd``;
    it is counted in ``unpriced_calls`` / ``unpriced_output_tokens`` instead, so
    that "what the total omits" stays reportable rather than vanishing into $0.
    """
    totals["calls"] += 1
    for field in ("input_tokens", "cache_write_5m", "cache_write_1h", "cache_read", "output_tokens"):
        totals[field] += int(call[field])
    totals["billable_input"] += _billable_input(call)
    cost = _usd_cost(call)
    if cost is None:
        totals["unpriced_calls"] += 1
        totals["unpriced_output_tokens"] += int(call["output_tokens"])
    else:
        totals["usd"] += cost


def _fmt_elapsed(value: object) -> str:
    """Format an elapsed value for the table ('-' when None/non-numeric)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.3f}"
    return "-"


def _optional_transcript(
    project_dir: str | None, session: str | None
) -> tuple[Path | None, str | None]:
    """Resolve a transcript for `report`, without ever failing the report.

    Transcript analysis is opt-in. With neither flag, `report` stays a pure
    function of the timing log rather than going hunting through ~/.claude for
    a session that may well belong to some other piece of work.

    Returns:
        ``(transcript, problem)``. ``transcript`` is ``None`` when none was
        asked for or none was found; ``problem`` describes why, or is ``None``
        when nothing was asked for.
    """
    if not project_dir and not session:
        return None, None

    if project_dir:
        directory = Path(project_dir)
    else:
        directory = CLAUDE_PROJECTS_DIR / _encode_project_dir(Path.cwd())
    if not directory.is_dir():
        return None, f"no transcript directory at '{directory}'"

    if session:
        transcript = directory / f"{session}.jsonl"
        if not transcript.is_file():
            return None, f"no transcript for session '{session}' in '{directory}'"
        return transcript, None

    candidates = sorted(
        directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        return None, f"no session transcripts in '{directory}'"
    return candidates[0], None


def cmd_report(path: Path, project_dir: str | None, session: str | None) -> int:
    """Read the log, print an aligned per-phase table and totals to stdout.

    Two times are reported for every phase and never collapsed into one:
    ``elapsed_sec`` is the wall clock, and ``active_sec`` is what is left after
    deducting the human gates and, when a transcript is supplied, the spans in
    which the session was interrupted or idle.
    """
    events = _read_events(path)
    summary = _summarise(events)
    gates = _summarise_gates(events)

    # Human gates are a recorded fact; idle spans have to be read out of the
    # transcript. Both are deducted as one union so an interruption that
    # happened during a gate is not subtracted twice.
    gate_spans = [
        span
        for span in (_closed_interval(row, "open_ts", "close_ts") for row in gates)
        if span is not None
    ]
    transcript, transcript_problem = _optional_transcript(project_dir, session)
    idle_spans: list[Span] = []
    if transcript is not None:
        stamps, interrupts = _transcript_activity(transcript)
        idle_spans = _idle_spans(stamps, interrupts)
    non_working = _merge_spans(gate_spans + idle_spans)

    # Column widths.
    phase_w = max([len("phase")] + [len(str(r["phase"])) for r in summary]) if summary else len("phase")
    ts_w = 32  # ISO 8601 with offset comfortably fits.
    elapsed_w = max(len("elapsed_sec"), 12)

    header = (
        f"{'phase':<{phase_w}}  {'start_ts':<{ts_w}}  "
        f"{'end_ts':<{ts_w}}  {'elapsed_sec':>{elapsed_w}}  "
        f"{'active_sec':>{elapsed_w}}"
    )
    print("Phase timing report")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    total_elapsed = 0.0
    total_active = 0.0
    # phase -> [interval count, summed elapsed, summed active]. A phase that was
    # run twice (a preprocess redone after the data was replaced, say) has two
    # rows above; this keeps the fact countable instead of leaving the reader to
    # notice the repeat.
    per_phase: dict[str, list[float]] = {}
    for row in summary:
        start_ts = str(row["start_ts"]) if row["start_ts"] is not None else "-"
        end_ts = str(row["end_ts"]) if row["end_ts"] is not None else "-"
        elapsed = row["elapsed_sec"]
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            total_elapsed += float(elapsed)
        window = _closed_interval(row)
        if window is None:
            active: object = None
        else:
            active = max(0.0, (window[1] - window[0]).total_seconds()
                         - _overlap_seconds(non_working, window))
            total_active += float(active)
        tally = per_phase.setdefault(str(row["phase"]), [0.0, 0.0, 0.0])
        tally[0] += 1
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
            tally[1] += float(elapsed)
        if isinstance(active, float):
            tally[2] += active
        print(
            f"{str(row['phase']):<{phase_w}}  {start_ts:<{ts_w}}  "
            f"{end_ts:<{ts_w}}  {_fmt_elapsed(elapsed):>{elapsed_w}}  "
            f"{_fmt_elapsed(active):>{elapsed_w}}"
        )

    print("-" * len(header))

    reentered = [(name, t) for name, t in per_phase.items() if t[0] > 1]
    if reentered:
        parts = [
            f"{name} ({int(t[0])}区間; elapsed {t[1]:.3f} sec, active {t[2]:.3f} sec)"
            for name, t in reentered
        ]
        print(f"Re-entered phases: {', '.join(parts)}")
        print("-" * len(header))

    # Marks listed separately, in chronological (file) order.
    marks = [ev for ev in events if ev.get("event") == "mark"]
    if marks:
        print(f"Marks ({len(marks)}):")
        for ev in marks:
            label = ev.get("label")
            ts = ev.get("ts")
            print(f"  [{ts}] {label}")
        print("-" * len(header))

    # Per-task section (independent of the per-phase table above).
    tasks = _summarise_tasks(events)
    if tasks:
        intervals = [_closed_interval(r) for r in tasks]
        overlaps = _task_overlaps(intervals)

        task_w = max([len("task")] + [len(str(r["task"])) for r in tasks])
        overlap_w = len("overlap")
        flag_w = len("suspect")
        task_header = (
            f"{'task':<{task_w}}  {'start_ts':<{ts_w}}  "
            f"{'end_ts':<{ts_w}}  {'elapsed_sec':>{elapsed_w}}  "
            f"{'overlap':>{overlap_w}}  {'suspect':>{flag_w}}"
        )
        print(f"Per-task timing ({len(tasks)}):")
        print(task_header)
        print("-" * len(task_header))
        suspect_count = 0
        for row, overlap, interval in zip(tasks, overlaps, intervals):
            start_ts = str(row["start_ts"]) if row["start_ts"] is not None else "-"
            end_ts = str(row["end_ts"]) if row["end_ts"] is not None else "-"
            elapsed = _fmt_elapsed(row["elapsed_sec"])
            overlap_str = "yes" if overlap else "no"
            # Derived from the timestamps, not from a flag in the event, so
            # logs written before this check are judged the same way.
            short = (
                interval is not None
                and (interval[1] - interval[0]).total_seconds() < MIN_PLAUSIBLE_TASK_SEC
            )
            if short:
                suspect_count += 1
            print(
                f"{str(row['task']):<{task_w}}  {start_ts:<{ts_w}}  "
                f"{end_ts:<{ts_w}}  {elapsed:>{elapsed_w}}  "
                f"{overlap_str:>{overlap_w}}  {('short' if short else '-'):>{flag_w}}"
            )
        print("-" * len(task_header))
        peak = _max_concurrency(intervals)
        print(f"Max observed task concurrency: {peak}")
        if suspect_count:
            print(
                f"Suspect task rows: {suspect_count} under "
                f"{MIN_PLAUSIBLE_TASK_SEC:.0f} sec. Their start and end were "
                "recorded together after the work, so the figure is not a "
                "measurement of the task. Read the per-spawn table instead."
            )
        print("-" * len(header))

    # Per-spawn section: the same durations taken from the transcript, which
    # does not depend on anyone remembering to record a task-start.
    if transcript is not None:
        spawns = _spawn_windows(transcript)
        if spawns:
            desc_w = min(48, max(len("description"), *(len(str(r["description"])) for r in spawns)))
            agent_w = max([len("agent_type")] + [len(str(r["agent_type"])) for r in spawns])
            spawn_header = (
                f"{'agent_type':<{agent_w}}  {'description':<{desc_w}}  "
                f"{'start_ts':<{ts_w}}  {'end_ts':<{ts_w}}  "
                f"{'elapsed_sec':>{elapsed_w}}"
            )
            print(f"Per-spawn wall-clock from transcript ({len(spawns)}):")
            print(spawn_header)
            print("-" * len(spawn_header))
            for row in spawns:
                description = str(row["description"])
                if len(description) > desc_w:
                    description = description[: desc_w - 1] + "*"
                print(
                    f"{str(row['agent_type']):<{agent_w}}  {description:<{desc_w}}  "
                    f"{str(row['start_ts']):<{ts_w}}  {str(row['end_ts']):<{ts_w}}  "
                    f"{_fmt_elapsed(row['elapsed_sec']):>{elapsed_w}}"
                )
            print("-" * len(spawn_header))
            print("-" * len(header))

    # Per-gate section (human-approval intervals).
    gate_total = 0.0
    if gates:
        gate_w = max([len("gate")] + [len(str(r["gate"])) for r in gates])
        gate_header = (
            f"{'gate':<{gate_w}}  {'open_ts':<{ts_w}}  "
            f"{'close_ts':<{ts_w}}  {'elapsed_sec':>{elapsed_w}}"
        )
        print(f"Per-gate timing ({len(gates)}):")
        print(gate_header)
        print("-" * len(gate_header))
        for row in gates:
            open_ts = str(row["open_ts"]) if row["open_ts"] is not None else "-"
            close_ts = str(row["close_ts"]) if row["close_ts"] is not None else "-"
            elapsed = row["elapsed_sec"]
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                gate_total += float(elapsed)
            print(
                f"{str(row['gate']):<{gate_w}}  {open_ts:<{ts_w}}  "
                f"{close_ts:<{ts_w}}  {_fmt_elapsed(elapsed):>{elapsed_w}}"
            )
        print("-" * len(gate_header))
        print("-" * len(header))
    # When there are no gate rows, gate_total stays 0.0 (agent compute ==
    # wall-clock) and no per-gate table is printed.

    # Totals.
    timestamps = _all_timestamps(events)
    if timestamps:
        earliest = min(timestamps)
        latest = max(timestamps)
        wall_clock = (latest - earliest).total_seconds()
        wall_str = f"{wall_clock:.3f}"
        span_str = f"{earliest.isoformat()} -> {latest.isoformat()}"
    else:
        wall_clock = 0.0
        wall_str = "0.000"
        span_str = "(no events)"

    agent_compute = max(0.0, wall_clock - gate_total)

    if timestamps:
        session = (earliest, latest)
        idle_total = _overlap_seconds(_merge_spans(idle_spans), session)
        phase_spans = _merge_spans(
            [
                span
                for span in (_closed_interval(row) for row in summary)
                if span is not None
            ]
        )
        unphased = max(0.0, wall_clock - _overlap_seconds(phase_spans, session))
        active_time = max(0.0, wall_clock - _overlap_seconds(non_working, session))
    else:
        idle_total = 0.0
        unphased = 0.0
        active_time = 0.0

    print(f"Summed phase elapsed_sec : {total_elapsed:.3f}")
    print(f"Summed phase active_sec  : {total_active:.3f}")
    print(f"Overall wall-clock span  : {wall_str} sec  ({span_str})")
    print(f"Human-gate total       : {gate_total:.3f} sec")
    if transcript is None:
        reason = transcript_problem or (
            "pass --project-dir/--session to deduct them from the transcript"
        )
        print(f"Idle/interrupted total  : not measured  ({reason})")
    else:
        print(
            f"Idle/interrupted total  : {idle_total:.3f} sec  "
            f"({len(idle_spans)} span(s) from '{transcript.name}'; a gap over "
            f"{IDLE_GAP_THRESHOLD_SEC:.0f} sec, or an interruption record)"
        )
    print(f"Time outside any phase  : {unphased:.3f} sec")
    print(
        f"Agent compute time      : {agent_compute:.3f} sec  "
        f"(wall-clock minus human-gate total)"
    )
    print(
        f"Active time             : {active_time:.3f} sec  "
        f"(wall-clock minus human-gate and idle/interrupted spans, unioned)"
    )
    print(_TOKEN_NOTE)
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _fmt_int(value: float) -> str:
    """Format a token count with thousands separators."""
    return f"{int(round(value)):,}"


def _fmt_usd(totals: dict[str, float]) -> str:
    """Format an accumulator's cost, flagging buckets the price table misses.

    Three distinct outcomes, never collapsed: no calls at all ("-"), every call
    on an unpriced model ("unpriced"), and a partial figure that omits some
    calls ("$X.XX+"). A bare "$X.XX" therefore means the figure is complete.
    """
    if totals["calls"] == 0:
        return "-"
    if totals["unpriced_calls"] >= totals["calls"]:
        return "unpriced"
    suffix = "+" if totals["unpriced_calls"] else ""
    return f"${totals['usd']:.2f}{suffix}"


def _print_usage_table(
    title: str, rows: list[tuple[str, dict[str, float]]], label_header: str
) -> None:
    """Print one aligned usage table, widest column sized to its contents."""
    label_w = max([len(label_header)] + [len(label) for label, _ in rows])
    header = (
        f"{label_header:<{label_w}}  {'calls':>6}  {'in':>10}  {'cache_wr':>10}  "
        f"{'cache_rd':>12}  {'out':>10}  {'billable_in':>12}  {'cost':>9}"
    )
    print()
    print(title)
    print("-" * len(header))
    print(header)
    print("-" * len(header))
    for label, totals in rows:
        print(
            f"{label:<{label_w}}  {int(totals['calls']):>6}  "
            f"{_fmt_int(totals['input_tokens']):>10}  "
            f"{_fmt_int(totals['cache_write_5m'] + totals['cache_write_1h']):>10}  "
            f"{_fmt_int(totals['cache_read']):>12}  "
            f"{_fmt_int(totals['output_tokens']):>10}  "
            f"{_fmt_int(totals['billable_input']):>12}  "
            f"{_fmt_usd(totals):>9}"
        )


def cmd_usage(
    path: Path,
    project_dir: str | None,
    session: str | None,
    as_json: bool,
) -> int:
    """Report real billed token usage for a session, attributed to phases.

    Reads the Claude Code transcript rather than the timing log, then uses the
    timing log's phase windows to attribute each API call to a phase.
    """
    transcript_dir = _resolve_transcript_dir(project_dir)
    transcript = _find_session_transcript(transcript_dir, session)
    calls = _collect_api_calls(transcript)

    if not calls:
        print(f"No API calls found in transcript '{transcript}'.", file=sys.stderr)
        return 3

    windows = _phase_windows(_read_events(path))

    overall = _blank_totals()
    by_phase: dict[str, dict[str, float]] = {}
    by_agent: dict[str, dict[str, float]] = {}
    by_spawn: dict[str, dict[str, float]] = {}
    unpriced_models: set[str] = set()

    for call in calls:
        if str(call.get("model")) not in MODEL_PRICING_USD_PER_MTOK:
            unpriced_models.add(str(call.get("model")))
        _accumulate(overall, call)
        _accumulate(by_phase.setdefault(_attribute_phase(call, windows), _blank_totals()), call)
        agent = str(call["agent_type"])
        _accumulate(by_agent.setdefault(agent, _blank_totals()), call)
        if call["spawn_id"] is not None:
            # ASCII separator on purpose: this label is printed, and a dash
            # outside the terminal's code page used to abort the whole report.
            label = f"{agent} - {call['description'] or call['spawn_id']}"
            _accumulate(by_spawn.setdefault(label, _blank_totals()), call)

    spawn_count = len(by_spawn)

    interval_counts = _phase_interval_counts(windows)

    if as_json:
        payload = {
            "transcript": str(transcript),
            "phase_windows": len(windows),
            # Keys stay the bare phase name so the payload stays machine-
            # readable; the interval count rides alongside rather than inside.
            "phase_interval_counts": interval_counts,
            "spawn_count": spawn_count,
            "overall": overall,
            "by_phase": by_phase,
            "by_agent_type": by_agent,
            "by_spawn": by_spawn,
            "unpriced_models": sorted(unpriced_models),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("Token usage report")
    print(f"transcript : {transcript}")
    print(f"spawns     : {spawn_count}")
    if not windows:
        print(
            "phases     : none closed in the timing log -- every call lands in "
            f"'{_UNATTRIBUTED}'"
        )
    else:
        print(f"phases     : {len(windows)} closed window(s)")

    ordered_phases = [
        (_phase_display_label(phase, interval_counts), totals)
        for phase, totals in sorted(
            by_phase.items(), key=lambda item: item[1]["billable_input"], reverse=True
        )
    ]
    _print_usage_table("By phase", ordered_phases, "phase")

    ordered_agents = sorted(
        by_agent.items(), key=lambda item: item[1]["billable_input"], reverse=True
    )
    _print_usage_table("By agent type", ordered_agents, "agent_type")

    if by_spawn:
        ordered_spawns = sorted(
            by_spawn.items(), key=lambda item: item[1]["billable_input"], reverse=True
        )
        _print_usage_table("By spawn", ordered_spawns, "spawn")

    _print_usage_table("Total", [("all", overall)], "scope")

    print()
    if overall["unpriced_calls"]:
        # The headline figure must never read as "this is what the run cost"
        # when part of the run has no price. State the omission on the same line.
        print(
            f"Priced total ${overall['usd']:.2f} "
            f"(excludes {int(overall['unpriced_calls'])} unpriced call(s) / "
            f"{_fmt_int(overall['unpriced_output_tokens'])} output tokens -- "
            "the true total is higher)"
        )
        print(
            f"Unpriced model(s): {', '.join(sorted(unpriced_models))}. "
            "Add the rate to MODEL_PRICING_USD_PER_MTOK in this tool after "
            "confirming it in the Anthropic pricing documentation."
        )
        print(
            "In the tables above, 'unpriced' marks a row with no priced call and "
            "'+' marks a row whose figure omits some calls."
        )
    else:
        print(f"Priced total ${overall['usd']:.2f} (every call priced)")
    print(
        "billable_in weights cache tokens by their price ratios "
        f"(5m write x{CACHE_WRITE_5M_MULTIPLIER}, 1h write x{CACHE_WRITE_1H_MULTIPLIER}, "
        f"read x{CACHE_READ_MULTIPLIER}); a raw token sum would overstate cached input."
    )
    return 0


def _add_file_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the common --file override to a subparser."""
    parser.add_argument(
        "--file",
        default=None,
        help=(
            "Override the log file path (default: "
            "<analysis layer root>/output/.phase_timing.jsonl)."
        ),
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic phase-timing instrument for EBPM analysis sessions. "
            "Time-only; token usage is not captured (see 'report' output)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_start = subparsers.add_parser("start", help="Record a phase start event.")
    p_start.add_argument("--phase", required=True, help="Phase name (arbitrary string).")
    p_start.add_argument("--note", default=None, help="Optional free-text note.")
    _add_file_arg(p_start)

    p_end = subparsers.add_parser("end", help="Record a phase end event.")
    p_end.add_argument("--phase", required=True, help="Phase name (arbitrary string).")
    p_end.add_argument("--note", default=None, help="Optional free-text note.")
    _add_file_arg(p_end)

    p_task_start = subparsers.add_parser(
        "task-start", help="Record a per-task start event."
    )
    p_task_start.add_argument(
        "--task", required=True, help="Task name (arbitrary string)."
    )
    p_task_start.add_argument("--note", default=None, help="Optional free-text note.")
    _add_file_arg(p_task_start)

    p_task_end = subparsers.add_parser(
        "task-end", help="Record a per-task end event."
    )
    p_task_end.add_argument(
        "--task", required=True, help="Task name (arbitrary string)."
    )
    p_task_end.add_argument("--note", default=None, help="Optional free-text note.")
    _add_file_arg(p_task_end)

    p_gate_open = subparsers.add_parser(
        "gate-open", help="Record a human-gate open event."
    )
    p_gate_open.add_argument(
        "--gate", required=True, help="Gate id (arbitrary string)."
    )
    p_gate_open.add_argument("--note", default=None, help="Optional free-text note.")
    _add_file_arg(p_gate_open)

    p_gate_close = subparsers.add_parser(
        "gate-close", help="Record a human-gate close event."
    )
    p_gate_close.add_argument(
        "--gate", required=True, help="Gate id (arbitrary string)."
    )
    p_gate_close.add_argument("--note", default=None, help="Optional free-text note.")
    _add_file_arg(p_gate_close)

    p_mark = subparsers.add_parser("mark", help="Record a labelled mark event.")
    p_mark.add_argument("--label", required=True, help="Free-text mark label.")
    _add_file_arg(p_mark)

    p_report = subparsers.add_parser("report", help="Print the timing report.")
    p_report.add_argument(
        "--project-dir",
        default=None,
        help=(
            "Claude Code transcript directory. Supply it (or --session) to "
            "deduct interrupted/idle spans from the active-time figures; "
            "without either, only human gates are deducted."
        ),
    )
    p_report.add_argument(
        "--session",
        default=None,
        help="Session id to read (default: the most recently modified transcript).",
    )
    _add_file_arg(p_report)

    p_reset = subparsers.add_parser("reset", help="Truncate the log file to empty.")
    _add_file_arg(p_reset)

    p_usage = subparsers.add_parser(
        "usage",
        help="Report real billed token usage for a session, attributed to phases.",
    )
    p_usage.add_argument(
        "--project-dir",
        default=None,
        help=(
            "Claude Code transcript directory (default: derived from the "
            "current working directory under ~/.claude/projects/)."
        ),
    )
    p_usage.add_argument(
        "--session",
        default=None,
        help="Session id to read (default: the most recently modified transcript).",
    )
    p_usage.add_argument(
        "--json", action="store_true", help="Emit the report as JSON instead of tables."
    )
    _add_file_arg(p_usage)

    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the phase_timer CLI.

    Returns:
        0 on success, 1 on generic/IO errors, 2 on a corrupted log during report.
    """
    _make_stdio_safe()
    args = _parse_args(argv)
    path = _resolve_path(args.file)

    if args.command == "start":
        return cmd_start(path, args.phase, args.note)
    if args.command == "end":
        return cmd_end(path, args.phase, args.note)
    if args.command == "task-start":
        return cmd_task_start(path, args.task, args.note)
    if args.command == "task-end":
        return cmd_task_end(path, args.task, args.note)
    if args.command == "gate-open":
        return cmd_gate_open(path, args.gate, args.note)
    if args.command == "gate-close":
        return cmd_gate_close(path, args.gate, args.note)
    if args.command == "mark":
        return cmd_mark(path, args.label)
    if args.command == "report":
        return cmd_report(path, args.project_dir, args.session)
    if args.command == "reset":
        return cmd_reset(path)
    if args.command == "usage":
        return cmd_usage(path, args.project_dir, args.session, args.json)

    print(f"Unknown command: {args.command!r}", file=sys.stderr)  # pragma: no cover
    return 1  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
