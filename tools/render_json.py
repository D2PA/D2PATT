"""
render_json.py -- JSONを人が読める字下げアウトラインに整形して表示するビューア。

このツールはドメイン知識を一切持たない。「このファイルが何か」「どの順に読むか」
という意味づけは持たず、JSONの構造を字下げアウトラインに変換することだけを行う
（意味づけは .claude/skills/json-viewer/SKILL.md が持つ）。

Usage:
    python tools/render_json.py <path> [<path> ...] [--depth N] [--all]

位置引数にはファイルでもディレクトリでも渡せる（ディレクトリは再帰的に走査して
*.json を集める）。複数ファイルは更新時刻（mtime）の昇順、同時刻ならパスの昇順で
並べるので、案件フォルダを渡せば作られた順＝走行の順に上から読める。保存はシェルの
リダイレクトに任せる（--out は無い）。終了番号の意味は --help の末尾に書いてある。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import NamedTuple

#: 字下げ1段分。
_INDENT = "  "

#: スカラーだけの配列を1行に収めてよい最大の桁数（キーを含む行の長さで測る）。
_INLINE_WIDTH = 80

#: cp932端末で日本語を殺さず、エンコードできない記号だけを '?' に置換する。
_STDIO_ERROR_HANDLER = "replace"

_OMITTED = "…（省略：深さ {depth}）"
_UNREADABLE = "!! 読めません: {reason}"

_EXIT_HELP = """\
Exit codes:
  0  すべてのファイルを整形して標準出力に書いた。
  1  読めなかったファイルがある。そのファイルの見出しの下に "!! 読めません: <理由>"
     と出し、残りのファイルはそのまま整形している。理由を読んでファイルを直すか、
     そのファイルを対象から外して再実行する。
  2  使い方の誤り（パスが存在しない・ディレクトリに *.json が1件も無い・--depth が
     1未満など）。stderr の ERROR 行の指示どおりに引数を直して再実行する。

保存はシェルのリダイレクトで行う: python tools/render_json.py <path> > out.txt
"""


class _UsageError(Exception):
    """使い方の誤り（exit 2 に落とし込む内部例外）。"""


class _UnreadableError(Exception):
    """1ファイルが読めなかったこと（exit 1 に落とし込む内部例外）。"""


class _Target(NamedTuple):
    """整形する対象1件。path=実体、display=見出しに出す相対パス、mtime=更新時刻。"""

    path: Path
    display: str
    mtime: float


def _make_stdio_safe() -> None:
    """stdout/stderrを、エンコードできない文字で落ちない状態にする。

    エラーハンドラだけを変え、端末自身のエンコーディングは変えない。日本語は
    そのまま届き、端末のコードページに無い記号だけが '?' に化ける。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - stream already replaced
            continue
        try:
            reconfigure(errors=_STDIO_ERROR_HANDLER)
        except (ValueError, OSError):  # pragma: no cover - detached stream
            continue


def _format_scalar(value: object) -> str:
    """スカラー（文字列・数値・真偽値・null）を表示用の文字列にする。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _join(pad: str, label: str, text: str) -> str:
    """字下げ・ラベル・値を1行に組み立てる（ラベルが空ならラベル無しで返す）。"""
    return f"{pad}{label} {text}" if label else f"{pad}{text}"


def _scalar_lines(pad: str, label: str, value: object) -> list[str]:
    """スカラー1件の行を返す。改行を含む文字列は2行目以降も同じ深さに置く。"""
    parts = _format_scalar(value).split("\n")
    return [_join(pad, label, parts[0])] + [pad + part for part in parts[1:]]


def _is_scalar_list(value: object) -> bool:
    """値が「スカラーだけの配列」かどうかを返す（空配列は呼び出し前に処理済み）。"""
    return isinstance(value, list) and not any(
        isinstance(item, (dict, list)) for item in value
    )


def _inline_scalar_list(pad: str, label: str, values: list) -> str | None:
    """スカラーだけの配列を1行に収められるならその1行を、無理なら None を返す。

    どの要素も改行を含まず、キーを含む行が _INLINE_WIDTH 桁以内に収まるときだけ
    1行にする。1行に収まる配列は「入れ子」ではなく1行の値なので、呼び出し側は
    これを深さの数えにも打ち切りにも入れない。
    """
    texts = [_format_scalar(item) for item in values]
    if any("\n" in text for text in texts):
        return None
    line = _join(pad, label, ", ".join(texts))
    return line if len(line) <= _INLINE_WIDTH else None


def _dashed_scalar_list_lines(
    pad: str, label: str, inner: str, values: list
) -> list[str]:
    """1行に収まらないスカラー配列を、"- 値" で縦に並べた行にする。"""
    lines = [pad + label] if label else []
    for item in values:
        parts = _format_scalar(item).split("\n")
        lines.append(f"{inner}- {parts[0]}")
        lines.extend(inner + part for part in parts[1:])
    return lines


def _render_children(
    value: dict | list, depth: int, max_depth: int | None
) -> list[str]:
    """dict/list の各要素を、1段深い深さで整形して連結する。"""
    if isinstance(value, dict):
        entries = [(f"{key}:", item) for key, item in value.items()]
    else:
        entries = [(f"{index}.", item) for index, item in enumerate(value, 1)]
    lines: list[str] = []
    for label, item in entries:
        lines.extend(_render_value(item, label, depth + 1, max_depth))
    return lines


def _render_value(
    value: object, label: str, depth: int, max_depth: int | None
) -> list[str]:
    """深さ *depth* にある値 *value* を行の並びに整形する。

    label は "キー:" または "1." の形。ルート値では空文字を渡し、そのときは
    見出し行を作らずに中身だけを返す。max_depth が None でなければ、それより
    深い入れ子は畳んで省略の注記を出す。ただし1行に収まるスカラー配列は入れ子と
    して数えず、打ち切りの深さでもそのまま1行で出す（畳むと行数が増えるうえに
    中身が消えて、俯瞰の役に立たないため）。
    """
    pad = _INDENT * (depth - 2)  # 見出し行の字下げ（ルートでは空）
    inner = _INDENT * (depth - 1)  # 中身の字下げ
    if not isinstance(value, (dict, list)):
        return _scalar_lines(pad, label, value)
    if not value:
        return [_join(pad, label, "[]" if isinstance(value, list) else "{}")]
    scalar_list = _is_scalar_list(value)
    if scalar_list:
        inline = _inline_scalar_list(pad, label, value)
        if inline is not None:
            return [inline]
    head = [pad + label] if label else []
    if max_depth is not None and depth > max_depth:
        return head + [inner + _OMITTED.format(depth=max_depth)]
    if scalar_list:
        return _dashed_scalar_list_lines(pad, label, inner, value)
    return head + _render_children(value, depth, max_depth)


def render_json(document: object, max_depth: int | None = None) -> list[str]:
    """JSONから読み込んだ値を、人が読める字下げアウトラインの行の並びにする。"""
    return _render_value(document, "", 1, max_depth)


def _mtime(path: Path) -> float:
    """更新時刻を返す。取れないときは 0.0（並べ替えを落とさないため）。"""
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - 直後の読み込みで exit 1 になる
        return 0.0


def _scan_directory(root: Path, include_all: bool) -> list[_Target]:
    """*root* 配下を再帰的に走査し、*.json の対象一覧を返す。"""
    found: list[_Target] = []
    for path in root.rglob("*.json"):
        relative = path.relative_to(root)
        if not include_all and any(part.startswith(".") for part in relative.parts):
            continue
        found.append(_Target(path, relative.as_posix(), _mtime(path)))
    return found


def _collect_targets(paths: list[str], include_all: bool) -> list[_Target]:
    """位置引数を対象一覧に展開し、mtimeの昇順（同着はパス順）に並べて返す。"""
    targets: list[_Target] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise _UsageError(
                f"パスが見つかりません: {raw} -- "
                "実在するファイルかディレクトリを、絶対パスで指定し直してください。"
            )
        if not path.is_dir():
            targets.append(_Target(path, path.name, _mtime(path)))
            continue
        found = _scan_directory(path, include_all)
        if not found:
            raise _UsageError(
                f"*.json が1件も見つかりません: {raw} -- 別のディレクトリを指定するか、"
                "ドット始まりのファイルしか無い場合は --all を付けて再実行してください。"
            )
        targets.extend(found)
    return sorted(targets, key=lambda target: (target.mtime, str(target.path)))


def _load(path: Path) -> object:
    """JSONを1件読む。読めないときは _UnreadableError を送出する。"""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _UnreadableError(
            "UTF-8として読めません。ファイルの文字コードをUTF-8に直してください。"
        ) from exc
    except OSError as exc:
        raise _UnreadableError(
            f"ファイルを開けません（{exc.strerror or exc}）。"
            "パスと読み取り権限を確かめてください。"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _UnreadableError(
            f"JSONとして解析できません（{exc.lineno}行{exc.colno}列: {exc.msg}）。"
            "ファイルの中身が正しいJSONか確かめてください。"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    """コマンドライン引数の定義を返す。"""
    parser = argparse.ArgumentParser(
        prog="render_json.py",
        description=(
            "JSONを人が読める字下げアウトラインに整形して標準出力に書く。ファイルでも"
            "ディレクトリでも渡せる（ディレクトリは再帰的に走査し、*.json を更新時刻の"
            "昇順で出す）。"
        ),
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="整形するJSONファイル、または走査するディレクトリ（1つ以上）。",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=None,
        metavar="N",
        help=(
            "N段より深い入れ子を畳む（N>=1）。省略時は無制限。大きなJSONの俯瞰用。"
            "1行に収まるスカラー配列は入れ子として数えず、そのまま1行で出す。"
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "ディレクトリ走査でドット始まりのファイル・フォルダも対象にする。位置引数で"
            "直接指定したファイルは、この指定に関わらず常に対象。"
        ),
    )
    return parser


def _print_document(target: _Target, max_depth: int | None) -> bool:
    """対象1件の見出しと本文を書く。読めなければ注記を書いて False を返す。"""
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(target.mtime))
    print(f"## {target.display}  ({stamp})")
    print()
    readable = True
    try:
        document = _load(target.path)
    except _UnreadableError as exc:
        print(_UNREADABLE.format(reason=exc))
        readable = False
    else:
        for line in render_json(document, max_depth):
            print(line)
    print()
    return readable


def main(argv: list[str] | None = None) -> int:
    """CLIの入口。終了番号（0=成功 / 1=読めないファイルあり / 2=使い方の誤り）を返す。"""
    _make_stdio_safe()
    args = _build_parser().parse_args(argv)
    try:
        if args.depth is not None and args.depth < 1:
            raise _UsageError(
                f"--depth には1以上の整数を指定してください（受け取った値: {args.depth}）。"
                "入れ子を畳まずに全部出すときは --depth を省いてください。"
            )
        targets = _collect_targets(args.paths, args.all)
    except _UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    unreadable = 0
    for target in targets:
        if not _print_document(target, args.depth):
            unreadable += 1
    if unreadable:
        print(
            f"ERROR: 読めなかったファイルが {unreadable} 件あります"
            "（本文の '!! 読めません' の行に理由を書きました）。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
