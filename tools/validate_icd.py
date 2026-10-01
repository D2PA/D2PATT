"""
validate_icd.py -- Validate a JSON file against an EBPM ICD schema.

Usage:
    python tools/validate_icd.py <schema_name> <target_json_path>
    python tools/validate_icd.py <schema_name> -   # read from stdin

Arguments:
    schema_name       One of: requirements-summary, analysis-brief,
                      analysis-output, report-spec, report-draft,
                      review-result, design-decision, verification-response,
                      research-output, mayor-feasibility-check, mayor-review.
                      The .json extension is optional.
    target_json_path  Path to the JSON file to validate, or '-' for stdin.

Exit codes:
    0  Validation passed.
    1  Validation errors (schema violations).
    2  Usage error: unknown schema name, schema load failure, file not found,
       or other I/O / parse error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path

import referencing
import referencing.jsonschema
from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCHEMA_DIR = Path(__file__).parent.parent / "agents_ICD" / "schemas"

_KNOWN_SCHEMAS: frozenset[str] = frozenset(
    {
        "requirements-summary",
        "analysis-brief",
        "analysis-output",
        "report-spec",
        "report-draft",
        "review-result",
        "design-decision",
        "verification-response",
        "research-output",
        "reference-values",
        "mayor-feasibility-check",
        "mayor-review",
    }
)

# ---------------------------------------------------------------------------
# Self-explaining exit codes
#
# Exit 1 says the JSON is wrong, not that the tool is. The tempting shortcut --
# loosening the schema until the document passes -- would silently retire the
# contract this tool exists to hold, so the tool rules it out in the message
# itself. These strings name no document and no section on purpose: a pointer
# into prose dies the moment the prose is reorganised.
# ---------------------------------------------------------------------------

#: Shown as the --help epilog, so the code list travels with the tool and
#: cannot be looked for in a document that has since been reorganised.
_EXIT_HELP = """\
Exit codes:
  0  Validation passed. Nothing to do.
  1  Schema violations, listed one per line on stderr as
     '<schema>: <jsonpath>: <message>' -- or '<schema>: <message>' when the
     error is on the root object and there is no path to name. The JSON is
     invalid, not the tool. Fix the JSON or return it to its author;
     do not edit the schema to make it pass.
  2  Usage error -- an unknown schema name, a schema that failed to load, a
     missing target file, or an unreadable/unparseable input. The message on
     stderr names the cause. Fix the invocation or the input and re-run.
"""


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------


def _build_registry() -> referencing.Registry:
    """Load all ICD schemas from the schema directory and return a Registry.

    All five schema files are registered by their filename (e.g.
    ``analysis-output.json``) so that cross-file ``$ref`` values like
    ``"$ref": "analysis-output.json"`` are resolved correctly.

    Returns:
        A ``referencing.Registry`` containing all ICD schemas.

    Raises:
        SystemExit(2): If any schema file cannot be read or parsed.
    """
    resources: list[tuple[str, referencing.Resource]] = []  # type: ignore[type-arg]
    for schema_file in _SCHEMA_DIR.glob("*.json"):
        try:
            with open(schema_file, encoding="utf-8") as fh:
                schema = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _exit2(f"Failed to load schema file '{schema_file.name}': {exc}")
        resources.append(
            (
                schema_file.name,
                referencing.Resource(
                    contents=schema,
                    specification=referencing.jsonschema.DRAFT7,
                ),
            )
        )

    if not resources:
        _exit2(f"No schema files found in '{_SCHEMA_DIR}'.")

    return referencing.Registry().with_resources(resources)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Path formatting
# ---------------------------------------------------------------------------


def _format_jsonpath(absolute_path: deque) -> str:  # type: ignore[type-arg]
    """Convert a jsonschema absolute_path deque to a human-readable JSON path.

    Examples:
        deque([])                               -> ''
        deque(['results', 0, 'causation_claim']) -> 'results[0].causation_claim'

    Args:
        absolute_path: The ``absolute_path`` attribute from a
            ``jsonschema.ValidationError``.

    Returns:
        A dot/bracket notation string. Empty string for the root object.
    """
    parts = list(absolute_path)
    if not parts:
        return ""

    tokens: list[str] = []
    for part in parts:
        if isinstance(part, int):
            tokens.append(f"[{part}]")
        elif tokens:
            tokens.append(f".{part}")
        else:
            tokens.append(str(part))
    return "".join(tokens)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exit2(message: str) -> None:
    """Print message to stderr and exit with code 2."""
    print(message, file=sys.stderr)
    sys.exit(2)


def _normalise_schema_name(raw: str) -> str:
    """Strip optional .json suffix and return the bare schema name."""
    if raw.endswith(".json"):
        return raw[:-5]
    return raw


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a JSON file against an EBPM ICD schema.\n\n"
            "Use '-' as target_json_path to read from stdin."
        ),
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "schema_name",
        metavar="schema_name",
        help=(
            "Schema to validate against. One of: "
            + ", ".join(sorted(_KNOWN_SCHEMAS))
            + ". The .json extension is optional."
        ),
    )
    parser.add_argument(
        "target_json_path",
        metavar="target_json_path",
        help="Path to the JSON file to validate, or '-' to read from stdin.",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------


def validate(schema_name: str, instance: object) -> list[str]:
    """Validate *instance* against the named ICD schema.

    Args:
        schema_name: Bare schema name (without ``.json``), e.g.
            ``"analysis-output"``.
        instance: Deserialised JSON object to validate.

    Returns:
        A list of formatted error strings.  Empty list means the instance is
        valid.  Each string has the form
        ``"<schema_name>: <jsonpath>: <message>"`` where ``<jsonpath>`` may
        be empty for root-level errors.

    Raises:
        SystemExit(2): If the schema file cannot be loaded or the schema
            itself is invalid.
    """
    schema_file = _SCHEMA_DIR / f"{schema_name}.json"
    if not schema_file.exists():
        _exit2(
            f"Schema file not found: '{schema_file}'. "
            f"Known schemas: {sorted(_KNOWN_SCHEMAS)}"
        )

    try:
        with open(schema_file, encoding="utf-8") as fh:
            schema = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _exit2(f"Failed to read schema '{schema_name}': {exc}")

    registry = _build_registry()

    try:
        validator = Draft7Validator(schema, registry=registry)
    except SchemaError as exc:
        _exit2(f"Schema '{schema_name}' is itself invalid: {exc.message}")

    errors: list[str] = []
    for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        path = _format_jsonpath(error.absolute_path)
        if path:
            errors.append(f"{schema_name}: {path}: {error.message}")
        else:
            errors.append(f"{schema_name}: {error.message}")
    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for the validate_icd CLI.

    Returns:
        0 on success, 1 on validation errors, 2 on usage / I/O errors.
    """
    args = _parse_args(argv)

    schema_name = _normalise_schema_name(args.schema_name)

    if schema_name not in _KNOWN_SCHEMAS:
        _exit2(
            f"Unknown schema name '{schema_name}'. "
            f"Known schemas: {sorted(_KNOWN_SCHEMAS)}"
        )

    # Read target JSON
    if args.target_json_path == "-":
        try:
            raw = sys.stdin.read()
        except OSError as exc:
            _exit2(f"Failed to read from stdin: {exc}")
    else:
        target_path = Path(args.target_json_path)
        if not target_path.exists():
            _exit2(f"Target file not found: '{target_path}'")
        try:
            with open(target_path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            _exit2(f"Failed to read target file '{target_path}': {exc}")

    try:
        instance = json.loads(raw)
    except json.JSONDecodeError as exc:
        _exit2(f"Target is not valid JSON: {exc}")

    errors = validate(schema_name, instance)

    if errors:
        for msg in errors:
            print(msg, file=sys.stderr)
        print(
            f"FAIL: {len(errors)} schema violation(s). The JSON is invalid, not "
            f"the tool. Fix the JSON or return it to its author; do not edit "
            f"the schema to make it pass.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {schema_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
