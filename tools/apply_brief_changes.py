"""apply_brief_changes.py -- 変更点の一覧のとおりに分析計画書を書き換える。

analysis-designer が返すのは計画の全文ではなく、変更点の一覧（design-decision.json の
``$defs/BriefChanges``）である。このツールは、その一覧を受け取り、計画書
（analysis-brief.json 様式）へ機械的に反映する。designer が計画の全文を書き直す道を
塞ぐことで、触ってはいけない欄（pii_columns など）の書き換えや、宛先の取り違えを
そもそも成立させない。

使い方:
    python tools/apply_brief_changes.py --brief <計画書のパス>
                                            --changes <変更点の一覧のパス>

断る条件（どれか一つでも当てはまれば、計画書を一字も変えずに終了番号1で終わる）:
    1. 変更点の一覧が BriefChanges の様式に合わない
    2. 宛先が実在しない（タスク名・欄の名前）
    3. 追加するタスク名が重複している
    4. 触れてはいけない欄を変えようとしている
    5. 同じ宛先を一度に二回変えている
    6. 削除するタスクを、同時に書き換え・追加している
    7. 書き換えたあとの計画書が analysis-brief の様式に合わない

終了番号:
    0  書き換えた（変更が空のときは、様式を確かめただけで終わる）
    1  断った（理由を標準出力に示す）
    2  ツール自体が動かなかった（標準エラー出力に原因を示す）
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from collections import deque
from pathlib import Path
from typing import Any

import referencing
import referencing.jsonschema
from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: ICD スキーマの置き場。validate_icd.py と同じ場所を見る。
_SCHEMA_DIR = Path(__file__).parent.parent / "agents_ICD" / "schemas"

#: 変更点の一覧が従うべき様式（design-decision.json の中の定義）。
_CHANGES_SCHEMA_REF = "design-decision.json#/$defs/BriefChanges"

#: 計画書が従うべき様式。
_BRIEF_SCHEMA_REF = "analysis-brief.json"

#: field_changes で指定できない計画書の欄。課長が確認の場で決める欄である。
_FORBIDDEN_BRIEF_FIELDS: tuple[str, ...] = (
    "pii_columns",
)

#: field_changes で指定できない欄（タスクは専用の操作で変更する）。
_TASKS_FIELD = "tasks"

#: task_changes で指定できないタスクの欄（タスク名は変更しない）。
_TASK_NAME_FIELD = "name"

# ---------------------------------------------------------------------------
# 終了番号の説明
#
# 番号の意味は、文書ではなくツール自身が持つ。文書は整理されると場所が変わるが、
# --help と終了時のメッセージは、ツールを動かした人の目の前に必ず出る。
# ---------------------------------------------------------------------------

#: --help の末尾に出す説明。
_EXIT_HELP = """\
終了番号:
  0  計画書を書き換えた。変更点の一覧が空のときは、何も変えずに様式を確かめて
     終わる（これも0）。
  1  断った。計画書は一字も変わっていない。どの条件に当たったかと宛先の名前を
     標準出力に示すので、その文をそのまま designer へ返し、直した変更点の一覧で
     やり直す。計画書を手で書き換えて通してはならない。
  2  ツール自体が動かなかった（ファイルが読めない、JSON として壊れている、
     様式のファイルが読めない、書き込みに失敗した等）。原因を標準エラー出力に
     示すので、入力か環境を直してもう一度実行する。
"""


# ---------------------------------------------------------------------------
# 補助
# ---------------------------------------------------------------------------


def _force_utf8_streams() -> None:
    """標準出力・標準エラー出力を UTF-8 に切り替える。

    このツールが出す文字は、--help の説明も、断り文も、故障の原因も、すべて日本語で
    ある。Windows の端末（cp932）のままだと、日本語やダッシュ（—）を書いた瞬間に
    UnicodeEncodeError で落ちる。落ちれば終了番号は 1 でも 2 でもなくなり、
    「断られた／ツールが動かなかった」の区別が壊れ、断る理由も相手に届かない。
    それを防ぐため、何かを印字する前に必ず呼ぶ。argparse が --help を書き出すのも
    この切り替えのあとになるよう、main の先頭で呼ぶこと。

    tools/profile_data.py と同じやり方（``stream.reconfigure``）である。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            # 切り替えられない流れ（テストで包んだ流れなど）はそのまま使う。
            pass


def _exit2(message: str) -> None:
    """原因を標準エラー出力に書いて、終了番号2で終わる。"""
    print(message, file=sys.stderr)
    sys.exit(2)


def _quote(name: object) -> str:
    """名前をバッククォートで囲んで返す（メッセージ中の宛先の表示に使う）。"""
    return f"`{name}`"


def _join_names(names: list[str]) -> str:
    """名前の一覧を「a / b / c」の形にする。空のときはその旨を返す。"""
    if not names:
        return "（一つもありません）"
    return " / ".join(names)


def _format_jsonpath(absolute_path: deque) -> str:  # type: ignore[type-arg]
    """jsonschema の absolute_path を「a[0].b」の形の文字列にする。

    Args:
        absolute_path: ``jsonschema.ValidationError`` の ``absolute_path``。

    Returns:
        ドットと角括弧で書いた位置。最上位の誤りのときは空文字列。
    """
    tokens: list[str] = []
    for part in absolute_path:
        if isinstance(part, int):
            tokens.append(f"[{part}]")
        elif tokens:
            tokens.append(f".{part}")
        else:
            tokens.append(str(part))
    return "".join(tokens)


# ---------------------------------------------------------------------------
# スキーマの読み込み（validate_icd.py と同じレジストリの組み方）
# ---------------------------------------------------------------------------


def _build_registry() -> referencing.Registry:  # type: ignore[type-arg]
    """ICD の様式をすべて読み込み、referencing のレジストリを返す。

    様式はファイル名（例 ``analysis-brief.json``）で登録する。design-decision.json の
    ``"$ref": "analysis-brief.json#/$defs/AnalysisTask"`` のような、ファイルをまたぐ
    参照を解けるようにするためである。

    Returns:
        ICD の様式をすべて含む ``referencing.Registry``。

    Raises:
        SystemExit: 様式のファイルが読めない・壊れているとき（終了番号2）。
    """
    resources: list[tuple[str, referencing.Resource]] = []  # type: ignore[type-arg]
    for schema_file in sorted(_SCHEMA_DIR.glob("*.json")):
        try:
            with open(schema_file, encoding="utf-8") as fh:
                schema = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            _exit2(f"様式のファイル '{schema_file}' を読めませんでした: {exc}")
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
        _exit2(f"様式のファイルが '{_SCHEMA_DIR}' に一つもありません。")

    return referencing.Registry().with_resources(resources)  # type: ignore[arg-type]


def _load_schema(file_name: str) -> dict[str, Any]:
    """様式のファイルを一つ読んで、その中身を返す。

    Args:
        file_name: 様式のファイル名（例 ``"analysis-brief.json"``）。

    Returns:
        読み込んだ様式の中身。

    Raises:
        SystemExit: 読めない・壊れているとき（終了番号2）。
    """
    path = _SCHEMA_DIR / file_name
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _exit2(f"様式のファイル '{path}' を読めませんでした: {exc}")
    if not isinstance(loaded, dict):
        _exit2(f"様式のファイル '{path}' の中身がオブジェクトではありません。")
    return loaded  # type: ignore[return-value]


def _schema_errors(schema_ref: str, instance: object) -> list[str]:
    """様式に照らして、合わない箇所を「位置: 説明」の一覧で返す。

    Args:
        schema_ref: 様式への参照（例 ``"analysis-brief.json"``）。
        instance: 確かめる対象（読み込み済みの JSON）。

    Returns:
        合わない箇所の一覧。空のリストなら様式に合っている。

    Raises:
        SystemExit: 様式そのものが壊れているとき（終了番号2）。
    """
    registry = _build_registry()
    try:
        validator = Draft7Validator({"$ref": schema_ref}, registry=registry)
    except SchemaError as exc:
        _exit2(f"様式 '{schema_ref}' 自体が正しくありません: {exc.message}")

    messages: list[str] = []
    for error in sorted(
        validator.iter_errors(instance), key=lambda e: list(e.absolute_path)
    ):
        path = _format_jsonpath(error.absolute_path)
        if path:
            messages.append(f"{path}: {error.message}")
        else:
            messages.append(f"（最上位）: {error.message}")
    return messages


# ---------------------------------------------------------------------------
# 宛先の一覧
# ---------------------------------------------------------------------------


def brief_field_names() -> list[str]:
    """field_changes で指定できる、計画書の最上位の欄の名前を返す。

    Returns:
        analysis-brief.json の最上位プロパティのうち、``tasks`` を除いたもの。
    """
    schema = _load_schema("analysis-brief.json")
    properties = schema.get("properties", {})
    return [name for name in properties if name != _TASKS_FIELD]


def task_field_names() -> list[str]:
    """task_changes で指定できる、タスクの中の欄の名前を返す。

    Returns:
        analysis-brief.json の ``$defs/AnalysisTask`` のプロパティのうち、
        ``name`` を除いたもの。
    """
    schema = _load_schema("analysis-brief.json")
    task_schema = schema.get("$defs", {}).get("AnalysisTask", {})
    properties = task_schema.get("properties", {})
    return [name for name in properties if name != _TASK_NAME_FIELD]


def existing_task_names(brief: object) -> list[str]:
    """計画書にあるタスクの名前を、書かれている順に返す。

    Args:
        brief: 読み込み済みの計画書。

    Returns:
        タスク名の一覧。tasks が無い・配列でないときは空のリスト。
    """
    if not isinstance(brief, dict):
        return []
    tasks = brief.get(_TASKS_FIELD)
    if not isinstance(tasks, list):
        return []
    names: list[str] = []
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get(_TASK_NAME_FIELD), str):
            names.append(task[_TASK_NAME_FIELD])
    return names


# ---------------------------------------------------------------------------
# 断る条件の検査
# ---------------------------------------------------------------------------


def check_changes_schema(changes: object, changes_path: Path) -> list[str]:
    """条件1——変更点の一覧が BriefChanges の様式に合うかを確かめる。

    Args:
        changes: 読み込み済みの変更点の一覧。
        changes_path: その一覧のファイルの場所（メッセージに出す）。

    Returns:
        断る理由の一覧。空なら様式に合っている。
    """
    errors = _schema_errors(_CHANGES_SCHEMA_REF, changes)
    if not errors:
        return []
    reasons = [
        f"条件1: 変更点の一覧 '{changes_path}' が BriefChanges の様式に合いません。"
        "知らない操作の名前、reason の書き忘れ、値の型の誤りなどが考えられます。"
    ]
    reasons.extend(f"  - {message}" for message in errors)
    return reasons


def check_destinations(brief: object, changes: dict[str, Any]) -> list[str]:
    """条件2〜6——宛先と重複を確かめる。

    様式（条件1）を通った変更点の一覧だけを渡すこと。

    Args:
        brief: 読み込み済みの計画書。
        changes: 読み込み済みの変更点の一覧（BriefChanges の様式に合ったもの）。

    Returns:
        断る理由の一覧。空なら宛先はすべて正しい。
    """
    reasons: list[str] = []

    field_changes: list[dict[str, Any]] = changes.get("field_changes", [])
    task_changes: list[dict[str, Any]] = changes.get("task_changes", [])
    added_tasks: list[dict[str, Any]] = changes.get("added_tasks", [])
    deleted_tasks: list[dict[str, Any]] = changes.get("deleted_tasks", [])

    task_names = existing_task_names(brief)
    allowed_brief_fields = brief_field_names()
    allowed_task_fields = task_field_names()

    # --- 条件4・条件2・条件5: 計画書の欄の書き換え ------------------------
    seen_fields: set[str] = set()
    for change in field_changes:
        field = change.get("field")
        if field in _FORBIDDEN_BRIEF_FIELDS:
            reasons.append(
                f"条件4: {_quote(field)} は変更できません"
                "（課長が確認の場で決める欄です）。"
            )
            continue
        if field == _TASKS_FIELD:
            reasons.append(
                f"条件2: {_quote(_TASKS_FIELD)} は field_changes では変更できません。"
                "タスクへの変更は task_changes・added_tasks・deleted_tasks で行って"
                "ください。"
            )
            continue
        if field not in allowed_brief_fields:
            reasons.append(
                f"条件2: 欄 {_quote(field)} は計画書の様式にありません。"
                f"指定できるのは {_join_names(allowed_brief_fields)} です。"
            )
            continue
        if field in seen_fields:
            reasons.append(
                f"条件5: 欄 {_quote(field)} が field_changes に二つあります。"
                "一度の改訂で同じ欄を二度変えることはできません。"
                "一つにまとめてください。"
            )
            continue
        seen_fields.add(field)

    # --- 条件2・条件5: タスクの中の欄の書き換え ---------------------------
    seen_task_fields: set[tuple[str, str]] = set()
    for change in task_changes:
        task_name = change.get("task_name")
        field = change.get("field")
        bad = False
        if task_name not in task_names:
            reasons.append(
                f"条件2: タスク {_quote(task_name)} は計画書にありません。"
                f"計画書にあるのは {_join_names(task_names)} です。"
            )
            bad = True
        if field == _TASK_NAME_FIELD:
            reasons.append(
                f"条件2: タスクの欄 {_quote(_TASK_NAME_FIELD)} は変更できません。"
                "タスク名を変えるときは、そのタスクを削除して、新しい名前で"
                "追加してください。"
            )
            bad = True
        elif field not in allowed_task_fields:
            reasons.append(
                f"条件2: タスクの欄 {_quote(field)} はタスクの様式（AnalysisTask）に"
                f"ありません。指定できるのは {_join_names(allowed_task_fields)} です。"
            )
            bad = True
        if bad:
            continue
        key = (str(task_name), str(field))
        if key in seen_task_fields:
            reasons.append(
                f"条件5: タスク {_quote(task_name)} の欄 {_quote(field)} が"
                " task_changes に二つあります。一度の改訂で同じ欄を二度変えることは"
                "できません。一つにまとめてください。"
            )
            continue
        seen_task_fields.add(key)

    # --- 条件3: 追加するタスク名の重複 ------------------------------------
    added_names: list[str] = []
    for addition in added_tasks:
        task = addition.get("task", {})
        name = task.get(_TASK_NAME_FIELD) if isinstance(task, dict) else None
        if name in task_names:
            reasons.append(
                f"条件3: 追加するタスク名 {_quote(name)} は、すでに計画書にあります。"
                f"計画書にあるのは {_join_names(task_names)} です。"
                "既存のタスクを直すときは task_changes を使ってください。"
            )
            continue
        if name in added_names:
            reasons.append(
                f"条件3: 追加するタスク名 {_quote(name)} が added_tasks の中で"
                "重複しています。タスク名は一つずつ別のものにしてください。"
            )
            continue
        if isinstance(name, str):
            added_names.append(name)

    # --- 条件2・条件6: 削除するタスク --------------------------------------
    deleted_names: list[str] = []
    for deletion in deleted_tasks:
        name = deletion.get("task_name")
        if name not in task_names:
            reasons.append(
                f"条件2: タスク {_quote(name)} は計画書にありません。"
                f"計画書にあるのは {_join_names(task_names)} です。"
            )
            continue
        if isinstance(name, str):
            deleted_names.append(name)

    for name in deleted_names:
        if any(change.get("task_name") == name for change in task_changes):
            reasons.append(
                f"条件6: タスク {_quote(name)} は deleted_tasks にありながら"
                " task_changes にもあります。削除するタスクを書き換えることは"
                "できません。どちらか一方にしてください。"
            )
        for addition in added_tasks:
            task = addition.get("task", {})
            added_name = task.get(_TASK_NAME_FIELD) if isinstance(task, dict) else None
            if added_name == name:
                reasons.append(
                    f"条件6: タスク {_quote(name)} は deleted_tasks にありながら"
                    " added_tasks にもあります。同じ名前で入れ替えることはできません。"
                    "書き換えるなら task_changes を、名前を変えるなら別の名前で"
                    "追加してください。"
                )
                break

    return reasons


# ---------------------------------------------------------------------------
# 適用
# ---------------------------------------------------------------------------


def apply_changes(brief: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """変更点の一覧を計画書へ当てた結果を、新しいオブジェクトとして返す。

    当てる順番は、削除 → 計画書の欄の書き換え → タスクの中の欄の書き換え → 追加。
    元の ``brief`` は変更しない。

    Args:
        brief: 読み込み済みの計画書。
        changes: 読み込み済みの変更点の一覧（検査を通ったもの）。

    Returns:
        書き換えたあとの計画書。
    """
    applied = copy.deepcopy(brief)

    # 1. 削除
    deleted_names = {
        deletion.get("task_name") for deletion in changes.get("deleted_tasks", [])
    }
    if deleted_names and isinstance(applied.get(_TASKS_FIELD), list):
        applied[_TASKS_FIELD] = [
            task
            for task in applied[_TASKS_FIELD]
            if not (isinstance(task, dict) and task.get(_TASK_NAME_FIELD) in deleted_names)
        ]

    # 2. 計画書の欄の書き換え
    for change in changes.get("field_changes", []):
        applied[change["field"]] = copy.deepcopy(change["new_value"])

    # 3. タスクの中の欄の書き換え
    for change in changes.get("task_changes", []):
        tasks = applied.get(_TASKS_FIELD)
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if isinstance(task, dict) and task.get(_TASK_NAME_FIELD) == change["task_name"]:
                task[change["field"]] = copy.deepcopy(change["new_value"])

    # 4. 追加
    for addition in changes.get("added_tasks", []):
        tasks = applied.get(_TASKS_FIELD)
        if not isinstance(tasks, list):
            # tasks が配列でない計画書には追加できない。この場合は書き換えたあとの
            # 様式の検査（条件7）で断ることになる。
            continue
        tasks.append(copy.deepcopy(addition["task"]))

    return applied


def count_operations(changes: dict[str, Any]) -> dict[str, int]:
    """変更点の一覧に含まれる操作の件数を、種類ごとに数える。

    Args:
        changes: 読み込み済みの変更点の一覧。

    Returns:
        操作の名前をキー、件数を値とする辞書。
    """
    return {
        "field_changes": len(changes.get("field_changes", [])),
        "task_changes": len(changes.get("task_changes", [])),
        "added_tasks": len(changes.get("added_tasks", [])),
        "deleted_tasks": len(changes.get("deleted_tasks", [])),
    }


# ---------------------------------------------------------------------------
# 入出力
# ---------------------------------------------------------------------------


def _load_json_file(path: Path, label: str) -> Any:
    """JSON のファイルを読む。読めないときは終了番号2で終わる。

    Args:
        path: 読むファイルの場所。
        label: メッセージに出す呼び名（例 "計画書"）。

    Returns:
        読み込んだ中身。

    Raises:
        SystemExit: ファイルが無い・読めない・JSON として壊れているとき。
    """
    if not path.exists():
        _exit2(f"{label}のファイルが見つかりません: '{path}'")
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        _exit2(f"{label}のファイル '{path}' を読めませんでした: {exc}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _exit2(f"{label}のファイル '{path}' は JSON として壊れています: {exc}")


def _write_temp(applied: dict[str, Any], brief_path: Path) -> Path:
    """書き換えたあとの計画書を、同じフォルダの一時ファイルへ書く。

    Args:
        applied: 書き換えたあとの計画書。
        brief_path: 本体のファイルの場所。

    Returns:
        書いた一時ファイルの場所。

    Raises:
        SystemExit: 書き込みに失敗したとき（終了番号2）。
    """
    directory = brief_path.parent
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(directory), prefix=brief_path.name + ".", suffix=".tmp"
        )
    except OSError as exc:
        _exit2(f"一時ファイルを '{directory}' に作れませんでした: {exc}")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(applied, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except OSError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        _exit2(f"一時ファイル '{tmp_path}' へ書き込めませんでした: {exc}")
    return tmp_path


def _reject(reasons: list[str], brief_path: Path) -> int:
    """断る理由を標準出力に示して、終了番号1を返す。"""
    for reason in reasons:
        print(reason)
    print(
        f"NG: 上の理由により、書き換えを断りました。"
        f"計画書 '{brief_path}' は一字も変えていません。"
        "上の文をそのまま designer へ返し、直した変更点の一覧でやり直してください。"
    )
    return 1


# ---------------------------------------------------------------------------
# 引数
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apply_brief_changes.py",
        description=(
            "変更点の一覧（design-decision.json の $defs/BriefChanges）のとおりに、"
            "分析計画書（analysis-brief.json 様式）を書き換えて同じ場所に保存し直す。"
            "\n宛先が実在しない、触れてはいけない欄を変えようとしている、同じ宛先を"
            "二度変えている——といったときは、計画書を一字も変えずに断る。"
        ),
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--brief",
        required=True,
        metavar="PATH",
        help="書き換える計画書（analysis-brief.json 様式）のパス。",
    )
    parser.add_argument(
        "--changes",
        required=True,
        metavar="PATH",
        help=(
            "変更点の一覧のパス。design-decision.json の $defs/BriefChanges の形の"
            "オブジェクト（DesignDecision 全体ではなく、その changes だけ）。"
        ),
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """apply_brief_changes の入口。

    Args:
        argv: コマンドラインの引数（省略時は ``sys.argv``）。

    Returns:
        0＝書き換えた／1＝断った／2＝ツール自体が動かなかった。
    """
    # 何かを印字する前に流れを UTF-8 にする。argparse が --help を書き出すのも
    # このあと（_parse_args の中）になる。
    _force_utf8_streams()

    args = _parse_args(argv)

    brief_path = Path(args.brief)
    changes_path = Path(args.changes)

    brief = _load_json_file(brief_path, "計画書")
    changes = _load_json_file(changes_path, "変更点の一覧")

    # --- 条件1: 変更点の一覧の様式 ----------------------------------------
    reasons = check_changes_schema(changes, changes_path)
    if reasons:
        return _reject(reasons, brief_path)

    if not isinstance(brief, dict):
        return _reject(
            [
                f"条件7: 計画書 '{brief_path}' の中身がオブジェクトではありません。"
                "analysis-brief の様式に合う計画書を渡してください。"
            ],
            brief_path,
        )

    # --- 条件2〜6: 宛先と重複 ----------------------------------------------
    reasons = check_destinations(brief, changes)
    if reasons:
        return _reject(reasons, brief_path)

    # --- 適用 ---------------------------------------------------------------
    applied = apply_changes(brief, changes)
    counts = count_operations(changes)
    total = sum(counts.values())

    if total == 0:
        # 変更が空。何も書かず、様式だけ確かめる。
        errors = _schema_errors(_BRIEF_SCHEMA_REF, applied)
        if errors:
            return _reject(_condition7_reasons(errors, brief_path), brief_path)
        print(
            f"OK: 変更点はありません。計画書 '{brief_path}' は変更していません"
            "（様式は確認済みです）。"
        )
        return 0

    # --- 保存（一時ファイル → 条件7の検査 → 置き換え） ----------------------
    tmp_path = _write_temp(applied, brief_path)

    errors = _schema_errors(_BRIEF_SCHEMA_REF, applied)
    if errors:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return _reject(_condition7_reasons(errors, brief_path), brief_path)

    try:
        os.replace(str(tmp_path), str(brief_path))
    except OSError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        _exit2(f"計画書 '{brief_path}' を置き換えられませんでした: {exc}")

    print(
        f"OK: 計画書 '{brief_path}' を書き換えました"
        f"（計画書の欄 {counts['field_changes']} 件 /"
        f" タスクの欄 {counts['task_changes']} 件 /"
        f" タスクの追加 {counts['added_tasks']} 件 /"
        f" タスクの削除 {counts['deleted_tasks']} 件）。"
    )
    return 0


def _condition7_reasons(errors: list[str], brief_path: Path) -> list[str]:
    """条件7の断り文を組み立てる。"""
    reasons = [
        f"条件7: 変更点を当てたあとの計画書が analysis-brief の様式に合いません"
        f"（対象: '{brief_path}'）。次の箇所が様式に反します。"
    ]
    reasons.extend(f"  - {message}" for message in errors)
    return reasons


if __name__ == "__main__":
    sys.exit(main())
