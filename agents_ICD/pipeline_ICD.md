# pipeline_ICD — データ監査・前処理・分析実行の受け渡し

**PM ↔ data-analyst ↔ data-scientist**

## この文書について

この文書は、データ監査・前処理・分析実行の三つの段階で、PM・data-analyst・data-scientist の間を行き来する依頼と返答の形を定める。

本書が定めるのは、どのやり取りで・どの書類を・何を添えて渡すか、だけである。役の分担と委譲の経路は `CLAUDE.md` → 「体制」が、データの取り扱いの制約は `CLAUDE.md` → 「守るべき制約」が、手順の進め方（いつ・どの順で行うか）は `playbooks/flow.md` が、書類そのものの形は `schemas/` 配下の JSON Schema が定める。

## 1. データ監査（要件整理の中・要件の確認の前）

監査の依頼は、入口で人間が宣言する `security_concern` が true（懸念あり）の案件でだけ発行する（宣言と分岐は `playbooks/flow.md` → データ把握が定める）。false のとき、この依頼は発行されない——PM が `profile_data.py` で同じ形の DataAuditResult（データ検査報告）を得る。どちらの道でも、結果は `$OUTPUT_DIR/data_audit.json` に保存され、以後の依頼（2-1・3-1）にはそのパスを渡す。

### 1-1. PM → data-analyst：監査の依頼

```json
{
  "request_type": "data_audit",
  "csv_paths": ["<元（マスク前）CSVの絶対パス。案件フォルダの input/ 配下>"]
}
```

### 1-2. data-analyst → data-scientist：監査の実行依頼

マーカー規約（§4）で渡す。

```
TASK_JSON: "data_audit"
CSV_PATH: <元CSVの絶対パス>
OUTPUT_DIR: <案件フォルダの output/ の絶対パス>
```

### 1-3. data-scientist → data-analyst：監査の結果

```
AUDIT_JSON: {
  "n_rows": 195,
  "n_cols": 42,
  "columns": ["学校名", "学年", "性別", "定住意向", "..."],
  "missing_counts": {"定住意向": 3, "自由記述": 18},
  "duplicate_rows": 0,
  "quality_issues": [
    "列『生徒ID』は個人識別子の可能性あり（文字列型・ユニーク率高）",
    "列『住所』は個人情報の可能性あり"
  ]
}
```

形は `schemas/analysis-output.json` → `$defs/DataAuditResult`。指摘は列名・型・集計値から行い、値の中身は見ない。

### 1-4. data-analyst → PM：監査結果の返却

data-analyst は AUDIT_JSON の中身（DataAuditResult）をそのまま PM へ返す。PM はこれを要件の確認で人間に提示し、`$OUTPUT_DIR/data_audit.json` に保存する。以後の依頼（2-1・3-1）には、このファイルのパスを渡す。

## 2. 前処理（分析設計の収束後・計画の確認の前）

前処理では、派生列（元の列から計算して作る列）を生データから一度だけ作り、全タスクが読む共有の `preprocessed_input.csv` を作る。受けた data-analyst の中の手順（派生→結合→マスク→検査の順序と、その理由）は `.claude/agents/data-analyst.md` が定める。

### 2-1. PM → data-analyst：前処理の依頼

```json
{
  "request_type": "preprocess",
  "brief_path": "<案件フォルダの output/analysis_brief.json の絶対パス>",
  "data_audit_path": "<案件フォルダの output/data_audit.json の絶対パス>",
  "external_merges": [ "外部データを結合するときのみ" ],
  "external_references": [ "参照値を抽出するときのみ" ]
}
```

- `brief_path` は計画書の場所。data-analyst はこれを Read で読み、`check_survival.py --brief` も同じファイルを読む。
- `data_audit_path` は毎回添える。data-analyst は走行ごとに新しく起動して監査の結果を持っておらず、Read で読んで派生列の見分け（計画書の変数名と `columns` の突き合わせ）に使うためである。
- `external_merges`・`external_references` は該当があるときだけ付ける。形と組み立て方は `playbooks/external_data.md` が定める。

### 2-2. data-analyst → data-scientist：前処理の実行依頼

マーカー規約（§4）で渡す。

```
TASK_JSON: {
  "type": "preprocess",
  "derived_columns": [ { "name": "<作る列の名前>", "description": "<作り方の説明>" } ],
  "external_merges": [ "2-1 で受け取ったものをそのまま（あるときのみ）" ],
  "output_path": "<案件フォルダの output/derived_input.csv の絶対パス>"
}
CSV_PATH: <元（マスク前）CSVの絶対パス>
OUTPUT_DIR: <案件フォルダの output/ の絶対パス>
```

どの変数を派生列とするか、`description` に何を写すかは、`.claude/agents/data-analyst.md` が定める。

### 2-3. data-scientist → data-analyst：前処理の結果

```
PREPROCESS_JSON: {
  "output_path": "<derived_input.csv の絶対パス>",
  "columns": ["<派生・結合後の全列名>"],
  "n_rows": 195,
  "reference_values_path": "<reference_values.json の絶対パス。参照値を抽出したときのみ>"
}
```

- `columns` は、派生と結合を終えた後の全列名。付け替え済みの外部列も含む。
- `reference_values_path` は、依頼に `external_references` があったときだけ返す。参照値ファイルの形は `schemas/reference-values.json` が定める。
- 結合できた行の割合は自己申告しない。`check_survival.py` の `non_null_count` が機械的に測る。

### 2-4. data-analyst → PM：前処理の返却

```json
{
  "preprocessed_csv_path": "<案件フォルダの output/preprocessed_input.csv の絶対パス>",
  "survival_result": {
    "overall_status": "has_degenerate",
    "results": [
      { "variable": "settlement_type", "status": "degenerate", "distinct_non_null_count": 1 },
      { "variable": "定住意向", "status": "ok", "distinct_non_null_count": 4 }
    ]
  },
  "reference_values_path": "<絶対パス。参照値を抽出したときのみ>"
}
```

- `results[*].status` は3値。`ok`／`degenerate`（列はあるが、欠損を除いた値の種類が2未満で、分析に使えない）／`not_found`（列が見つからない）。
- `overall_status` も3値。`all_usable`（全変数 ok）／`needs_resolution`（not_found あり・degenerate 無し）／`has_degenerate`（degenerate が1つ以上）。
- 生の値・値の一覧は返さない。返すのは列名・件数・状態だけ。

PM は survival_result を計画の確認で提示する。`needs_resolution` のときは、提示の前に列名の解決を行う（`playbooks/flow.md` → 列名の解決）。

## 3. 分析実行（計画の確認の承認後・結果の確認の前）

### 3-1. PM → data-analyst：実行の依頼

```json
{
  "request_type": "execute",
  "brief_path": "<案件フォルダの output/analysis_brief.json の絶対パス>",
  "data_audit_path": "<案件フォルダの output/data_audit.json の絶対パス>",
  "reference_values_path": "<絶対パス。前処理で返っていたときのみ>"
}
```

- `brief_path` は前処理と同じ承認済み計画書の場所。列名の解決を行った場合は、解決が反映済みのファイルになっている。
- `data_audit_path` は毎回添える。data-analyst はこのファイルを Read で読み、AnalysisOutput の `data_audit` 欄に写して埋める（自分では監査していないため）。
- `reference_values_path` は、前処理の返却（2-4）にあったときだけ付ける。

### 3-2. data-analyst → data-scientist：タスクごとの実行依頼

brief の各タスクについて一つずつ、マーカー規約（§4）で渡す。

```
TASK_JSON: { "...": "AnalysisTask 一件（brief.tasks の要素をそのまま）" }
CSV_PATH: <案件フォルダの output/preprocessed_input.csv の絶対パス>
OUTPUT_DIR: <案件フォルダの output/ の絶対パス>
REFERENCE_VALUES: <reference_values.json の絶対パス。そのタスクの external_refs が空でないときのみ>
```

### 3-3. data-scientist → data-analyst：タスクの結果

```
RESULT_JSON: { "task_name": "...", "method": "...", "...": "..." }
```

形は `schemas/analysis-output.json` → `$defs/SingleAnalysisResult`。`task_name` は依頼した `AnalysisTask.name` と完全に一致させる。

### 3-4. data-analyst → PM：分析結果一式の返却

全タスクの結果を AnalysisOutput（形は `schemas/analysis-output.json`）に組み立て、`<OUTPUT_DIR>/analysis_output.json` に保存する（後段の道具はこのファイルを読む）。`data_audit` 欄には 3-1 のファイルの中身をそのまま写し、実行できなかったタスクは `skipped_tasks` に理由を書く。PM へは、保存先のパスと要点一覧を返す——各タスクの発見（`key_finding`）の平易文と主張の強さ（`causation_claim`）、`skipped_tasks`、critical な指摘の有無。JSON の全体は返さない。

## 4. マーカー規約（data-analyst ↔ data-scientist）

data-analyst と data-scientist のやり取りは、JSON の書類ではなく、プレーンテキストにマーカーを付けて行う。

**入力（data-analyst → data-scientist）**

| マーカー | 中身 | 出どころ |
|---|---|---|
| `TASK_JSON` | タスクの指定。形はタスク型で変わる（下表） | 監査は固定文字列。前処理は 2-2 のとおり。分析は `brief.tasks` の一件 |
| `CSV_PATH` | 読むCSVの絶対パス（下表） | PM が解決して渡す |
| `OUTPUT_DIR` | 案件フォルダの output/ の絶対パス | PM が解決して渡す |
| `REFERENCE_VALUES` | 参照値ファイルの絶対パス。任意 | 前処理の返却（2-4）の `reference_values_path`。そのタスクの `external_refs` が空でないときのみ |

**タスク型と CSV_PATH**

| タスク型 | TASK_JSON | CSV_PATH |
|---|---|---|
| 監査 | 文字列 `"data_audit"` | 元（マスク前）CSV |
| 前処理 | 2-2 のオブジェクト | 元（マスク前）CSV |
| 通常分析 | AnalysisTask 一件 | 共有 `preprocessed_input.csv` |

監査と前処理が元CSVを読むのは例外である。前処理済みCSVがまだ無く、派生は生の値から作るためである。どのタスク型でも、出力は集計レベルのみとする（行データは不可）。

**出力（data-scientist → data-analyst）**

返答の最終行を、次のいずれかのマーカーで終える。

| マーカー | 使う場面 | 形 |
|---|---|---|
| `AUDIT_JSON` | 監査 | `schemas/analysis-output.json` → `$defs/DataAuditResult` |
| `PREPROCESS_JSON` | 前処理 | 2-3 のとおり |
| `RESULT_JSON` | 通常分析 | `schemas/analysis-output.json` → `$defs/SingleAnalysisResult` |

## 5. 検証

PM は、ゲートで提示する書類を、提示の前に `validate_icd.py` で検証する。本書の範囲で対象になるのは analysis-brief（計画の確認）と analysis-output（結果の確認）。

```bash
bash "$LAYER_DIR/tools/run" validate_icd.py \
  analysis-output "<OUTPUT_DIR>/analysis_output.json"
```
