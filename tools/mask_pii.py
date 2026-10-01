"""
mask_pii.py -- Replace PII column values with SHA-256 hex digests.

Usage:
    python tools/mask_pii.py --input <csv|xlsx|xls> --pii-columns "colA,colB" --output <csv> [--force]

Supported input formats:
    .csv          -- read with pd.read_csv(); utf-8-sig, then cp932 fallback
    .xlsx / .xls  -- read with pd.read_excel(engine='openpyxl'); requires openpyxl

Output is always written as CSV regardless of input format, and always in
UTF-8: the masked file is an internal artefact of ours, so downstream readers
are entitled to assume UTF-8 and no encoding fallback exists below this point.

Exit codes:
    0  success
    1  any error (same path, file exists without --force, self-check failure, etc.)
    2  the input CSV decoded as neither UTF-8 nor Shift_JIS (cp932)
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import pandas as pd


_HEX_CHARS = frozenset("0123456789abcdef")
_HASH_LEN = 64

# Boundary encodings, in the order they are tried. This ordering is load-bearing:
# cp932 bytes are almost always invalid UTF-8, so trying UTF-8 first cannot
# misread a cp932 file, whereas cp932-first would happily decode a UTF-8 file
# into mojibake and let it pass silently -- the worst failure mode.
BOUNDARY_ENCODINGS = ("utf-8-sig", "cp932")

UNDECODABLE_MESSAGE = (
    "入力ファイルの文字コードを判別できませんでした: {path}\n"
    "UTF-8 でも Shift_JIS でも読めませんでした。ファイルの文字コードを確認してください。"
)


# ---------------------------------------------------------------------------
# Self-explaining exit codes
#
# Exit 1 covers two very different situations, and the caller must not conflate
# them: most causes are self-correctable from the message (add --force, fix a
# column name), but a self-check failure means masking did not hold and the run
# must stop. So the tool says which kind it hit, on stderr and in --help. These
# strings name no document and no section on purpose: a pointer into prose dies
# the moment the prose is reorganised.
# ---------------------------------------------------------------------------

#: Appended to the self-check failure block -- the one exit-1 cause that is
#: never self-correctable.
_SELF_CHECK_NOTE = (
    "No output file was written. Do not proceed with unmasked data; stop and "
    "report this failure."
)

#: Shown as the --help epilog, so the code list travels with the tool and
#: cannot be looked for in a document that has since been reorganised.
_EXIT_HELP = """\
Exit codes:
  0  Success. The masked CSV was written and every masked value passed the
     post-masking self-check.
  1  Setup or masking error. Read the ERROR message on stderr -- it says which
     kind this is. Most causes are self-correctable: the output file already
     exists (re-run with --force), a --pii-columns name is not in the input
     (the available column names are listed), an unsupported extension, or a
     missing openpyxl. But a self-check failure is different: masking did not
     hold, no output was written, and the run must stop -- do not proceed with
     unmasked data and do not retry until the cause is understood.
  2  The input CSV decoded as neither UTF-8 nor Shift_JIS (cp932). Nothing was
     written and nothing is guessed at, because a mis-decoded file would be
     masked into silent mojibake. Stop and confirm the file's encoding with the
     operator.
"""


class UndecodableInputError(Exception):
    """The boundary CSV decoded as neither utf-8-sig nor cp932."""


def _read_boundary_csv(input_path: Path) -> tuple[pd.DataFrame, str]:
    """Read a user-supplied CSV, returning (df, encoding_label).

    Both attempts are strict -- no errors="replace" -- so a file we cannot
    decode stops loudly rather than yielding silently corrupted values.

    NOTE: profile_data.py carries a same-shaped helper on purpose. These tools
    are self-contained single-file scripts, so the duplicate is preferred over
    a shared module.
    """
    for encoding in BOUNDARY_ENCODINGS:
        try:
            return pd.read_csv(input_path, encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UndecodableInputError(UNDECODABLE_MESSAGE.format(path=input_path))


def _sha256_hex(value: object) -> str:
    """Return the full 64-char SHA-256 hex digest of str(value)."""
    return hashlib.sha256(str(value).encode()).hexdigest()


def mask_columns(df: pd.DataFrame, pii_columns: list[str]) -> pd.DataFrame:
    """Return a copy of df with each named column's non-NaN values replaced by SHA-256 hex digests.

    NaN values are preserved as NaN. Only columns present in df are processed.

    Args:
        df: Input DataFrame. Not mutated.
        pii_columns: Column names to hash.

    Returns:
        New DataFrame with masked columns.
    """
    df = df.copy()
    for col in pii_columns:
        if col not in df.columns:
            continue
        # Cast to object dtype first so SHA-256 hex strings can be assigned
        # regardless of the original dtype (e.g. int64 from xlsx integer columns).
        df[col] = df[col].astype(object)
        mask = df[col].notna()
        df.loc[mask, col] = df.loc[mask, col].map(_sha256_hex)
    return df


def self_check(df: pd.DataFrame, pii_columns: list[str]) -> list[str]:
    """Verify that every non-NaN value in each masked column is a 64-char lowercase hex string.

    Args:
        df: DataFrame after masking.
        pii_columns: Column names that were masked.

    Returns:
        List of violation messages (empty if all checks pass).
    """
    violations: list[str] = []
    for col in pii_columns:
        if col not in df.columns:
            continue
        non_nan = df[col].dropna()
        bad = non_nan[
            ~non_nan.astype(str).apply(
                lambda v: len(v) == _HASH_LEN and all(c in _HEX_CHARS for c in v)
            )
        ]
        if len(bad) > 0:
            violations.append(
                f"Column '{col}': {len(bad)} value(s) are not valid 64-char lowercase hex strings."
            )
    return violations


def _resolve_path(p: str) -> Path:
    return Path(p).resolve()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace PII column values with SHA-256 hex digests.",
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        required=True,
        metavar="FILE",
        help="Path to the input file (.csv, .xlsx, or .xls).",
    )
    parser.add_argument(
        "--pii-columns",
        required=True,
        metavar="COL1,COL2,...",
        help="Comma-separated list of column names to mask.",
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="CSV",
        help="Path for the masked output CSV file (always written as CSV).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the mask_pii CLI.

    Returns:
        0 on success, 1 on any error.
    """
    args = _parse_args(argv)

    input_path = _resolve_path(args.input)
    output_path = _resolve_path(args.output)
    pii_columns = [c.strip() for c in args.pii_columns.split(",") if c.strip()]

    # Safety guard 1: same path check
    if input_path == output_path:
        print(
            f"ERROR: --output resolves to the same path as --input: {input_path}\n"
            "Refusing to overwrite the input file.",
            file=sys.stderr,
        )
        return 1

    # Safety guard 2: output exists without --force
    if output_path.exists() and not args.force:
        print(
            f"ERROR: Output file already exists: {output_path}\n"
            "Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    # Read input
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        return 1

    suffix = input_path.suffix.lower()
    if suffix == ".csv":
        try:
            df, _source_encoding = _read_boundary_csv(input_path)
        except UndecodableInputError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(
                f"ERROR: Failed to read input CSV '{input_path}': {exc}",
                file=sys.stderr,
            )
            return 1
    elif suffix in (".xlsx", ".xls"):
        try:
            import openpyxl  # noqa: F401 -- presence check only
        except ImportError:
            print(
                f"ERROR: Reading '{suffix}' files requires the 'openpyxl' package, which is not installed.\n"
                "Install it with: pip install openpyxl",
                file=sys.stderr,
            )
            return 1
        try:
            df = pd.read_excel(input_path, engine="openpyxl")
        except Exception as exc:
            print(
                f"ERROR: Failed to read input Excel file '{input_path}': {exc}",
                file=sys.stderr,
            )
            return 1
    else:
        print(
            f"ERROR: Unsupported input file extension '{suffix}' for '{input_path}'.\n"
            "Supported extensions: .csv, .xlsx, .xls",
            file=sys.stderr,
        )
        return 1

    # Validate requested columns exist
    missing = [c for c in pii_columns if c not in df.columns]
    if missing:
        print(
            f"ERROR: The following --pii-columns are not present in the input file: {missing}\n"
            f"Available columns: {df.columns.tolist()}",
            file=sys.stderr,
        )
        return 1

    # Mask
    df_masked = mask_columns(df, pii_columns)

    # Self-check
    violations = self_check(df_masked, pii_columns)
    if violations:
        print("ERROR: Self-check failed after masking:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(_SELF_CHECK_NOTE, file=sys.stderr)
        return 1

    # Write output
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Always UTF-8, whatever the input was: the masked CSV is an internal
        # file, and everything below this point reads UTF-8 with no fallback.
        df_masked.to_csv(output_path, index=False, encoding="utf-8-sig")
    except Exception as exc:
        print(
            f"ERROR: Failed to write output CSV '{output_path}': {exc}",
            file=sys.stderr,
        )
        return 1

    # Summary (no data values)
    print(f"Masked columns : {pii_columns}")
    print(f"Row count      : {len(df_masked)}")
    print(f"Output written : {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
