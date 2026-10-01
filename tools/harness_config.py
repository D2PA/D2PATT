"""
harness_config.py -- Shared config.json reader for the harness's round/addition caps.

Design principle: "control by LLM, guarantee by script." round_counter.py and
check_result_lock.py each enforce a numeric cap (design-loop rounds,
verification-loop rounds, exploratory result additions) deterministically. The
caps themselves used to be hardcoded constants; this module lets a municipal
operator edit them in one place (src/config.json) while keeping enforcement in
the CLIs, unchanged.

Config file path resolution, in precedence order:
    1. HARNESS_CONFIG_PATH environment variable (used by tests so the real
       src/config.json is never read or polluted).
    2. <this file's parent>/../config.json (i.e. src/config.json), resolved
       from __file__ so it does not depend on the current working directory.

Config file shape (all keys optional; a missing file is not an error). Each
key maps to an object, not a bare number, so a human (or the PM, reading it
aloud in chat) can see what the value is for without leaving the file:
    {
        "design_max_rounds": {
            "value": 3,
            "description": "何のための値かを説明する日本語の文章（省略可）"
        },
        "verification_max_rounds": {"value": 2, "description": "..."},
        "max_result_additions": {"value": 2, "description": "..."}
    }
"description" may be omitted entirely (an operator who deletes it by mistake
must not lose the feature); "value" may not be.

Each key also has a maximum (see _KEY_MAXIMUMS). This is not a business rule
about how many rounds or additions are appropriate -- it is a sanity check
that catches an operator's typo in config.json (e.g. an extra digit) before it
turns into a needlessly long or expensive run. The ceiling is generous
relative to the built-in defaults (3, 2, 2); a legitimate use is not expected
to ever need to reach it. If one genuinely does, the fix is to edit
_KEY_MAXIMUMS in this file, not to work around the check.

This module never calls sys.exit() from load_config() / get_max_rounds() /
get_max_additions(). All config problems -- a corrupt file, an unknown key, a
value of the wrong type, a value outside its [minimum, maximum] range -- are
raised as HarnessConfigError so the calling CLI decides the exit code and
wording. The small main() CLI at the bottom of this file is the one exception:
it exists only so an operator can mechanically confirm a hand- or chat-driven
edit to config.json is valid, and it does call sys.exit() like the other
tools' CLIs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default config file location, relative to this module (tools/ -> src/config.json).
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.json"

#: Environment variable that, when set, overrides DEFAULT_CONFIG_PATH (for tests).
_CONFIG_PATH_ENV = "HARNESS_CONFIG_PATH"

#: Built-in values used for any key the config file does not set, and for the
#: whole config when the file does not exist at all.
_BUILTIN_DEFAULTS: dict[str, int] = {
    "design_max_rounds": 3,
    "verification_max_rounds": 2,
    "max_result_additions": 2,
}

#: Minimum legal value per key. max_result_additions alone allows 0, matching
#: check_result_lock.py's own --max-additions floor.
_KEY_MINIMUMS: dict[str, int] = {
    "design_max_rounds": 1,
    "verification_max_rounds": 1,
    "max_result_additions": 0,
}

#: Maximum legal value per key. This is not a business rule -- it is a sanity
#: ceiling that catches an operator's input mistake (e.g. an extra digit) in
#: config.json before it turns into a very long or very expensive run. It is
#: generous relative to the built-in defaults (3, 2, 2): a legitimate use is
#: not expected to ever need this much headroom.
_KEY_MAXIMUMS: dict[str, int] = {
    "design_max_rounds": 10,
    "verification_max_rounds": 10,
    "max_result_additions": 10,
}

#: Maps a round_counter.py --phase tag to the config key that supplies its
#: default --max-rounds.
_PHASE_TO_KEY: dict[str, str] = {
    "design": "design_max_rounds",
    "verification": "verification_max_rounds",
}


class HarnessConfigError(Exception):
    """Raised for any problem loading or validating src/config.json.

    Callers (the round_counter.py / check_result_lock.py CLIs) catch this and
    decide their own exit code and message wrapping; this module never exits
    the process itself.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    """Return the active config file path.

    Precedence: the HARNESS_CONFIG_PATH env override (tests), then
    DEFAULT_CONFIG_PATH (src/config.json).
    """
    override = os.environ.get(_CONFIG_PATH_ENV)
    if override:
        return Path(override)
    return DEFAULT_CONFIG_PATH


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config() -> dict[str, int]:
    """Load and validate src/config.json, merged over the built-in defaults.

    Returns:
        A dict with all three keys (``design_max_rounds``,
        ``verification_max_rounds``, ``max_result_additions``) set to
        integers: the value from the config file where present and valid,
        otherwise the built-in default.

    Raises:
        HarnessConfigError: If the config file exists but is unparseable,
            has a non-object top level, names an unknown key, gives a known
            key something other than a ``{"value": ..., "description": ...}``
            object, gives that object an unknown field or omits ``value``,
            gives ``value`` a non-integer (or bool) value or one outside its
            [minimum, maximum] range, or gives ``description`` a non-string
            value.
    """
    path = _config_path()
    if not path.exists():
        return dict(_BUILTIN_DEFAULTS)

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise HarnessConfigError(
            f"設定ファイル '{path}' は不正な JSON です: {exc}"
        ) from exc
    except OSError as exc:
        raise HarnessConfigError(
            f"設定ファイル '{path}' を読み込めませんでした: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise HarnessConfigError(
            f"設定ファイル '{path}' の形式が不正です: 最上位の値はオブジェクトである"
            f"必要があります（実際の型: {type(data).__name__}）。"
        )

    unknown_keys = sorted(set(data) - set(_BUILTIN_DEFAULTS))
    if unknown_keys:
        raise HarnessConfigError(
            f"設定ファイル '{path}' に未知のキーがあります: {unknown_keys}。"
            f"使えるキーは {sorted(_BUILTIN_DEFAULTS)} です。"
            f"打ち間違いの可能性があるため、値を無視せずエラーにしています。"
        )

    merged = dict(_BUILTIN_DEFAULTS)
    for key, entry in data.items():
        if not isinstance(entry, dict):
            raise HarnessConfigError(
                f"設定ファイル '{path}' のキー '{key}' はオブジェクト"
                f'（{{"value": ..., "description": ...}}）である必要があります'
                f"（実際の型: {type(entry).__name__}）。"
            )

        unknown_fields = sorted(set(entry) - {"value", "description"})
        if unknown_fields:
            raise HarnessConfigError(
                f"設定ファイル '{path}' のキー '{key}' に未知の項目があります: "
                f"{unknown_fields}。使える項目は ['description', 'value'] です。"
                f"打ち間違いの可能性があるため、値を無視せずエラーにしています。"
            )

        if "value" not in entry:
            raise HarnessConfigError(
                f"設定ファイル '{path}' のキー '{key}' に 'value' がありません。"
            )
        value = entry["value"]

        # bool is a subclass of int; reject it explicitly to catch type drift.
        if not isinstance(value, int) or isinstance(value, bool):
            raise HarnessConfigError(
                f"設定ファイル '{path}' のキー '{key}' は整数である必要があります"
                f"（実際の型: {type(value).__name__}）。"
            )
        minimum = _KEY_MINIMUMS[key]
        if value < minimum:
            raise HarnessConfigError(
                f"設定ファイル '{path}' のキー '{key}' は {minimum} 以上である"
                f"必要があります（指定値: {value}）。"
            )
        maximum = _KEY_MAXIMUMS[key]
        if value > maximum:
            raise HarnessConfigError(
                f"設定ファイル '{path}' のキー '{key}' は {maximum} 以下である"
                f"必要があります（指定値: {value}）。意図的に大きな値が必要な"
                f"場合は、コードの `_KEY_MAXIMUMS` を見直してください。"
            )

        description = entry.get("description")
        if description is not None and not isinstance(description, str):
            raise HarnessConfigError(
                f"設定ファイル '{path}' のキー '{key}' の 'description' は文字列で"
                f"ある必要があります（実際の型: {type(description).__name__}）。"
            )

        merged[key] = value

    return merged


def get_max_rounds(phase: str) -> int:
    """Return the default --max-rounds for round_counter.py's *phase* tag.

    Args:
        phase: The --phase tag ("design" or "verification").

    Returns:
        The configured (or built-in default) round cap for that phase.

    Raises:
        HarnessConfigError: If *phase* is not a recognised tag, or if
            load_config() itself fails validation.
    """
    key = _PHASE_TO_KEY.get(phase)
    if key is None:
        raise HarnessConfigError(
            f"未知の --phase '{phase}' には既定の上限がありません。"
            f"--max-rounds を明示するか、'design' または 'verification' を"
            f"使ってください。"
        )
    return load_config()[key]


def get_max_additions() -> int:
    """Return the default --max-additions for check_result_lock.py.

    Raises:
        HarnessConfigError: If load_config() fails validation.
    """
    return load_config()["max_result_additions"]


# ---------------------------------------------------------------------------
# Main
#
# load_config() / get_max_rounds() / get_max_additions() never call
# sys.exit() -- that discipline is for the library API, which round_counter.py
# and check_result_lock.py call mid-CLI-run and must be able to handle
# themselves. main() below is the one place in this file that behaves like a
# standalone CLI (matching round_counter.py / check_result_lock.py's own
# Main sections), so it alone uses _exit2 / _EXIT_HELP.
# ---------------------------------------------------------------------------

#: Shown as the --help epilog, so the code list travels with the tool and
#: cannot be looked for in a document that has since been reorganised.
_EXIT_HELP = """\
Exit codes:
  0  Success. stdout is the resolved {"design_max_rounds": ...,
     "verification_max_rounds": ..., "max_result_additions": ...} JSON.
  2  config.json exists but is invalid. The reason is on stderr; fix
     config.json and re-run -- do not guess at the resolved values.
"""


def _exit2(message: str) -> NoReturn:
    """Print *message* to stderr and exit with code 2."""
    print(message, file=sys.stderr)
    sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    """Resolve and print src/config.json (merged over built-in defaults).

    No subcommands or flags besides --help: this tool exists only so an
    operator (or the PM acting on their behalf) can mechanically confirm a
    hand- or chat-driven edit to config.json is valid, and see the resulting
    values, before proceeding.

    Returns:
        0 on success (resolved config printed as one JSON line to stdout).
    """
    parser = argparse.ArgumentParser(
        description=(
            "Resolve and print src/config.json (merged over built-in "
            "defaults) as a single JSON line. Used to mechanically confirm "
            "a hand- or chat-driven edit to config.json is valid."
        ),
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args(argv)
    try:
        config = load_config()
    except HarnessConfigError as exc:
        _exit2(str(exc))
    print(json.dumps(config, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
