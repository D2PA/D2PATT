---
name: report-export
description: 報告書（Markdown）を PDF と Word に書き出して、案件の output フォルダに置く。最終承認のあとに PM が使うほか、課長がいつでも名前で呼んで使える。引数に案件名か Markdown のパスを取れる（省略可）。
---

# SKILL: Report Export（報告書を PDF と Word に書き出す）

呼ばれたら、報告書の Markdown を PDF（配る用）と Word（課長が手直しする用）に書き出す。
中身は変えない。見た目だけを整える。

---

## 呼ばれたらすること

### 1. 対象を決める

引数があれば、それが対象。案件名（`260815_01`）なら
`$LAYER_DIR/projects/<案件名>/output/report_draft.md` に読み替える。Markdown のパスなら、
そのファイルが対象。**引数があるときは、次の質問をしない。**

引数が無ければ、報告書のある案件を新しい順に集める。

```bash
ls -dt "$LAYER_DIR"/projects/*/ 2>/dev/null | while read -r d; do
  [ -f "$d/output/report_draft.md" ] && echo "$(basename "$d") | $(date -r "$d/output/report_draft.md" "+%Y-%m-%d")"
done
```

- 0件なら、報告書のある案件が無いことを伝えて終わる
- 1件なら、質問せずにそれを使い、どれを使ったかを伝える
- 2件以上なら、上から4件までを `AskUserQuestion` で尋ねる。`header` は `案件`、各選択肢の
  `label` は案件名、`description` は `<報告書の更新日>`。5件以上あるときは、質問文に
  「他 N 件は『その他』に案件名を入力」と添える

「その他」に打たれた案件名は、上の一覧にあるかを確かめる。無ければ一覧を見せて、もう一度
尋ねる。**推測で近い名前に読み替えない。**

### 2. 形式を尋ねる

`AskUserQuestion` で尋ねる。`header` は `形式`。

| label | description |
|-------|-------------|
| PDF と Word の両方 | PDF は配布用、Word は手直し用 |
| PDF のみ | 配布用 |
| Word のみ | 手直し用 |
| 書き出さない | 何も作らずに終わる |

「書き出さない」なら、ここで終わる。

### 3. 書き出す

出力先は、報告書と同じフォルダ。選ばれた形式のブロックを一つだけ動かす。
`REPORT` には報告書の絶対パスを入れる。

```bash
# PDF と Word の両方
REPORT="<報告書の絶対パス>"; OUT="$(dirname "$REPORT")"
bash "$LAYER_DIR/tools/run" export_report.py --report "$REPORT" --pdf "$OUT/report.pdf" --docx "$OUT/report.docx"
```

```bash
# PDF のみ
REPORT="<報告書の絶対パス>"; OUT="$(dirname "$REPORT")"
bash "$LAYER_DIR/tools/run" export_report.py --report "$REPORT" --pdf "$OUT/report.pdf"
```

```bash
# Word のみ
REPORT="<報告書の絶対パス>"; OUT="$(dirname "$REPORT")"
bash "$LAYER_DIR/tools/run" export_report.py --report "$REPORT" --docx "$OUT/report.docx"
```

終了番号で分ける。

| 番号 | 意味 | すること |
|------|------|---------|
| 0 | 書き出せた | 手順 4 へ |
| 1 | 変換できなかった（図が見つからない、など） | 手順 3b へ |
| 2 | 使い方の誤り | 道具の説明に従って引数を直し、もう一度だけ動かす。直せなければ手順 3b と同じく伝えて終わる |
| 3 | 同じ名前のファイルが既にある | 手順 3a へ |

`警告:` で始まる行が出たとき（PDF で表示できない文字があった）は、書き出しは済んでいる。
どの文字が PDF で空白や四角になるかを、課長に伝える。

#### 3a. 既にあるファイルの扱いを尋ねる

課長が Word を手直ししたあとかもしれない。**黙って上書きしない。** `AskUserQuestion` で
尋ねる。`header` は `上書き`。

| label | description |
|-------|-------------|
| 上書きする | 今あるファイルを置き換える。Word の手直しは消える |
| 別名で残す | 今あるファイルは残し、日時を付けた名前で新しく作る |
| やめる | 何も作らずに終わる |

- 上書きする → 手順 3 と同じブロックの末尾に `--force` を付けて動かす
- 別名で残す → 手順 3 と同じブロックで、出力先のファイル名だけを
  `report_$(date +%Y%m%d-%H%M).pdf`／`report_$(date +%Y%m%d-%H%M).docx` に替えて動かす

#### 3b. 書き出せなかったことを伝える

道具の説明を、専門用語を使わずに言い換えて課長に伝える。言い換えで中身を変えない。
あわせて、次の二つを伝える。

- 報告書（`report_draft.md`）はそのまま残っており、何も変わっていないこと
- 書き出しのために報告書を直すかどうかは課長が決めること。直すなら、それは報告書の
  修正であり、最終確認をやり直すことになる

**自分で報告書を直さない。** 報告書を直すのは report-writer の仕事である
（`playbooks/pm.md` → 成果物の扱い）。

### 4. 場所を伝える

道具が標準出力に出した絶対パスを、そのまま課長に伝える（`$LAYER_DIR` のままにしない）。
Word は課長が自由に直してよいこと、直しても元の `report_draft.md` は変わらないことを
一言添える。

## 機密性について

この道具が読むのは、報告書と、そこから参照された図だけである。生データは読まない。
図は報告書と同じフォルダの中にあるものだけを読み、インターネットには出ない。報告書の中の
HTML は実行せず、文字として出す。これは「情報の機密性」に沿う。詳細は次にある。

    `CLAUDE.md` → 守るべき制約
