"""
round_counter.py -- Deterministic round-cap guard for agent review loops.

Design principle: "control by LLM, guarantee by script." The orchestrator
(an LLM) must not rely on its own memory to know how many loop rounds
have occurred. Instead, a state file is the single source of truth for the
current round number, and this CLI enforces the maximum-round cap
deterministically.

Two loops use this counter, each with its OWN state file so that one never
overwrites the other's count:
    design loop        <layer>/output/.design_round_state.json  (default)
    verification loop  <layer>/output/.verification_round_state.json  via
                       --state-file / --phase

State file (per case and per loop; overwritten on init):
    {"phase": "design", "round": N, "max_rounds": M}

State file path resolution, in precedence order:
    1. --state-file (explicit; flow.md passes it for both loops)
    2. ROUND_STATE_PATH environment variable (used by tests so the real output
       state file is never polluted)
    3. DEFAULT_STATE_PATH (the design-loop default)

Usage:
    python tools/round_counter.py init [--max-rounds N] [--phase design]
                                           [--state-file <path>]
    python tools/round_counter.py increment [--state-file <path>]
    python tools/round_counter.py status [--state-file <path>]

When --max-rounds is omitted, the default comes from harness_config.py, which
reads src/config.json (design_max_rounds / verification_max_rounds, keyed by
--phase) and falls back to a built-in default when that file is absent. This
lets the operator edit the cap without editing this script; enforcement
itself (init's `>= 1` check) is unchanged.

`--phase` only tags the state file so a reader can tell the two loops apart;
increment/status preserve whatever tag init wrote.

Every subcommand prints a JSON object to stdout:
    {"round": N, "max_rounds": M, "at_cap": bool}
where at_cap == (round >= max_rounds).

Exit codes:
    0  Success (init / increment / status).
    2  Usage error: invalid subcommand or arguments; state file missing for
       status / increment; state file corrupt (unparseable JSON, missing
       required keys, or wrong value types).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

# harness_config.py is a sibling script-style module, not a package import: at
# runtime this always works because the wrapper (tools/run) execs this file by
# its own absolute path, so Python puts this directory at sys.path[0]. mypy
# instead resolves imports by package structure (src/tools/__init__.py makes
# this a package), so it cannot find a bare "harness_config" -- hence the
# ignore, not a real missing dependency.
import harness_config  # type: ignore[import-not-found]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default state file location, relative to this module (tools/ -> <layer>/output/).
DEFAULT_STATE_PATH = Path(__file__).parent.parent / "output" / ".design_round_state.json"

#: Environment variable that, when set, overrides DEFAULT_STATE_PATH (for tests).
_STATE_PATH_ENV = "ROUND_STATE_PATH"

#: Default phase tag stored in the state file (overridable via --phase).
_PHASE = "design"

# ---------------------------------------------------------------------------
# Self-explaining exit codes
#
# The whole point of this counter is that the orchestrator must not substitute
# its own memory for the state file. A corrupt state file is therefore the one
# case where "work around it" is exactly wrong, so the tool says so in the
# error itself. These strings name no document and no section on purpose: a
# pointer into prose dies the moment the prose is reorganised.
# ---------------------------------------------------------------------------

#: Appended to every error that leaves the round count untrustworthy -- a state
#: file that cannot be parsed or validated, and a write that did not land.
#: NOT appended to "no state file yet", which has its own recovery ('init').
_CORRUPT_STATE_NOTE = (
    "Do not hand-edit the state file or substitute a remembered round count; "
    "stop and show this error to the operator."
)

#: Shown as the --help epilog, so the code list travels with the tool and
#: cannot be looked for in a document that has since been reorganised.
_EXIT_HELP = """\
Exit codes:
  0  Success. at_cap=true in the stdout JSON is a normal outcome (the cap was
     reached), not an error -- read the JSON, do not read the exit code, to
     learn whether the loop may run again.
  2  Usage or state error -- an invalid subcommand or arguments; no state file
     yet (run 'init' first); or a state file that exists but is corrupt
     (unparseable JSON, a missing required key, or a value of the wrong type).
     A corrupt state file means the round count can no longer be trusted: do
     not hand-edit it and do not substitute a remembered count. Stop and show
     the error to the operator.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exit2(message: str) -> NoReturn:
    """Print *message* to stderr and exit with code 2."""
    print(message, file=sys.stderr)
    sys.exit(2)


def _state_path(explicit: str | None = None) -> Path:
    """Return the active state file path.

    Precedence: explicit --state-file, then the ROUND_STATE_PATH env override
    (tests), then DEFAULT_STATE_PATH. With neither of the first two, the
    design-loop default applies unchanged.

    Args:
        explicit: Value of --state-file, or None when not given.
    """
    if explicit:
        return Path(explicit)
    override = os.environ.get(_STATE_PATH_ENV)
    if override:
        return Path(override)
    return DEFAULT_STATE_PATH


def _result_dict(round_value: int, max_rounds: int) -> dict[str, object]:
    """Build the stdout result object with derived at_cap flag."""
    return {
        "round": round_value,
        "max_rounds": max_rounds,
        "at_cap": round_value >= max_rounds,
    }


def _emit(round_value: int, max_rounds: int) -> None:
    """Print the result object as a single JSON line to stdout."""
    print(json.dumps(_result_dict(round_value, max_rounds)))


def _write_state(
    path: Path, round_value: int, max_rounds: int, phase: str = _PHASE
) -> None:
    """Persist the state file, creating parent directories as needed.

    Args:
        path: Destination state file path.
        round_value: Current round number to store.
        max_rounds: Configured maximum number of rounds.
        phase: Phase tag to record (defaults to the design-loop tag).

    Raises:
        SystemExit(2): If the file cannot be written.
    """
    state = {"phase": phase, "round": round_value, "max_rounds": max_rounds}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError as exc:
        _exit2(f"Failed to write state file '{path}': {exc}. {_CORRUPT_STATE_NOTE}")


def _load_state(path: Path) -> tuple[int, int, str]:
    """Read and validate the state file.

    Args:
        path: State file path.

    Returns:
        A ``(round, max_rounds, phase)`` tuple. The first two are validated
        integers; ``phase`` is the recorded tag, falling back to the default
        for state files written before the tag was configurable.

    Raises:
        SystemExit(2): If the file is missing, unparseable, missing required
            keys, or contains values of the wrong type.
    """
    if not path.exists():
        _exit2(
            f"State file not found: '{path}'. "
            f"Run 'round_counter.py init' first."
        )

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        _exit2(
            f"State file '{path}' is corrupt (invalid JSON): {exc}. "
            f"{_CORRUPT_STATE_NOTE}"
        )
    except OSError as exc:
        _exit2(f"Failed to read state file '{path}': {exc}. {_CORRUPT_STATE_NOTE}")

    if not isinstance(data, dict):
        _exit2(
            f"State file '{path}' is corrupt: top-level value is not an object. "
            f"{_CORRUPT_STATE_NOTE}"
        )

    for key in ("round", "max_rounds"):
        if key not in data:
            _exit2(
                f"State file '{path}' is corrupt: missing required key '{key}'. "
                f"{_CORRUPT_STATE_NOTE}"
            )
        # bool is a subclass of int; reject it explicitly to catch type drift.
        if not isinstance(data[key], int) or isinstance(data[key], bool):
            _exit2(
                f"State file '{path}' is corrupt: key '{key}' must be an "
                f"integer (got {type(data[key]).__name__}). "
                f"{_CORRUPT_STATE_NOTE}"
            )

    phase = data.get("phase")
    if not isinstance(phase, str) or not phase:
        phase = _PHASE

    return data["round"], data["max_rounds"], phase


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_init(
    max_rounds: int | None, state_file: str | None = None, phase: str = _PHASE
) -> int:
    """Initialise (or overwrite) the state file with round=0.

    Args:
        max_rounds: Maximum number of rounds to enforce, or None to resolve
            it from harness_config.py (src/config.json / built-in default)
            based on *phase*.
        state_file: Explicit state file path, or None for the default.
        phase: Phase tag to record in the state file.

    Returns:
        Exit code 0 on success.
    """
    if max_rounds is None:
        try:
            max_rounds = harness_config.get_max_rounds(phase)
        except harness_config.HarnessConfigError as exc:
            _exit2(str(exc))
    if max_rounds < 1:
        _exit2(f"--max-rounds must be >= 1 (got {max_rounds}).")
    path = _state_path(state_file)
    _write_state(path, 0, max_rounds, phase)
    _emit(0, max_rounds)
    return 0


def cmd_increment(state_file: str | None = None) -> int:
    """Increment the round number, clamped at max_rounds.

    Reaching or exceeding the cap is not an error: increment is idempotent at
    the cap and still exits 0 so the orchestrator can mechanically observe
    at_cap. The phase tag written by init is preserved.

    Args:
        state_file: Explicit state file path, or None for the default.

    Returns:
        Exit code 0 on success.
    """
    path = _state_path(state_file)
    round_value, max_rounds, phase = _load_state(path)
    if round_value < max_rounds:
        round_value += 1
    _write_state(path, round_value, max_rounds, phase)
    _emit(round_value, max_rounds)
    return 0


def cmd_status(state_file: str | None = None) -> int:
    """Print the current state without modifying it.

    Args:
        state_file: Explicit state file path, or None for the default.

    Returns:
        Exit code 0 on success.
    """
    path = _state_path(state_file)
    round_value, max_rounds, _phase = _load_state(path)
    _emit(round_value, max_rounds)
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic round-cap guard for agent review loops (design "
            "and verification). The state file is the single "
            "source of truth for the current round number."
        ),
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def _add_state_file(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--state-file",
            default=None,
            help=(
                "State file path. Defaults to the design-loop state file; pass a "
                "separate path for another loop (e.g. verification) so the two "
                "counts never overwrite each other."
            ),
        )

    p_init = subparsers.add_parser(
        "init", help="Initialise the round counter at round 0 (overwrites)."
    )
    p_init.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help=(
            "Maximum number of rounds. When omitted, the default comes from "
            "config.json (design_max_rounds / verification_max_rounds, "
            "selected by --phase) or a built-in default when that file is "
            "absent."
        ),
    )
    p_init.add_argument(
        "--phase",
        default=_PHASE,
        help=(
            f"Phase tag recorded in the state file, so a reader can tell the "
            f"loops apart (default: {_PHASE})."
        ),
    )
    _add_state_file(p_init)

    p_increment = subparsers.add_parser(
        "increment", help="Increment the round number (clamped at max_rounds)."
    )
    _add_state_file(p_increment)

    p_status = subparsers.add_parser("status", help="Print the current round state.")
    _add_state_file(p_status)

    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the round_counter CLI.

    Returns:
        0 on success, 2 on usage / state errors.
    """
    args = _parse_args(argv)

    if args.command == "init":
        return cmd_init(args.max_rounds, args.state_file, args.phase)
    if args.command == "increment":
        return cmd_increment(args.state_file)
    if args.command == "status":
        return cmd_status(args.state_file)

    # argparse with required=True should prevent reaching here.
    _exit2(f"Unknown command: {args.command!r}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
