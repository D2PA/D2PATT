"""
export_report.py -- 報告書（Markdown）を PDF と Word（.docx）に書き出す。

課長が報告書を最終承認したあと、配布用の PDF と、課長が自分で手直しする用の
Word を作る。見た目は「Markdown を GitHub 風にレンダリングしたもの」。ネットワークには
出ない。報告書中の生の HTML は解釈せず、文字としてそのまま出す。

画像は、報告書と同じフォルダ配下にある実在の PNG / JPEG だけを使う。相対パスでも
絶対パスでもよい（分析結果の様式は図を絶対パスで渡すため）が、シンボリックリンクも
たどって解決した実体が報告書のフォルダの中になければならない。URL・フォルダの外を
指すパス（.. やリンクで外へ出るものを含む）・存在しないファイルが一つでもあれば、
何も書かずに止まる。

出力先が既にあるときは、--force が無ければ書かない（課長が Word を手で直したあとに
上書きして、手直しを消さないため）。書き込みは同じフォルダの一時ファイルに書き、
読み戻して確かめてから置き換える。--pdf と --docx を両方指定したときは、両方が
揃ってはじめて置き換える（片方だけが残ることはない）。

Usage:
    bash "$LAYER_DIR/tools/run" export_report.py --report <md> [--pdf <path>]
                                                  [--docx <path>] [--force]

終了番号の意味は --help の末尾に書いてある。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

import markdown

# report_pdf.py / report_docx.py are sibling script-style modules, not package
# imports: at runtime this always works because the wrapper (tools/run) execs
# this file by its own absolute path, so Python puts this directory at
# sys.path[0]. mypy instead resolves imports by package structure
# (src/tools/__init__.py makes this a package), so it cannot find a bare
# "report_pdf" -- hence the ignore, not a real missing dependency.
import report_docx  # type: ignore[import-not-found]
import report_pdf  # type: ignore[import-not-found]

#: cp932端末で日本語を殺さず、エンコードできない記号だけを '?' に置換する。
_STDIO_ERROR_HANDLER = "replace"

#: 報告書中で使える画像の形式（PDF・Word の両方が扱えるもの）。
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})

#: PDF の末尾付近で %%EOF を探す範囲（バイト）。
_PDF_TAIL_BYTES = 1024
_PDF_PAGE = re.compile(rb"/Type\s*/Page(?![a-zA-Z])")

#: 段落を割り込んで始まる箇条書きの行（GitHub と同じく「- * +」と「1.」だけ）。
_LIST_START = re.compile(r"^(?:[-*+]|1[.)])[ \t]+\S")
#: 箇条書きの項目の行（入れ子を含む）。
_LIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)])[ \t]+")
#: 箇条書きの項目の行を、字下げ（空白のみ）・記号以降に分ける。
_LIST_ITEM = re.compile(r"^( *)((?:[-*+]|\d+[.)])[ \t]+.*)$")
#: Python-Markdown が入れ子一段に求める字下げの幅。
_NEST_WIDTH = 4
#: コードブロックの囲みの行。
_FENCE = re.compile(r"^\s{0,3}(?:```|~~~)")
#: Windows のドライブ付きパス（C:/... や C:\...）。
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

#: XML が知っている 5 つ以外の名前付き文字参照（&copy; など）。文字として残す。
_NAMED_ENTITY = re.compile(r"&(?!(?:amp|lt|gt|quot|apos);)([A-Za-z][A-Za-z0-9]*;)")

_EXIT_EXISTS = 3

#: 想定外の例外の理由を stderr に出すときの最大の文字数（一行に収める）。
_MAX_REASON_CHARS = 200

_EXIT_HELP = """\
Exit codes:
  0  書き出した。書いたファイルの絶対パスを 1 行ずつ標準出力に出す。PDF のフォントに
     字形の無い文字があったときは、stderr に「警告:」で列挙する（その文字は PDF では
     正しく表示されない。報告書の文字を置き換えるかどうかを決める）。
  1  書き出せなかった（何も書いていない）。図のファイルが無い・報告書の外を指している、
     フォントが見つからない、報告書を解釈・組み立てできない、書き込めない、など。
     stderr の ERROR 行の理由と対処に従って直し、再実行する。
  2  使い方の誤り（報告書が無い・--pdf も --docx も無い・拡張子が違う・出力先の
     フォルダが無い）。stderr の ERROR 行の指示どおりに引数を直して再実行する。
  3  出力先に既にファイルがある（課長が Word を手直ししている可能性がある）。何も
     書いていない。上書きしてよければ --force を付けて再実行し、残すなら別の名前の
     出力先を指定する。
"""


class _UsageError(Exception):
    """使い方の誤り（exit 2 に落とし込む内部例外）。"""


class _ExportError(Exception):
    """書き出せなかったこと（exit 1 に落とし込む内部例外）。"""


class _Output(NamedTuple):
    """書き出す先一件。kind は "PDF" か "Word"。"""

    kind: str
    path: Path


class _NoRawHtml(markdown.extensions.Extension):
    """報告書中の生の HTML を解釈せず、文字として残す拡張。"""

    def extendMarkdown(self, md: markdown.Markdown) -> None:
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")


def _make_stdio_safe() -> None:
    """stdout/stderrを、エンコードできない文字で落ちない状態にする。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - stream already replaced
            continue
        try:
            reconfigure(errors=_STDIO_ERROR_HANDLER)
        except (ValueError, OSError):  # pragma: no cover - detached stream
            continue


def separate_lists(text: str) -> str:
    """段落の直後に空行なしで始まる箇条書きの前に、空行を挟む。

    GitHub は段落の直後の「- 」行を箇条書きとして描くが、Python-Markdown は空行を
    要求して段落に吸収してしまう。見た目を GitHub にそろえるための前処理。
    コードブロック（``` / ~~~）の中は触らない。
    """
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
        previous = out[-1] if out else ""
        is_para_line = previous.strip() and not previous[:1].isspace()
        if (
            not in_fence
            and _LIST_START.match(line)
            and is_para_line
            and not _LIST_LINE.match(previous)
        ):
            out.append("")
        out.append(line)
    return "\n".join(out)


def _nest_depth(stack: list[int], indent: int) -> int:
    """祖先の項目の字下げ *stack* を更新し、字下げ *indent* の項目の段数を返す。"""
    while stack and stack[-1] >= indent:
        stack.pop()
    stack.append(indent)
    return len(stack) - 1


def normalize_list_indent(text: str) -> str:
    """箇条書きの入れ子の字下げを、段数に応じて 4 字単位に揃える。

    GitHub は 2 字や 3 字の字下げでも入れ子として描くが、Python-Markdown は 4 字を
    求める。見た目を GitHub にそろえるための前処理。4 字単位で一貫して書かれた入れ子は
    変わらない。箇条書きの外で 4 字以上下げた行（字下げのコードブロック）と、
    コードブロック（``` / ~~~）の中は触らない。
    """
    out: list[str] = []
    stack: list[int] = []  # いま開いている項目の、元の字下げ
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        item = None if in_fence else _LIST_ITEM.match(line)
        if item is None or (not stack and len(item.group(1)) >= _NEST_WIDTH):
            if not in_fence and line.strip() and not line[:1].isspace():
                stack = []  # 字下げの無い本文の行で箇条書きは終わる
            out.append(line)
            continue
        depth = _nest_depth(stack, len(item.group(1)))
        out.append(" " * (_NEST_WIDTH * depth) + item.group(2))
    return "\n".join(out)


def parse_markdown(text: str) -> ET.Element:
    """Markdown の文字列を、ブロック要素を子に持つ一つの要素の木にする。"""
    html = markdown.markdown(
        separate_lists(normalize_list_indent(text)),
        extensions=["tables", "fenced_code", "sane_lists", _NoRawHtml()],
    )
    safe = _NAMED_ENTITY.sub(r"&amp;\1", html)
    # ここで解析するのは、生の HTML を無効にした markdown の出力だけである。報告書中の
    # "<" と ">" は markdown が &lt; / &gt; にエスケープしてから渡すので、DOCTYPE や
    # ENTITY の宣言は入り得ず、外部実体や実体の膨張（XXE・billion laughs）は起きない。
    # そのため defusedxml は使わない（依存を増やさない）。ruff の S314 はこのリポジトリ
    # の設定では有効でなく、noqa を付けると RUF100 になるので付けない。この前提は
    # test_doctype_and_entity_declarations_stay_as_text が見張る。
    return ET.fromstring(f"<div>{safe}</div>")


def first_heading(root: ET.Element, fallback: str) -> str:
    """最初の h1 の文字を返す。無ければ *fallback*。"""
    heading = root.find(".//h1")
    text = "".join(heading.itertext()).strip() if heading is not None else ""
    return text or fallback


def _image_target(base: Path, decoded: str) -> Path:
    """画像のパスを、*base* から見た実体の絶対パスに解決する（リンクもたどる）。

    右辺が絶対パスなら pathlib の結合はその絶対パスそのものになるので、相対パスと
    絶対パスを同じ式で扱える（Windows のドライブ付きパスも Windows ではそうなる）。
    """
    return (base / decoded).resolve()


def image_problem(base: Path, src: str) -> str | None:
    """画像の src が使えない理由を返す。使えるなら None。*base* は解決済みのフォルダ。"""
    if not src:
        return "画像のパスが空です"
    if urlsplit(src).scheme and not _DRIVE.match(src):
        return "URL は使えません（ネットワークから取りに行かない）"
    decoded = unquote(src)
    if _DRIVE.match(decoded) and not Path(decoded).is_absolute():
        return (
            "Windows のドライブ付きパスは、この OS では確かめられません"
            "（報告書のフォルダからの相対パスにする）"
        )
    target = _image_target(base, decoded)
    if not target.is_relative_to(base):
        return "報告書のフォルダの外を指しています"
    if not target.is_file():
        return "ファイルがありません"
    if target.suffix.lower() not in _IMAGE_SUFFIXES:
        return "対応していない形式です（PNG か JPEG にする）"
    return None


def resolve_images(root: ET.Element, base: Path) -> dict[str, Path]:
    """木の中の画像をすべて確かめ、src から実在ファイルへの対応を返す。

    使えない画像が一つでもあれば、全部を列挙した _ExportError を送出する。
    """
    base = base.resolve()
    images: dict[str, Path] = {}
    problems: list[str] = []
    for img in root.iter("img"):
        src = img.get("src", "")
        reason = image_problem(base, src)
        if reason is None:
            images[src] = _image_target(base, unquote(src))
        else:
            problems.append(f"  {src or '（空）'}: {reason}")
    if problems:
        raise _ExportError(
            f"使えない画像が {len(problems)} 件あります。\n"
            + "\n".join(problems)
            + "\n図は報告書と同じフォルダ配下にある PNG / JPEG に限ります（相対パスでも"
            "絶対パスでもよい）。報告書の該当行を直すか、図のファイルをそのフォルダ配下に"
            "置いてから再実行してください。何も書き出していません。"
        )
    return images


def check_pdf(path: Path) -> None:
    """書いた PDF を読み戻して、形が整っているかを確かめる。"""
    data = path.read_bytes()
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-_PDF_TAIL_BYTES:]:
        raise _ExportError("書いた PDF の先頭か末尾が壊れています。")
    if not _PDF_PAGE.search(data):
        raise _ExportError("書いた PDF にページが 1 枚もありません。")


def check_docx(path: Path) -> None:
    """書いた Word を読み戻して、開けるか・本文があるかを確かめる。"""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile as exc:
        raise _ExportError("書いた Word ファイルを ZIP として開けません。") from exc
    if "word/document.xml" not in names:
        raise _ExportError(
            "書いた Word ファイルに本文（word/document.xml）がありません。"
        )


def _outputs(args: argparse.Namespace) -> list[_Output]:
    """引数から書き出す先を集め、拡張子と親フォルダを確かめる。"""
    wanted = [("PDF", args.pdf, ".pdf"), ("Word", args.docx, ".docx")]
    outputs = []
    for kind, raw, suffix in wanted:
        if raw is None:
            continue
        path = Path(raw).resolve()
        if path.suffix.lower() != suffix:
            raise _UsageError(
                f"{kind} の出力先の拡張子は {suffix} にしてください（受け取った値: {raw}）。"
            )
        if not path.parent.is_dir():
            raise _UsageError(
                f"{kind} の出力先のフォルダがありません: {path.parent} -- 実在するフォルダを指定してください。"
            )
        if path.is_dir():
            raise _UsageError(
                f"{kind} の出力先がフォルダです: {raw} -- ファイル名まで指定してください。"
            )
        outputs.append(_Output(kind, path))
    if not outputs:
        raise _UsageError("--pdf か --docx の少なくとも一方を指定してください。")
    return outputs


def _validate(args: argparse.Namespace) -> tuple[Path, list[_Output]]:
    """報告書と出力先を確かめる。誤りは _UsageError。"""
    report = Path(args.report)
    if not report.is_file():
        raise _UsageError(
            f"報告書が見つかりません: {args.report} -- 実在する Markdown ファイルを指定してください。"
        )
    return report.resolve(), _outputs(args)


def _existing(outputs: list[_Output]) -> list[_Output]:
    """既にある出力先を返す。"""
    return [output for output in outputs if output.path.exists()]


def _render(
    report: Path, outputs: list[_Output]
) -> tuple[dict[Path, bytes], list[str]]:
    """報告書を読み、各出力のバイト列と、PDF で字形の無い文字の一覧を返す。"""
    try:
        text = report.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _ExportError(
            "報告書を UTF-8 として読めません。UTF-8 で保存し直してください。"
        ) from exc
    root = parse_markdown(text)
    images = resolve_images(root, report.parent)
    title = first_heading(root, report.stem)
    rendered: dict[Path, bytes] = {}
    missing: list[str] = []
    for output in outputs:
        if output.kind == "PDF":
            rendered[output.path] = report_pdf.build_pdf(root, images, title)
            missing = report_pdf.missing_glyphs("".join(root.itertext()))
        else:
            rendered[output.path] = report_docx.build_docx(root, images, title)
    return rendered, missing


def _write_temp(path: Path, data: bytes) -> Path:
    """*path* と同じフォルダの一時ファイルに書き、その場所を返す。

    書き込みに失敗したら、作りかけの一時ファイルを消してから例外を送り直す。
    """
    handle, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temp = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def _backup(target: Path) -> Path | None:
    """既にある *target* を同じフォルダの .bak に退避し、その場所を返す。無ければ None。"""
    if not target.exists():
        return None
    handle, name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".bak"
    )
    os.close(handle)  # Windows では開いたままだと、次の置き換えが「使用中」で失敗する
    backup = Path(name)
    try:
        os.replace(target, backup)
    except OSError:
        backup.unlink(missing_ok=True)
        raise
    return backup


def _rollback(done: list[tuple[Path, Path | None]]) -> list[str]:
    """置き換え済みの出力を元に戻す。戻せなかったものの説明を返す。

    一件戻せなくても、残りの巻き戻しは続ける。
    """
    left: list[str] = []
    for target, backup in reversed(done):
        try:
            if backup is not None:
                os.replace(backup, target)
            else:
                target.unlink(missing_ok=True)
        except OSError:
            if backup is not None:
                left.append(
                    f"  元のファイルは {backup} に残っています（{target} に戻す）"
                )
            else:
                left.append(f"  作りかけの {target} を消せませんでした（手で消す）")
    return left


def _write_failure(exc: OSError, left: list[str]) -> str:
    """書き込みの失敗を、理由と対処の文にする。戻せなかったものがあれば添える。"""
    message = (
        f"ファイルを書き込めません（{exc.strerror or exc}: {exc.filename or ''}）。"
        "出力先を Word などで開いていれば閉じ、フォルダの書き込み権限を確かめてください。"
    )
    if not left:
        return message
    return (
        message
        + "\n元に戻しきれなかったファイルがあります。次の案内どおりに手で戻してください。\n"
        + "\n".join(left)
    )


def _commit(temps: dict[Path, Path]) -> None:
    """一時ファイルを出力先に置き換える。途中で失敗したら元の状態に戻す。"""
    done: list[tuple[Path, Path | None]] = []
    try:
        for target, temp in temps.items():
            done.append((target, _backup(target)))
            os.replace(temp, target)
    except OSError as exc:
        raise _ExportError(_write_failure(exc, _rollback(done))) from exc
    for _, backup in done:
        if backup is not None:
            with contextlib.suppress(OSError):
                backup.unlink(missing_ok=True)


def _write_all(rendered: dict[Path, bytes]) -> None:
    """すべての出力を一時ファイルに書いて確かめ、揃ってから置き換える。"""
    temps: dict[Path, Path] = {}
    try:
        for target, data in rendered.items():
            temps[target] = _write_temp(target, data)
            (check_pdf if target.suffix.lower() == ".pdf" else check_docx)(
                temps[target]
            )
        _commit(temps)
    except OSError as exc:
        raise _ExportError(_write_failure(exc, [])) from exc
    finally:
        for temp in temps.values():
            temp.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    """コマンドライン引数の定義を返す。"""
    parser = argparse.ArgumentParser(
        prog="export_report.py",
        description="報告書（Markdown）を PDF と Word（.docx）に書き出す。ネットワークには出ない。",
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--report", required=True, metavar="MD", help="書き出す報告書（Markdown）。"
    )
    parser.add_argument("--pdf", metavar="PATH", help="PDF の出力先（拡張子 .pdf）。")
    parser.add_argument(
        "--docx", metavar="PATH", help="Word の出力先（拡張子 .docx）。"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="出力先が既にあっても置き換える。課長の手直しを消してよいと確かめてから付ける。",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    """検証・組み立て・書き込みを順に行い、終了番号を返す。"""
    report, outputs = _validate(args)
    existing = _existing(outputs)
    if existing and not args.force:
        listed = "\n".join(f"  {output.path}" for output in existing)
        print(
            f"ERROR: 出力先に既にファイルがあります（課長が Word を手直ししている可能性があります）。\n{listed}\n"
            "上書きしてよければ --force を付けて再実行し、残したければ別の名前の出力先を指定してください。"
            "何も書き出していません。",
            file=sys.stderr,
        )
        return _EXIT_EXISTS
    rendered, missing = _render(report, outputs)
    _write_all(rendered)
    if missing:
        chars = "、".join(f"'{char}'(U+{ord(char):04X})" for char in missing)
        print(
            f"警告: PDF のフォントに字形の無い文字があります（PDF では正しく表示されません）: {chars}",
            file=sys.stderr,
        )
    for output in outputs:
        print(output.path)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLIの入口。終了番号（0=書き出した / 1=書き出せない / 2=使い方の誤り / 3=既存あり）を返す。"""
    _make_stdio_safe()
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except _UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except _ExportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except report_pdf.FontMissingError as exc:
        print(
            f"ERROR: PDF 用の日本語フォントが見つかりません（{exc}）。"
            "japanize-matplotlib が入っているか確かめ、uv sync を実行してから再実行してください。",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - トレースバックを利用者に見せないため
        reason = str(exc).splitlines()[0][:_MAX_REASON_CHARS] if str(exc) else ""
        print(
            f"ERROR: 報告書を書き出せませんでした（{type(exc).__name__}: {reason}）。"
            "報告書の書き方（表の形・画像など）を見直し、直らなければ開発側に報告してください。"
            "何も書き出していません。",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
