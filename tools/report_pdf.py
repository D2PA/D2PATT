"""
report_pdf.py -- 報告書の木（Markdown から作った要素の木）を PDF に組み立てる部品。

export_report.py から使う部品。直接は実行しない。

見た目は「Markdown を GitHub 風にレンダリングしたもの」に合わせる。和文フォントは
japanize-matplotlib に同梱の IPAex ゴシックを埋め込む（ネットワークに出ない）。
太字の書体が無いため、太字の区間だけ文字の輪郭を太く描く（PDF の文字描画モード 2）。
その描き方の差し替えは PDF を組む間だけ有効にし、終わったら（例外時も）元に戻す。
"""

from __future__ import annotations

import importlib.util
import io
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfgen.textobject import PDFTextObject
from reportlab.platypus import (
    Flowable,
    HRFlowable,
    Image,
    ListFlowable,
    ListItem,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

#: 本文の書体名と、太字の区間を表す書体名（同じフォントファイルを別名で登録する）。
FONT = "IPAexGothic"
BOLD = "IPAexGothic-Bold"

#: フォントに字形の無い記号の置き換え（見た目の近い字形のある文字へ）。
GLYPH_SUBSTITUTES = {"≥": "≧", "≤": "≦"}

_INK = colors.HexColor("#1f2328")
_BORDER = colors.HexColor("#d1d9e0")
_SUBTLE = colors.HexColor("#f6f8fa")
_FOOTER_INK = colors.HexColor("#59636e")
_QUOTE_INK = colors.HexColor("#59636e")

_MARGINS = {"left": 16 * mm, "right": 16 * mm, "top": 18 * mm, "bottom": 20 * mm}
_MAX_IMAGE_HEIGHT = 150 * mm
_MIN_COLUMN_WIDTH = 14 * mm
_CELL_PADDING = 6  # 表のセルの左右の余白（pt、片側）
_BOLD_STROKE_RATIO = 0.02  # 擬似太字の輪郭の太さ（文字サイズ比）
_CODE_LINE_CHARS = 90  # コードブロックを折り返す桁数
_FRAME_PADDING = 6  # SimpleDocTemplate の本文枠の内側の余白（pt、片側）

_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_BLOCK_TAGS = frozenset(
    {"p", "ul", "ol", "pre", "table", "blockquote", "hr", "div", *_HEADINGS}
)


class FontMissingError(Exception):
    """和文フォントのファイルが見つからないこと。"""


def _style(name: str, **overrides: object) -> ParagraphStyle:
    """本文の既定値に *overrides* を重ねた段落スタイルを返す。"""
    base: dict[str, object] = {
        "fontName": FONT,
        "fontSize": 10,
        "leading": 16,
        "textColor": _INK,
        "wordWrap": "CJK",
        "spaceAfter": 5,
    }
    return ParagraphStyle(name, **{**base, **overrides})


_STYLES = {
    "p": _style("p"),
    "h1": _style("h1", fontName=BOLD, fontSize=18, leading=26, spaceAfter=4),
    "h2": _style(
        "h2", fontName=BOLD, fontSize=14, leading=20, spaceBefore=14, spaceAfter=3
    ),
    "h3": _style(
        "h3", fontName=BOLD, fontSize=12, leading=18, spaceBefore=10, spaceAfter=4
    ),
    "cell": _style("cell", fontSize=9, leading=13, spaceAfter=0),
    "th": _style("th", fontName=BOLD, fontSize=9, leading=13, spaceAfter=0),
    "li": _style("li", spaceAfter=2),
    "pre": _style("pre", fontSize=8.5, leading=12, spaceAfter=0),
    "quote": _style("quote", textColor=_QUOTE_INK),
}


def font_path() -> Path:
    """IPAex ゴシックのファイルの場所を返す。見つからなければ FontMissingError。

    japanize-matplotlib は import すると matplotlib の設定を書き換えるので、
    import はせず、パッケージの置き場所だけを引く。
    """
    spec = importlib.util.find_spec("japanize_matplotlib")
    if spec is None or spec.origin is None:
        raise FontMissingError("japanize-matplotlib が入っていません")
    path = Path(spec.origin).parent / "fonts" / "ipaexg.ttf"
    if not path.is_file():
        raise FontMissingError(f"フォントファイルがありません: {path}")
    return path


def register_fonts() -> None:
    """本文用と太字用の書体名で IPAex ゴシックを登録する（何度呼んでもよい）。"""
    path = str(font_path())
    for name in (FONT, BOLD):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, path))


def missing_glyphs(text: str) -> list[str]:
    """*text* のうちフォントに字形の無い文字を、重複なしで出現順に返す。"""
    char_map = pdfmetrics.getFont(FONT).face.charToGlyph
    seen: dict[str, None] = {}
    for char in text:
        if char.isspace() or ord(char) < 0x20 or char in GLYPH_SUBSTITUTES:
            continue
        if ord(char) not in char_map:
            seen.setdefault(char, None)
    return list(seen)


@contextmanager
def bold_text_mode() -> Iterator[None]:
    """PDF を組む間だけ、BOLD の区間を輪郭付きで描くように差し替える。"""
    original_set, original_inline = PDFTextObject.setFont, PDFTextObject._setFont

    def mode(text_object: PDFTextObject, name: str, size: float) -> None:
        stroke = f"2 Tr {size * _BOLD_STROKE_RATIO:.3f} w"
        text_object._code.append(stroke if name == BOLD else "0 Tr")

    def set_font(self, psfontname, size, leading=None):
        original_set(self, psfontname, size, leading)
        mode(self, psfontname, size)

    def set_font_inline(self, psfontname, size):
        original_inline(self, psfontname, size)
        mode(self, psfontname, size)

    PDFTextObject.setFont, PDFTextObject._setFont = set_font, set_font_inline
    try:
        yield
    finally:
        PDFTextObject.setFont, PDFTextObject._setFont = original_set, original_inline


def substitute(text: str) -> str:
    """字形の無い記号を、字形のある近い記号に置き換える。"""
    for before, after in GLYPH_SUBSTITUTES.items():
        text = text.replace(before, after)
    return text


def inline_markup(el: ET.Element, skip: frozenset[str] = frozenset()) -> str:
    """要素の中身を reportlab の段落記法に変換する（*skip* の子要素は飛ばす）。"""
    out = escape(substitute(el.text or ""))
    for child in el:
        if child.tag not in skip:
            out += _inline_child(child)
        out += escape(substitute(_tail(child)))
    return out


def _tail(child: ET.Element) -> str:
    """子要素の後ろの文字を返す。改行（br）の直後の行頭の空白は落とす。"""
    tail = child.tail or ""
    return tail.lstrip() if child.tag == "br" else tail


def _inline_child(child: ET.Element) -> str:
    """段落の中の子要素一つを段落記法にする。画像は別に置くので文字にしない。"""
    body = inline_markup(child)
    if child.tag in ("strong", "b"):
        return f'<font name="{BOLD}">{body}</font>'
    if child.tag == "code":
        return f'<font backColor="#eff1f3" size="9">{body}</font>'
    if child.tag == "br":
        return "<br/>"
    if child.tag == "img":
        return ""
    return body  # em・a など: 文字だけ残す


def _plain(el: ET.Element) -> str:
    """要素の文字だけを返す（表の列幅の見積もり用）。"""
    return substitute("".join(el.itertext()))


def column_widths(natural: list[float], total: float) -> list[float]:
    """各列の自然な幅 *natural* から、合計が *total* になる列幅を配分する。

    各列は最小幅を確保し、一列が枠幅の半分を超えないよう上限を設ける。収まるときは
    比例で広げ、収まらないときは狭い列から自然な幅を先に与え、残りを広い列で等分する。
    """
    cap = max(total / 2, _MIN_COLUMN_WIDTH)
    wants = [min(max(width, _MIN_COLUMN_WIDTH), cap) for width in natural]
    if sum(wants) <= total:
        scale = total / sum(wants)
        return [width * scale for width in wants]
    widths = list(wants)
    remaining = total
    pending = sorted(range(len(wants)), key=lambda index: wants[index])
    while pending:
        share = remaining / len(pending)
        if wants[pending[0]] > share:
            for index in pending:
                widths[index] = share
            break
        remaining -= wants[pending[0]]
        pending.pop(0)
    return widths


class _NumberedCanvas(rl_canvas.Canvas):
    """各ページの下中央に「n / N」を描くキャンバス。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []

    def showPage(self) -> None:
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont(FONT, 8)
            self.setFillColor(_FOOTER_INK)
            self.drawCentredString(A4[0] / 2, 10 * mm, f"{self._pageNumber} / {total}")
            super().showPage()
        super().save()


class _Builder:
    """要素の木を reportlab の部品（flowable）の並びに変える。"""

    def __init__(self, images: Mapping[str, Path], width: float) -> None:
        self.images = images
        self.width = width

    def image(self, src: str) -> Flowable:
        """確認済みの画像を、枠に収まるよう縮小して置く（拡大はしない）。"""
        # Image は ImageReader を受け取れないので、パスを渡して一度だけ読ませ、
        # その寸法から描く大きさを決める。
        image = Image(str(self.images[src]), hAlign="CENTER")
        pixel_w, pixel_h = image.imageWidth, image.imageHeight
        scale = min(self.width / pixel_w, _MAX_IMAGE_HEIGHT / pixel_h, 1.0)
        image.drawWidth, image.drawHeight = pixel_w * scale, pixel_h * scale
        return image

    def paragraph(self, el: ET.Element, style_key: str = "p") -> list[Flowable]:
        """段落を置く。中の画像は段落の後ろに図として並べる。"""
        out: list[Flowable] = []
        markup = inline_markup(el)
        if markup.strip():
            out.append(Paragraph(markup, _STYLES[style_key]))
        for img in el.iter("img"):
            out += [Spacer(1, 4), self.image(img.get("src", "")), Spacer(1, 6)]
        return out

    def table(self, el: ET.Element) -> list[Flowable]:
        """表を置く。見出し行は網掛け、本文は一行おきに網掛けする。"""
        rows = [
            row
            for part in el
            for row in (part if part.tag in ("thead", "tbody") else [part])
        ]
        ncol = max(len(row) for row in rows)
        natural = [0.0] * ncol
        data = []
        for row in rows:
            cells = []
            for index, cell in enumerate(row):
                font = BOLD if cell.tag == "th" else FONT
                width = max(
                    pdfmetrics.stringWidth(line, font, 9)
                    for line in _plain(cell).split("\n")
                )
                natural[index] = max(natural[index], width + 2 * _CELL_PADDING)
                cells.append(
                    Paragraph(
                        inline_markup(cell),
                        _STYLES["th" if cell.tag == "th" else "cell"],
                    )
                )
            data.append(cells + [""] * (ncol - len(cells)))
        table = Table(data, colWidths=column_widths(natural, self.width), repeatRows=1)
        commands = [
            ("GRID", (0, 0), (-1, -1), 0.6, _BORDER),
            ("BACKGROUND", (0, 0), (-1, 0), _SUBTLE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        commands += [
            ("BACKGROUND", (0, i), (-1, i), _SUBTLE) for i in range(2, len(rows), 2)
        ]
        table.setStyle(TableStyle(commands))
        return [table, Spacer(1, 6)]

    def list_item(self, li: ET.Element) -> ListItem:
        """箇条書きの一項目を置く。入れ子の箇条書きや段落はその下に続ける。"""
        flows: list[Flowable] = []
        markup = inline_markup(li, skip=_BLOCK_TAGS)
        if markup.strip():
            flows.append(Paragraph(markup, _STYLES["li"]))
        for child in li:
            if child.tag == "p":
                flows += self.paragraph(child, "li")
            elif child.tag in _BLOCK_TAGS:
                flows += self.block(child)
        return ListItem(flows or [Paragraph("", _STYLES["li"])], leftIndent=14)

    def bullet_list(self, el: ET.Element) -> list[Flowable]:
        """箇条書き（ul）と番号付き（ol）を置く。"""
        items = [self.list_item(li) for li in el if li.tag == "li"]
        if el.tag == "ol":
            options = {"bulletType": "1", "start": el.get("start", "1")}
        else:
            options = {"bulletType": "bullet", "start": "•"}
        flow = ListFlowable(
            items,
            bulletFontName=FONT,
            bulletFontSize=10,
            bulletOffsetY=-1,
            leftIndent=14,
            spaceAfter=4,
            **options,
        )
        return [flow]

    def quote(self, el: ET.Element) -> list[Flowable]:
        """引用を、左に縦線を引いた枠の中に置く。"""
        inner = [flow for child in el for flow in self.block(child, quoted=True)]
        if not inner:
            return []
        box = Table([[inner]], colWidths=[self.width])
        box.setStyle(
            TableStyle(
                [
                    ("LINEBEFORE", (0, 0), (0, 0), 3, _BORDER),
                    ("LEFTPADDING", (0, 0), (0, 0), 10),
                ]
            )
        )
        return [box, Spacer(1, 6)]

    def code_block(self, el: ET.Element) -> list[Flowable]:
        """コードブロックを、網掛けの枠の中に等幅の見た目で置く。"""
        text = substitute("".join(el.itertext()).rstrip("\n"))
        code = Preformatted(
            text, _STYLES["pre"], maxLineLength=_CODE_LINE_CHARS, newLineChars=""
        )
        box = Table([[code]], colWidths=[self.width])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), _SUBTLE)]))
        return [box, Spacer(1, 6)]

    def heading(self, el: ET.Element) -> list[Flowable]:
        """見出しを置く。h1・h2 には下線を引く。"""
        key = el.tag if el.tag in _STYLES else "h3"
        title = Paragraph(inline_markup(el), _STYLES[key])
        title.keepWithNext = True
        if el.tag not in ("h1", "h2"):
            return [title]
        return [
            title,
            HRFlowable(width="100%", thickness=0.6, color=_BORDER, spaceAfter=6),
        ]

    def block(self, el: ET.Element, quoted: bool = False) -> list[Flowable]:
        """ブロック要素一つを部品の並びにする。"""
        if el.tag in _HEADINGS:
            return self.heading(el)
        if el.tag in ("ul", "ol"):
            return self.bullet_list(el)
        if el.tag == "table":
            return self.table(el)
        if el.tag == "hr":
            return [
                HRFlowable(
                    width="100%",
                    thickness=1.5,
                    color=_BORDER,
                    spaceBefore=8,
                    spaceAfter=10,
                )
            ]
        if el.tag == "pre":
            return self.code_block(el)
        if el.tag == "blockquote":
            return self.quote(el)
        return self.paragraph(el, "quote" if quoted else "p")


def build_pdf(root: ET.Element, images: Mapping[str, Path], title: str) -> bytes:
    """要素の木 *root* から PDF を組み立て、そのバイト列を返す。

    *images* は報告書中の画像の src から、確認済みの実在ファイルへの対応。
    フォントが見つからなければ FontMissingError を送出する。
    """
    register_fonts()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=title,
        leftMargin=_MARGINS["left"],
        rightMargin=_MARGINS["right"],
        topMargin=_MARGINS["top"],
        bottomMargin=_MARGINS["bottom"],
    )
    builder = _Builder(images, doc.width - 2 * _FRAME_PADDING)
    story = [flow for el in root for flow in builder.block(el)]
    with bold_text_mode():
        doc.build(story, canvasmaker=_NumberedCanvas)
    return buffer.getvalue()
