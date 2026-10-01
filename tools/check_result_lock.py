"""
check_result_lock.py -- Direction lock for the result-verification loop.

The verification loop lets the econometrician review EXECUTED results before
the report is written. That creates the one hazard statistics cannot tolerate:
strengthening a claim after seeing the numbers, or quietly swapping analyses
until the numbers please. This tool is the mechanical backstop for the room's
rule -- the verification loop may fix, weaken, confirm, point forward, and
(marked) explore, but it may never strengthen a confirmatory claim after the
fact.

It compares a baseline snapshot of the AnalysisOutput (taken before the loop
starts) with the current AnalysisOutput, and enforces five properties:

    1. Every task present in the baseline is still present (no silent deletion).
    2. For tasks common to both, causation_claim has NOT been upgraded
       (descriptive < correlation_only < causal_evidence; current <= baseline).
    3. Every ADDED task (a task_name absent from the baseline) carries the
       exploratory marker `origin: "verification_exploratory"`. A missing `origin`
       field counts as NO marker -- an unmarked addition is a violation
       (fail-closed). Baseline-derived tasks may omit `origin` freely.
    4. Every added task's causation_claim is at most correlation_only
       (an exploratory addition can never claim causal evidence).
    5. The number of added tasks does not exceed --max-additions (default 2).

Deliberately NOT checked: the `statistics` values themselves. A bug fix
(`error_refix`) that makes a number stronger is legitimate -- what is locked is
the CLAIM level, not the numbers. Locking numbers would forbid honest repair.

Usage:
    python tools/check_result_lock.py --baseline <json> --current <json>
                                          [--max-additions N]

When --max-additions is omitted, the default comes from harness_config.py,
which reads src/config.json (max_result_additions) and falls back to a
built-in default when that file is absent. This lets the operator edit the
cap without editing this script; enforcement itself (check 5, below) is
unchanged.

Exit codes:
    0  Pass -- no violations.
    1  Violations found (listed one per line on stdout).
    2  Execution error: file missing/unreadable, malformed JSON, duplicate
       task_name within one file, or a missing/unknown causation_claim.
       Ambiguous input is never guessed at.
"""

from __future__ import annotations

import argparse
import json
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

#: Ordered claim ladder. A higher rank is a stronger causal claim.
_CLAIM_RANK: dict[str, int] = {
    "descriptive": 0,
    "correlation_only": 1,
    "causal_evidence": 2,
}

#: The only value that counts as the verification-loop exploratory marker.
_EXPLORATORY_MARKER = "verification_exploratory"

#: Ceiling for exploratory additions (see check 4).
_ADDITION_CLAIM_CEILING = "correlation_only"

# ---------------------------------------------------------------------------
# Self-explaining exit codes
#
# This is an assurance tool: exit 1 means the guarantee it exists to hold was
# broken, and the only correct response is to stop and show the operator. So
# the tool says that itself, on stderr and in --help, rather than pointing at a
# document -- a pointer into prose dies the moment the prose is reorganised.
# ---------------------------------------------------------------------------

#: Shown as the --help epilog, so the code list travels with the tool and
#: cannot be looked for in a document that has since been reorganised.
_EXIT_HELP = """\
Exit codes:
  0  Pass -- the result lock is intact. Nothing to do.
  1  Violations found, listed one per line on stdout. The guarantee this tool
     exists to hold was broken. Stop and surface the violation lines to the
     operator. Do not hand-edit results or the baseline to make this check
     pass, and do not proceed as if it had passed.
  2  Execution error -- a file is missing or unreadable, the JSON is malformed,
     a task_name is duplicated, or a causation_claim is missing or unknown. The
     message on stderr names the cause. Ambiguous input is never guessed at:
     fix the input and re-run.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exit2(message: str) -> NoReturn:
    """Print *message* to stderr and exit with code 2."""
    print(message, file=sys.stderr)
    sys.exit(2)


def _load_results(path_str: str, label: str) -> dict[str, dict]:
    """Load an AnalysisOutput JSON and index its results by task_name.

    Args:
        path_str: Path to the AnalysisOutput JSON file.
        label: Human-facing name for this file ("baseline" / "current"), used
            in error messages.

    Returns:
        A mapping of ``task_name`` -> the SingleAnalysisResult object.

    Raises:
        SystemExit(2): If the file is missing/unreadable, is not valid JSON,
            has no usable ``results`` array, contains a result without a
            string ``task_name``, or repeats a ``task_name``.
    """
    path = Path(path_str)
    if not path.exists():
        _exit2(f"{label} file not found: '{path}'")

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        _exit2(f"{label} file '{path}' is not valid JSON: {exc}")
    except OSError as exc:
        _exit2(f"Failed to read {label} file '{path}': {exc}")

    if not isinstance(data, dict):
        _exit2(f"{label} file '{path}' is malformed: top-level value is not an object.")

    results = data.get("results")
    if not isinstance(results, list):
        _exit2(
            f"{label} file '{path}' is malformed: 'results' must be an array "
            f"(got {type(results).__name__})."
        )

    indexed: dict[str, dict] = {}
    for position, entry in enumerate(results):
        if not isinstance(entry, dict):
            _exit2(
                f"{label} file '{path}' is malformed: results[{position}] is not "
                f"an object."
            )
        task_name = entry.get("task_name")
        if not isinstance(task_name, str) or not task_name:
            _exit2(
                f"{label} file '{path}' is malformed: results[{position}] has no "
                f"usable 'task_name'."
            )
        if task_name in indexed:
            # Never guess which duplicate is authoritative.
            _exit2(
                f"{label} file '{path}' has a duplicate task_name "
                f"'{task_name}'. task_name must be unique within one file."
            )
        indexed[task_name] = entry

    return indexed


def _claim_rank(entry: dict, task_name: str, label: str) -> int:
    """Return the claim ladder rank for *entry*'s causation_claim.

    Raises:
        SystemExit(2): If causation_claim is absent or not a known value.
    """
    claim = entry.get("causation_claim")
    if claim is None:
        _exit2(
            f"{label}: task '{task_name}' has no 'causation_claim'. "
            f"The claim level cannot be compared."
        )
    if claim not in _CLAIM_RANK:
        _exit2(
            f"{label}: task '{task_name}' has an unknown causation_claim "
            f"'{claim}'. Known values: {sorted(_CLAIM_RANK)}."
        )
    return _CLAIM_RANK[claim]


# ---------------------------------------------------------------------------
# Core comparison
# ---------------------------------------------------------------------------


def compare(
    baseline: dict[str, dict], current: dict[str, dict], max_additions: int
) -> list[str]:
    """Compare baseline and current result sets and return violation messages.

    Args:
        baseline: task_name -> result, snapshotted before the verification loop.
        current: task_name -> result, as the AnalysisOutput stands now.
        max_additions: Maximum number of newly added tasks allowed.

    Returns:
        A list of violation strings; empty means the comparison passed.

    Raises:
        SystemExit(2): If a compared task has a missing/unknown
            causation_claim.
    """
    violations: list[str] = []

    # Check 1: no silent deletions.
    for task_name in baseline:
        if task_name not in current:
            violations.append(
                f"deleted_task: '{task_name}' exists in the baseline but is "
                f"absent from the current output. The verification loop must "
                f"not drop an analysis silently."
            )

    # Check 2: no claim upgrades on tasks common to both.
    for task_name in baseline:
        if task_name not in current:
            continue
        before = _claim_rank(baseline[task_name], task_name, "baseline")
        after = _claim_rank(current[task_name], task_name, "current")
        if after > before:
            violations.append(
                f"claim_upgraded: '{task_name}' moved from "
                f"'{baseline[task_name]['causation_claim']}' to "
                f"'{current[task_name]['causation_claim']}'. The verification "
                f"loop may only weaken a claim, never strengthen it after "
                f"seeing results."
            )

    # Checks 3 & 4: additions must be marked, and may not claim causal evidence.
    added = [name for name in current if name not in baseline]
    for task_name in sorted(added):
        entry = current[task_name]
        if entry.get("origin") != _EXPLORATORY_MARKER:
            violations.append(
                f"unmarked_addition: '{task_name}' is not in the baseline and "
                f"does not carry origin='{_EXPLORATORY_MARKER}'. Every task "
                f"added in the verification loop must be marked exploratory."
            )
        rank = _claim_rank(entry, task_name, "current")
        if rank > _CLAIM_RANK[_ADDITION_CLAIM_CEILING]:
            violations.append(
                f"addition_claim_too_strong: '{task_name}' claims "
                f"'{entry['causation_claim']}'. A task added in the "
                f"verification loop may claim at most "
                f"'{_ADDITION_CLAIM_CEILING}'."
            )

    # Check 5: addition count cap.
    if len(added) > max_additions:
        violations.append(
            f"too_many_additions: {len(added)} tasks were added "
            f"({', '.join(sorted(added))}); the cap is {max_additions}."
        )

    return violations


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare a pre-loop AnalysisOutput baseline with the current one "
            "and enforce the verification-loop direction lock: no claim upgrades, no "
            "silent deletions, additions marked and capped."
        ),
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--baseline",
        required=True,
        help=(
            "Path to the AnalysisOutput JSON snapshotted before the "
            "verification loop."
        ),
    )
    parser.add_argument(
        "--current",
        required=True,
        help="Path to the current AnalysisOutput JSON.",
    )
    parser.add_argument(
        "--max-additions",
        type=int,
        default=None,
        help=(
            "Maximum number of exploratory tasks may add. When "
            "omitted, the default comes from config.json "
            "(max_result_additions) or a built-in default when that file is "
            "absent."
        ),
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the check_result_lock CLI.

    Returns:
        0 when no violations are found, 1 when violations are found.
        Execution errors exit 2 directly.
    """
    args = _parse_args(argv)

    if args.max_additions is None:
        try:
            args.max_additions = harness_config.get_max_additions()
        except harness_config.HarnessConfigError as exc:
            _exit2(str(exc))

    if args.max_additions < 0:
        _exit2(f"--max-additions must be >= 0 (got {args.max_additions}).")

    baseline = _load_results(args.baseline, "baseline")
    current = _load_results(args.current, "current")

    violations = compare(baseline, current, args.max_additions)

    if violations:
        for message in violations:
            print(message)
        # The summary points at the lines just printed. stdout is block-buffered
        # when it is a pipe while stderr is not, so without this flush a caller
        # merging the two streams sees the summary above the lines it counts.
        sys.stdout.flush()
        print(
            f"FAIL: {len(violations)} result-lock violation(s). "
            f"Stop and surface the violation lines (printed on stdout) to the "
            f"operator. Do not hand-edit results or the baseline to make this "
            f"check pass.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: result lock intact "
        f"({len(baseline)} baseline task(s), "
        f"{len(current) - len(baseline)} addition(s), cap {args.max_additions})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
