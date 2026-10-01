"""
profile_data.py -- Emit a STRUCTURAL profile of a data file as a DataAuditResult JSON.

Purpose:
    Used by the orchestrator (main) in the *no-security-concern*
    (security_concern=false) branch to obtain a DataAuditResult without ever
    reading raw row values into its own context (and therefore the API).
    Structure (shape, column names, missing counts, duplicate count) is a
    formal property of the file that a deterministic script computes exactly;
    no individual cell value is ever emitted.

Usage:
    python tools/profile_data.py --input <csv|xlsx|xls>

Supported input formats:
    .csv          -- read with pd.read_csv(); utf-8-sig, then cp932 fallback
    .xlsx / .xls  -- read with pd.read_excel(engine='openpyxl'); requires openpyxl

Output:
    A DataAuditResult JSON object is printed to stdout, conforming to
    agents_ICD/schemas/analysis-output.json#/$defs/DataAuditResult:
        n_rows, n_cols, columns, missing_counts, duplicate_rows, quality_issues
    quality_issues is always [] (PII detection is the security-concern path's
    job, handled by the data-audit subagent, not here).
    For CSV input the optional field source_encoding ("utf-8-sig" | "cp932")
    records which encoding actually decoded the file, so that preprocessing
    downstream reads the same raw file deliberately rather than guessing again.

    SAFETY: only df.shape / df.columns / df.isna().sum() / df.duplicated().sum()
    are used. No df.head()/tail()/to_string()/sample() and no individual cell
    value is ever printed. This is the core guarantee: even if a human wrongly
    declares "no concern", raw rows do not reach the API.

Exit codes:
    0  success
    1  any error (file not found, unreadable, unsupported extension, missing dep)
    2  the input CSV decoded as neither UTF-8 nor Shift_JIS (cp932)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


def profile_dataframe(df: pd.DataFrame) -> dict:
    """Return a structural DataAuditResult dict for df.

    Only structural aggregates are read -- never individual cell values.

    Args:
        df: Input DataFrame.

    Returns:
        Dict with keys n_rows, n_cols, columns, missing_counts,
        duplicate_rows, quality_issues (always []).
    """
    n_rows, n_cols = df.shape
    columns = [str(c) for c in df.columns.tolist()]
    # isna().sum() -> per-column missing counts; values are counts, not data.
    missing_counts = {str(col): int(count) for col, count in df.isna().sum().items()}
    duplicate_rows = int(df.duplicated().sum())
    return {
        "n_rows": int(n_rows),
        "n_cols": int(n_cols),
        "columns": columns,
        "missing_counts": missing_counts,
        "duplicate_rows": duplicate_rows,
        "quality_issues": [],
    }


def _resolve_path(p: str) -> Path:
    return Path(p).resolve()


# Boundary encodings, in the order they are tried. This ordering is load-bearing:
# cp932 bytes are almost always invalid UTF-8, so trying UTF-8 first cannot
# misread a cp932 file, whereas cp932-first would happily decode a UTF-8 file
# into mojibake and let it pass silently -- the worst failure mode.
BOUNDARY_ENCODINGS = ("utf-8-sig", "cp932")

UNDECODABLE_MESSAGE = (
    "入力ファイルの文字コードを判別できませんでした: {path}\n"
    "UTF-8 でも Shift_JIS でも読めませんでした。ファイルの文字コードを確認してください。"
)


class UndecodableInputError(Exception):
    """The boundary CSV decoded as neither utf-8-sig nor cp932."""


def _read_boundary_csv(input_path: Path) -> tuple[pd.DataFrame, str]:
    """Read a user-supplied CSV, returning (df, encoding_label).

    Both attempts are strict -- no errors="replace" -- so a file we cannot
    decode stops loudly rather than yielding silently corrupted values.

    NOTE: mask_pii.py carries a same-shaped helper on purpose. These tools are
    self-contained single-file scripts, so the duplicate is preferred over a
    shared module.
    """
    for encoding in BOUNDARY_ENCODINGS:
        try:
            return pd.read_csv(input_path, encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UndecodableInputError(UNDECODABLE_MESSAGE.format(path=input_path))


def _read_input(input_path: Path) -> tuple[pd.DataFrame, str | None]:
    """Read the input file by extension.

    Returns:
        (df, source_encoding). source_encoding is None for Excel input, which
        is not a text format and therefore has no encoding to report.

    Raises:
        ValueError: unsupported extension or missing dependency.
        UndecodableInputError: CSV readable as neither UTF-8 nor cp932.
    """
    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        return _read_boundary_csv(input_path)
    if suffix in (".xlsx", ".xls"):
        try:
            import openpyxl  # noqa: F401 -- presence check only
        except ImportError as exc:
            raise ValueError(
                f"Reading '{suffix}' files requires the 'openpyxl' package, which is not installed.\n"
                "Install it with: pip install openpyxl"
            ) from exc
        return pd.read_excel(input_path, engine="openpyxl"), None
    raise ValueError(
        f"Unsupported input file extension '{suffix}' for '{input_path}'.\n"
        "Supported extensions: .csv, .xlsx, .xls"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit a structural DataAuditResult JSON for a data file (CSV/Excel). "
        "Structure only -- no row values are ever read or printed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        required=True,
        metavar="FILE",
        help="Path to the input file (.csv, .xlsx, or .xls).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the profile_data CLI.

    Returns:
        0 on success, 1 on any error, 2 on an undecodable input CSV.
    """
    # Force UTF-8 stdout so Japanese column names are not garbled when the OS
    # locale is cp932 (every Windows municipal machine -- a product
    # requirement; the matching *input*-side requirement is handled by
    # _read_boundary_csv, which reads cp932-saved CSVs). Without this, the
    # orchestrator reads mojibake and re-runs the script a second time under
    # UTF-8. reconfigure() is Python 3.7+; the same pattern is used by the
    # outcome_descriptives step in analysis_log.md.
    # stderr gets the same treatment: the undecodable-input message is Japanese
    # and is read on precisely the cp932 machines it is written for, so leaving
    # stderr on the locale codec would garble the one message meant to explain
    # the failure.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            # Non-reconfigurable stream (e.g. already-wrapped in tests); the JSON is
            # ASCII-safe except for column names, which json.dumps escapes on the
            # rare stream that cannot be set to UTF-8.
            pass

    args = _parse_args(argv)
    input_path = _resolve_path(args.input)

    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        return 1

    try:
        df, source_encoding = _read_input(input_path)
    except UndecodableInputError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: Failed to read input file '{input_path}': {exc}", file=sys.stderr)
        return 1

    result = profile_dataframe(df)
    if source_encoding is not None:
        # Additive/optional: lets preprocessing re-read the raw file with the
        # encoding we already proved works, instead of detecting a second time.
        result["source_encoding"] = source_encoding
    # Structural JSON only -- this is the sole thing written to stdout.
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
