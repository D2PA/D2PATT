"""
check_survival.py -- Verify approved analysis variables are usable after preprocessing.

After preprocessing, this tool checks, for each variable the human approved in
the analysis brief, whether the variable is in a state the team can actually
analyze: its column must exist in the preprocessed CSV AND carry signal (at
least two distinct non-null values).

Variable name matching is EXACT (whole-string equality against the CSV columns).
This is deliberate: deciding that a short/placeholder name in the brief "probably
means" some real column is a judgement, not a formal property, so it is NOT
mechanised here. Instead this tool raises the RESOLUTION of its output -- it
distinguishes "the column exists but collapsed" from "no column matched the
name" -- and leaves the (judgement) column-name resolution to the orchestrator.

Usage:
    python tools/check_survival.py --csv <preprocessed CSV path>
                                       --brief <brief JSON path>
                                       [--file <output JSON path>]

Behavior:
    Reads the brief JSON, takes its `variables_of_interest` array (a list of
    column-name strings), loads the CSV, and for each variable checks whether
    the column exists (exact match) and how many DISTINCT NON-NULL values it has.

    Per-variable status (three values):
        ok          -- column present with >= 2 distinct non-null values
                       (analysable).
        degenerate  -- column PRESENT but < 2 distinct non-null values (single
                       constant value or entirely null): the column exists but
                       genuinely collapsed and cannot be analysed.
        not_found   -- column NOT present in the CSV (the brief name did not
                       match any column exactly). The data may still exist under
                       a different name -- this is a name-resolution question,
                       not a genuine collapse.

    Overall status (three values; there is NO `all_survived` boolean -- the
    overall verdict uses the same vocabulary layer as the per-variable ones):
        all_usable        -- every variable is `ok`.
        needs_resolution  -- at least one `not_found` and NO `degenerate`:
                             column-name resolution may make these usable; this
                             is a "please check / resolve names" signal, not a
                             guarantee failure.
        has_degenerate    -- at least one `degenerate` (regardless of whether
                             any `not_found` is also present). A real collapse
                             outranks any name-resolution question, so this is
                             the most severe overall verdict.

    A single JSON object is printed to stdout:
        {"overall_status": <str>,
         "results": [{"variable": <str>, "status": <str>,
                      "distinct_non_null_count": <int>,
                      "non_null_count": <int | null>}, ...]}

    `non_null_count` is the number of NON-MISSING cells in the column (used at
    the plan-review gate to make join coverage visible). It is an int for `ok` and
    `degenerate`; for `not_found` it is null, because there is no column to
    count (unlike `distinct_non_null_count`, which stays 0 there for backward
    compatibility). It is informational only: the ok/degenerate/not_found
    classification, the overall status, and the exit codes are unchanged.

PII rule (absolute):
    Output contains ONLY column names, counts, and statuses. Raw cell values
    and the distinct values themselves are NEVER printed. The distinct COUNT
    is allowed; the distinct VALUES are forbidden. No df.head(), no sample
    rows, no value lists anywhere.

Exit codes:
    0  all_usable        -- every variable is `ok`.
    1  has_degenerate    -- at least one column collapsed (genuine problem;
                            surfaced to the human, never silently swallowed).
    2  usage error       -- missing args, file not found, CSV/JSON parse error,
                            or `variables_of_interest` missing from the brief.
    3  needs_resolution  -- at least one variable not found and none degenerate;
                            distinct from exit 1 so the orchestrator can run its
                            column-name resolution step instead of treating it
                            as a collapse. NOT a guarantee failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Per-variable status constants
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_DEGENERATE = "degenerate"
STATUS_NOT_FOUND = "not_found"

# ---------------------------------------------------------------------------
# Overall status constants (no `all_survived` boolean -- same vocabulary layer)
# ---------------------------------------------------------------------------

OVERALL_ALL_USABLE = "all_usable"
OVERALL_NEEDS_RESOLUTION = "needs_resolution"
OVERALL_HAS_DEGENERATE = "has_degenerate"

# ---------------------------------------------------------------------------
# Exit codes -- four distinguishable states; needs_resolution (3) is kept
# separate from has_degenerate (1) on purpose.
# ---------------------------------------------------------------------------

EXIT_ALL_USABLE = 0
EXIT_HAS_DEGENERATE = 1
EXIT_USAGE = 2
EXIT_NEEDS_RESOLUTION = 3

_EXIT_BY_OVERALL = {
    OVERALL_ALL_USABLE: EXIT_ALL_USABLE,
    OVERALL_HAS_DEGENERATE: EXIT_HAS_DEGENERATE,
    OVERALL_NEEDS_RESOLUTION: EXIT_NEEDS_RESOLUTION,
}

# ---------------------------------------------------------------------------
# Self-explaining exit codes
#
# The caller of this tool is an agent that may otherwise read any non-zero exit
# as "the tool broke". Two of the three non-zero codes here are the opposite:
# they are results the run is supposed to produce. So the tool states, on
# stderr and in --help, what each code means and what the next move is. These
# strings are self-contained on purpose -- they name no document and no section,
# because a pointer into prose dies the moment the prose is reorganised.
# ---------------------------------------------------------------------------

#: Printed to stderr when the run ends in has_degenerate (exit 1).
_DEGENERATE_NOTE = (
    "has_degenerate (exit 1): a variable exists but has collapsed (fewer than 2 "
    "distinct non-null values). This is a finding, not a tool failure. Report "
    "the affected variables upward (listed in the JSON on stdout); do not drop, "
    "rename, or substitute variables to make the check pass."
)

#: Printed to stderr when the run ends in needs_resolution (exit 3).
#:
#: Deliberately does NOT tell its reader to resolve the names: whoever runs
#: this check is usually not the role that owns the brief, and resolving a
#: short or placeholder name to a real column is a judgement that belongs to
#: that owner. So the first action named here is "pass it up".
_NEEDS_RESOLUTION_NOTE = (
    "needs_resolution (exit 3): some variables were not found by exact column "
    "name and none are degenerate. This is NOT a failure. Pass the JSON on "
    "stdout upward unchanged. Resolving the brief's variable names to the "
    "actual column names belongs to whoever owns the brief, and this check is "
    "re-run once they are resolved -- do not resolve or substitute them here."
)

#: Shown as the --help epilog, so the code list travels with the tool and
#: cannot be looked for in a document that has since been reorganised.
_EXIT_HELP = """\
Exit codes:
  0  all_usable -- every approved variable exists and carries signal. Nothing
     to do.
  1  has_degenerate -- a variable exists but has collapsed (fewer than 2
     distinct non-null values). This is a finding, not a tool failure. Report
     the affected variables upward; do not drop, rename, or substitute
     variables to make the check pass.
  2  usage error -- bad arguments, a missing file, or an unreadable CSV/JSON.
     The message on stderr names the cause. Fix the invocation or the input and
     re-run.
  3  needs_resolution -- some variables were not found by exact column name and
     none are degenerate. This is NOT a failure. Pass the JSON upward
     unchanged. Resolving the brief's variable names to the actual column names
     belongs to whoever owns the brief, and this check is re-run once they are
     resolved -- do not resolve or substitute them here.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exit2(message: str) -> None:
    """Print message to stderr and exit with the usage-error code (2)."""
    print(message, file=sys.stderr)
    sys.exit(EXIT_USAGE)


def _classify(distinct_non_null_count: int, present: bool) -> str:
    """Map a (presence, distinct-non-null-count) pair to a per-variable status.

    The decision rule itself is unchanged from the original tool (column must
    exist AND have >= 2 distinct non-null values to be ``ok``); only the
    classification of the failure cases is refined so that "column exists but
    collapsed" (``degenerate``) is separated from "no matching column"
    (``not_found``).

    Args:
        distinct_non_null_count: Number of distinct non-null values in the
            column. Ignored when ``present`` is False.
        present: Whether the column exists in the CSV (exact-name match).

    Returns:
        One of ``"ok"``, ``"degenerate"``, ``"not_found"``.
    """
    if not present:
        return STATUS_NOT_FOUND
    if distinct_non_null_count < 2:
        # 0 distinct = entirely null; 1 distinct = single constant. Either way
        # the column exists but carries no analysable signal.
        return STATUS_DEGENERATE
    return STATUS_OK


def _overall_status(results: list[dict]) -> str:
    """Derive the overall status from the per-variable statuses.

    Precedence: any ``degenerate`` -> ``has_degenerate`` (most severe; a real
    collapse outranks a name-resolution question). Otherwise any ``not_found``
    -> ``needs_resolution``. Otherwise ``all_usable``.

    An empty variable list is vacuously ``all_usable``.

    Args:
        results: Per-variable result dicts (each carrying a ``status``).

    Returns:
        One of ``"all_usable"``, ``"needs_resolution"``, ``"has_degenerate"``.
    """
    statuses = {r["status"] for r in results}
    if STATUS_DEGENERATE in statuses:
        return OVERALL_HAS_DEGENERATE
    if STATUS_NOT_FOUND in statuses:
        return OVERALL_NEEDS_RESOLUTION
    return OVERALL_ALL_USABLE


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


def check_survival(df: pd.DataFrame, variables: list[str]) -> dict:
    """Check whether each approved variable is usable after preprocessing.

    For each variable, determines whether its column exists in *df* (exact-name
    match) and how many distinct non-null values it carries, then assigns a
    per-variable status. The overall status summarises the set.

    Args:
        df: The preprocessed data.
        variables: Column names to check (the brief's
            ``variables_of_interest``).

    Returns:
        A dict of the form
        ``{"overall_status": str, "results": [{"variable": str,
        "status": str, "distinct_non_null_count": int,
        "non_null_count": int | None}, ...]}``.
        ``non_null_count`` is the number of non-missing cells (``None`` for
        ``not_found`` -- no column exists to count). Only column names,
        statuses, and counts appear -- never raw values.
    """
    results: list[dict] = []
    for variable in variables:
        present = variable in df.columns
        if present:
            # nunique(dropna=True) counts DISTINCT NON-NULL values only.
            distinct_non_null_count = int(df[variable].nunique(dropna=True))
            non_null_count = int(df[variable].notna().sum())
        else:
            distinct_non_null_count = 0
            non_null_count = None
        status = _classify(distinct_non_null_count, present)
        results.append(
            {
                "variable": variable,
                "status": status,
                "distinct_non_null_count": distinct_non_null_count,
                "non_null_count": non_null_count,
            }
        )

    return {"overall_status": _overall_status(results), "results": results}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _load_variables(brief_path: Path) -> list[str]:
    """Load and return ``variables_of_interest`` from the brief JSON.

    Args:
        brief_path: Path to the analysis-brief JSON file.

    Returns:
        The list of column-name strings.

    Raises:
        SystemExit(2): If the file is missing, not valid JSON, or lacks a
            list-valued ``variables_of_interest`` field.
    """
    if not brief_path.exists():
        _exit2(f"Brief file not found: '{brief_path}'")
    try:
        with open(brief_path, encoding="utf-8") as fh:
            brief = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _exit2(f"Failed to read brief JSON '{brief_path}': {exc}")

    if not isinstance(brief, dict) or "variables_of_interest" not in brief:
        _exit2(
            f"Brief '{brief_path}' is missing the 'variables_of_interest' field."
        )

    variables = brief["variables_of_interest"]
    if not isinstance(variables, list) or not all(
        isinstance(v, str) for v in variables
    ):
        _exit2(
            f"Brief '{brief_path}' field 'variables_of_interest' must be a list "
            f"of column-name strings."
        )
    return variables


def _load_csv(csv_path: Path) -> pd.DataFrame:
    """Load the preprocessed CSV into a DataFrame.

    Args:
        csv_path: Path to the preprocessed CSV file.

    Returns:
        The loaded DataFrame.

    Raises:
        SystemExit(2): If the file is missing or cannot be parsed as CSV.
    """
    if not csv_path.exists():
        _exit2(f"CSV file not found: '{csv_path}'")
    try:
        # utf-8-sig tolerates a BOM, matching mask_pii.py's reader.
        return pd.read_csv(csv_path, encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001 -- surface any parse failure as usage error
        _exit2(f"Failed to read CSV '{csv_path}': {exc}")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that each analysis variable the human approved is usable "
            "after preprocessing (column exists and has >= 2 distinct non-null "
            "values). Matching is exact; the tool separates 'column collapsed' "
            "(degenerate) from 'no matching column' (not_found).\n\n"
            "Output contains only column names, statuses, and counts -- never "
            "raw cell values."
        ),
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--csv",
        required=True,
        metavar="CSV",
        help="Path to the preprocessed CSV file.",
    )
    parser.add_argument(
        "--brief",
        required=True,
        metavar="JSON",
        help="Path to the analysis-brief JSON file (provides "
        "variables_of_interest).",
    )
    parser.add_argument(
        "--file",
        metavar="OUT",
        default=None,
        help="Optional path to also write the JSON result to (utf-8).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the check_survival CLI.

    Returns:
        0 if overall status is `all_usable`, 1 if `has_degenerate`,
        3 if `needs_resolution`, 2 on usage / I/O / parse errors.
    """
    args = _parse_args(argv)

    variables = _load_variables(Path(args.brief))
    df = _load_csv(Path(args.csv))

    report = check_survival(df, variables)

    # Serialise once; reuse for both stdout and the optional file.
    payload = json.dumps(report, ensure_ascii=False)

    if args.file is not None:
        out_path = Path(args.file)
        try:
            out_path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            _exit2(f"Failed to write result to '{out_path}': {exc}")

    print(payload)

    # The stderr notes below point at this JSON. stdout is block-buffered when
    # it is a pipe while stderr is not, so without this flush a caller merging
    # the two streams sees the note printed above the JSON it refers to.
    sys.stdout.flush()

    exit_code = _EXIT_BY_OVERALL[report["overall_status"]]
    if exit_code == EXIT_HAS_DEGENERATE:
        print(_DEGENERATE_NOTE, file=sys.stderr)
    elif exit_code == EXIT_NEEDS_RESOLUTION:
        print(_NEEDS_RESOLUTION_NOTE, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
