"""
lint_results.py -- Statistical integrity linter for EBPM analysis outputs.

Usage:
    python tools/lint_results.py [--analysis-output <json>] [--report <md>]
                                     [--strict]

At least one of --analysis-output or --report must be provided.

Checks:
    1. stat_reporting     -- crosstab results missing chi2 / cramers_v
                             (needs --analysis-output)
    2. causal_expression  -- causal language unsupported by causation_claim
                             (needs --report; --analysis-output lets the check
                             cross-reference causation_claim per task)

This tool does not police how many people a reported figure covers. Suppressing
small cells was removed from the tool: the operator holds the source data
already, so hiding an aggregate protects nothing here, and deciding what may be
published outside the office is the publishing officer's job.

Exit codes:
    0  no violations, or warnings present without --strict
    1  warnings present and --strict is set
    2  execution error (file read failure, no targets specified, etc.)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CAUSAL_PHRASES = [
    "影響している", "影響を与える", "引き起こす", "規定する",
    "左右する", "のせいで", "につながった", "をもたらす",
    "決定する", "決定している",
    # report-writer.md で causal_evidence のみ許可される表現（T-3対応）
    "効果が確認された", "効果があった", "効果を与えた",
    "が原因", "によって生じた",
]

CROSSTAB_METHOD_KEYWORDS = ["chi2", "crosstab", "クロス集計", "contingency"]

# Statistics required when a crosstab/chi2 method is detected
_REQUIRED_CROSSTAB_STATS = ["chi2", "cramers_v"]

# ---------------------------------------------------------------------------
# Self-explaining exit codes
#
# This linter's exit code is easy to misread in both directions: exit 0 does NOT
# mean "clean" (warnings without --strict still exit 0), and exit 1 does not
# mean the tool broke. So the tool states what each code means, and what the
# next move is, on stderr and in --help. These strings name no document and no
# section on purpose: a pointer into prose dies the moment the prose is
# reorganised.
# ---------------------------------------------------------------------------

#: Appended to every exit-2 error; they are all invocation or input problems.
_ERROR_NEXT_STEP = "Fix the invocation or input and re-run."

#: Printed to stderr when --strict turns warnings into exit 1.
_STRICT_NOTE = (
    "warnings present under --strict (exit 1): address the findings listed on "
    "stdout; do not edit outputs merely to silence the linter."
)

#: Shown as the --help epilog, so the code list travels with the tool and
#: cannot be looked for in a document that has since been reorganised.
_EXIT_HELP = """\
Exit codes:
  0  No violations, OR warnings present without --strict. Warnings alone are
     not a failure here: without --strict this tool exits 0 even when it
     printed WARNING lines. Read the lines on stdout, not the exit code, to
     learn whether anything was found.
  1  Warnings present and --strict was set. Address the findings listed on
     stdout; do not edit outputs merely to silence the linter.
  2  Execution error -- no target was given, or a file could not be read. The
     ERROR line on stderr names the cause. Fix the invocation or input and
     re-run.
"""


def _error(message: str) -> None:
    """Print an exit-2 error to stderr, with the next move stated."""
    print(f"ERROR: {message} {_ERROR_NEXT_STEP}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Data classes (lightweight)
# ---------------------------------------------------------------------------

class LintMessage:
    """Represents a single lint finding."""

    def __init__(self, level: str, check_id: str, file: str, location: str, message: str) -> None:
        self.level = level          # "WARNING" | "SKIPPED"
        self.check_id = check_id
        self.file = file
        self.location = location
        self.message = message

    def __str__(self) -> str:
        return f"{self.level} [{self.check_id}] {self.file} {self.location}: {self.message}"


# ---------------------------------------------------------------------------
# Check 1: statistical reporting requirements on analysis-output JSON
# ---------------------------------------------------------------------------

def _is_crosstab_method(method: str) -> bool:
    """Return True if the method string indicates a cross-tabulation analysis."""
    lower = method.lower()
    return any(kw in lower for kw in CROSSTAB_METHOD_KEYWORDS)


def check_stat_reporting(analysis_output_path: Path) -> list[LintMessage]:
    """Check that crosstab results include required statistics (chi2 + cramers_v).

    Args:
        analysis_output_path: Path to the AnalysisOutput JSON file.

    Returns:
        List of LintMessage objects.
    """
    messages: list[LintMessage] = []
    rel = str(analysis_output_path)

    try:
        with analysis_output_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        messages.append(LintMessage(
            "WARNING", "stat_reporting", rel, "",
            f"could not read analysis-output JSON: {exc}"
        ))
        return messages

    results = data.get("results", [])
    if not isinstance(results, list):
        messages.append(LintMessage(
            "WARNING", "stat_reporting", rel, "",
            "JSON 'results' field is not a list"
        ))
        return messages

    for result in results:
        task_name = result.get("task_name", "<unknown>")
        method = result.get("method", "")
        if not _is_crosstab_method(method):
            continue
        statistics = result.get("statistics", {}) or {}
        for required_stat in _REQUIRED_CROSSTAB_STATS:
            if required_stat not in statistics:
                messages.append(LintMessage(
                    "WARNING", "stat_reporting", rel,
                    f"task '{task_name}'",
                    f"statistics missing '{required_stat}'"
                    f" (required for crosstab method)"
                ))

    return messages


# ---------------------------------------------------------------------------
# Check 2: causal expression in Markdown report
# ---------------------------------------------------------------------------

def _find_causal_phrases_in_line(line: str) -> list[str]:
    """Return list of causal phrases found in line."""
    return [phrase for phrase in CAUSAL_PHRASES if phrase in line]


def _load_analysis_results(analysis_output_path: Path) -> list[dict] | None:
    """Load and return results list from analysis-output JSON, or None on error."""
    try:
        with analysis_output_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        results = data.get("results")
        if isinstance(results, list):
            return results
    except Exception:
        pass
    return None


def check_causal_expression(
    report_path: Path,
    analysis_output_path: Path | None = None,
) -> list[LintMessage]:
    """Scan a Markdown report for causal language.

    When analysis_output_path is also provided, cross-references causation_claim
    to suppress warnings where causation_claim == 'causal_evidence'.

    Args:
        report_path: Path to the Markdown report file.
        analysis_output_path: Optional path to AnalysisOutput JSON for correlation.

    Returns:
        List of LintMessage objects.
    """
    messages: list[LintMessage] = []
    rel = str(report_path)

    try:
        report_text = report_path.read_text(encoding="utf-8")
    except Exception as exc:
        messages.append(LintMessage(
            "WARNING", "causal_expression", rel, "",
            f"could not read report file: {exc}"
        ))
        return messages

    lines = report_text.splitlines()

    # Load analysis results for cross-referencing (may be None)
    analysis_results: list[dict] | None = None
    if analysis_output_path is not None:
        analysis_results = _load_analysis_results(analysis_output_path)

    for line_num, line in enumerate(lines, start=1):
        found_phrases = _find_causal_phrases_in_line(line)
        if not found_phrases:
            continue

        for phrase in found_phrases:
            if analysis_results is not None:
                # Try to find a result whose key_finding or interpretation_note
                # matches this line — conservative: warn unless we find causal_evidence.
                causation_claim = _infer_causation_claim(line, analysis_results)
                if causation_claim == "causal_evidence":
                    # Phrase is backed by causal inference; skip warning
                    continue
                messages.append(LintMessage(
                    "WARNING", "causal_expression", rel,
                    f"line={line_num}",
                    f"'{phrase}' may imply causation"
                    f" (causation_claim is '{causation_claim}')"
                ))
            else:
                # Report-only mode: list occurrence
                messages.append(LintMessage(
                    "WARNING", "causal_expression", rel,
                    f"line={line_num}",
                    f"'{phrase}' may imply causation"
                ))

    return messages


def _infer_causation_claim(line: str, results: list[dict]) -> str:
    """Best-effort: find a result whose task_name or key_finding text overlaps with line.

    Returns the causation_claim if a match is found, else 'correlation_only'
    (conservative: assume worst case when no match).
    """
    # Try substring match between result task_name / key_finding and the line
    for result in results:
        task_name = result.get("task_name", "")
        key_finding = result.get("key_finding", "")
        interpretation = result.get("interpretation_note", "")
        # If any content from this result appears in the line, use its causation_claim
        for text in (task_name, key_finding, interpretation):
            if text and text in line:
                return result.get("causation_claim", "correlation_only")
    # Conservative fallback
    if results:
        # Use the first result's causation_claim as a last resort if there's only one result
        if len(results) == 1:
            return results[0].get("causation_claim", "correlation_only")
    return "correlation_only"


# ---------------------------------------------------------------------------
# Output and summary
# ---------------------------------------------------------------------------

def _print_messages(messages: list[LintMessage]) -> None:
    """Print all messages to stdout."""
    for msg in messages:
        print(str(msg))


def _print_summary(warnings: int, skipped: int) -> None:
    """Print the lint summary block."""
    print("--- lint summary ---")
    print(f"warnings: {warnings}, skipped: {skipped}")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Statistical integrity linter for EBPM analysis outputs.",
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--analysis-output",
        metavar="JSON",
        help="Path to the AnalysisOutput JSON file.",
    )
    parser.add_argument(
        "--report",
        metavar="MD",
        help="Path to the Markdown report file.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any warnings are present (default: exit 0 on warnings).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Run the lint_results CLI.

    Returns:
        0 on clean / warnings without --strict
        1 on warnings with --strict
        2 on execution errors
    """
    args = _parse_args(argv)

    # Validate: at least one target must be specified
    if not any([args.analysis_output, args.report]):
        _error(
            "at least one of --analysis-output or --report must be specified."
        )
        return 2

    all_messages: list[LintMessage] = []

    # Check 1: statistical reporting
    if args.analysis_output:
        analysis_path = Path(args.analysis_output).resolve()
        if not analysis_path.exists():
            _error(f"--analysis-output file not found: {analysis_path}")
            return 2
        try:
            msgs = check_stat_reporting(analysis_path)
            all_messages.extend(msgs)
        except Exception as exc:
            _error(f"stat_reporting check failed: {exc}")
            return 2

    # Check 2: causal expression
    if args.report:
        report_path = Path(args.report).resolve()
        if not report_path.exists():
            _error(f"--report file not found: {report_path}")
            return 2
        analysis_path_for_report: Path | None = None
        if args.analysis_output:
            analysis_path_for_report = Path(args.analysis_output).resolve()
        try:
            msgs = check_causal_expression(report_path, analysis_path_for_report)
            all_messages.extend(msgs)
        except Exception as exc:
            _error(f"causal_expression check failed: {exc}")
            return 2

    # Print all messages
    _print_messages(all_messages)

    # Count by level
    warnings = sum(1 for m in all_messages if m.level == "WARNING")
    skipped = sum(1 for m in all_messages if m.level == "SKIPPED")

    # Summary
    if all_messages:
        _print_summary(warnings, skipped)
    else:
        print("OK: no issues found")

    if warnings > 0 and args.strict:
        # The note points at the findings just printed. stdout is block-buffered
        # when it is a pipe while stderr is not, so without this flush a caller
        # merging the two streams sees the note above the findings.
        sys.stdout.flush()
        print(_STRICT_NOTE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
