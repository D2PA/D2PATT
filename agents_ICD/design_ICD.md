# design_ICD — 分析設計の受け渡し

**PM ↔ analysis-designer ↔ econometrician ↔ mayor**

## この文書について

この文書は、分析設計の往復と、収束後の行動可能性チェックで行き来する依頼と返答の形を定める。往復の駆動（ラウンドカウンタの呼び方・収束判定・最終ラウンドの決まり）は `playbooks/flow.md` → 分析設計が、書類の形は `schemas/` 配下の JSON Schema が定める。

この工程では、計画書・要件のまとめ・データ検査の結果を、中身ではなくファイルの場所で渡す。受け手はそのファイルを Read で読む。指摘書（ReviewResult）と首長のコメントは、そのまま依頼文に載せる。

改訂するときは、designer が計画書の全体を返すのではなく、変更点を一覧にして返す。

## 1. PM → analysis-designer：初回設計の依頼

タイミング：分析設計の開始時（ラウンドカウンタの初期化後）。

```json
{
  "step": "design",
  "csv_path": "<元 CSV の絶対パス。データ監査の依頼（csv_paths）に渡したもの>",
  "output_dir": "<OUTPUT_DIR の絶対パス>",
  "requirements_path": "<OUTPUT_DIR/requirements_summary.json の絶対パス>",
  "data_audit_path": "<OUTPUT_DIR/data_audit.json の絶対パス>",
  "research_candidates_path": "<OUTPUT_DIR/research_candidates.json の絶対パス。外部データ調査を行った案件のみ>"
}
```

designer は初版 AnalysisBrief（形は `schemas/analysis-brief.json`）を返す。計画書の全体が返答に載るのは、この一回だけである。PM はこれを `<OUTPUT_DIR>/analysis_brief.json` に保存する。以後の往復では、designer も econometrician もこのファイルを読む。派生列の名指し方（分析が読む列には派生後の名前を書き、作り方は rationale の文章に記す）は `.claude/agents/analysis-designer.md` が定める。

## 2. PM → econometrician：計画レビューの依頼（各ラウンド）

```json
{
  "brief_path": "<OUTPUT_DIR/analysis_brief.json の絶対パス>",
  "requirements_path": "<OUTPUT_DIR/requirements_summary.json の絶対パス>",
  "data_audit_path": "<OUTPUT_DIR/data_audit.json の絶対パス>",
  "previous_review": { "...": "前ラウンドの ReviewResult 全体。2ラウンド目以降のみ" },
  "previous_changes": { "...": "前ラウンドの DesignDecision の changes 全体。2ラウンド目以降のみ" },
  "is_final_round": false
}
```

`is_final_round` には、ラウンドカウンタの `at_cap` をそのまま写す。`previous_review` と `previous_changes` は、2ラウンド目以降に必ず付ける（1ラウンド目には付けない）。econometrician は ReviewResult（形は `schemas/review-result.json`）を返す。

## 3. PM → analysis-designer：改訂の依頼（各ラウンド）

```json
{
  "step": "revise",
  "brief_path": "<OUTPUT_DIR/analysis_brief.json の絶対パス>",
  "review": { "...": "ReviewResult 全体" },
  "requirements_path": "<OUTPUT_DIR/requirements_summary.json の絶対パス>",
  "data_audit_path": "<OUTPUT_DIR/data_audit.json の絶対パス>",
  "is_final_round": false
}
```

designer は DesignDecision（形は `schemas/design-decision.json`）を返す。計画書の全体は返さない。返すのは判定（verdict）と、変更点の一覧（changes）と、改訂の要旨（revision_notes）である。

**changes の中身。** 四つの操作だけを持つ。該当の無い欄は付けない。

```json
{
  "field_changes": [
    { "field": "policy_question", "new_value": "...", "reason": "..." }
  ],
  "task_changes": [
    { "task_name": "<既存タスクの名前>", "field": "method", "new_value": "...", "reason": "..." }
  ],
  "added_tasks": [
    { "task": { "...": "AnalysisTask 全体" }, "reason": "..." }
  ],
  "deleted_tasks": [
    { "task_name": "<既存タスクの名前>", "reason": "..." }
  ]
}
```

- 宛先は名前で書く。タスクは `task_name`、欄は欄の名前で指す。位置や番号では指さない。
- `reason` はすべての変更に付ける。どの指摘への対応かが読み取れるように書く。
- タスクの改名は操作として持たない。要るときは削除と追加で書く。
- 変更が無いラウンドは、`verdict` を `done` にして `changes` の中身を空にする。

**適用。** PM は受け取った changes を `<OUTPUT_DIR>/.design_changes_r<ラウンド番号>.json` に保存し、`apply_brief_changes.py` で計画書に適用する。PM が計画書を直接編集することはない。

```bash
bash "$LAYER_DIR/tools/run" apply_brief_changes.py \
  --brief "$OUTPUT_DIR/analysis_brief.json" \
  --changes "$OUTPUT_DIR/.design_changes_r<ラウンド番号>.json"
```

道具が 0 以外の番号で終わったときの扱いは `CLAUDE.md` → 「動かすときの約束」のとおり。道具が断る条件（宛先が実在しない・課長が決める欄に触れている・同じ場所を二度変えている・適用後の形が様式に合わない）は道具自身が説明を出す。

## 4. PM → mayor：行動可能性チェックの依頼（収束後・1回）

```json
{
  "brief_path": "<OUTPUT_DIR/analysis_brief.json の絶対パス>",
  "requirements_path": "<OUTPUT_DIR/requirements_summary.json の絶対パス>"
}
```

mayor は MayorFeasibilityCheck（形は `schemas/mayor-feasibility-check.json`）を返す。`action_required` の後続（designer への回付・ラウンド消費の扱い）は `playbooks/flow.md` → 分析設計が定める。回付するときは、3. の形で `review` の代わりに首長の `comments` を載せる。

## 5. 案件フォルダに残るファイル

| ファイル | 書く役 | 読む役 |
|---|---|---|
| `requirements_summary.json` | PM（要件整理で保存） | analysis-designer・econometrician・mayor |
| `data_audit.json` | PM（データ把握で保存） | analysis-designer・econometrician |
| `analysis_brief.json` | 初版は PM が保存。以後は `apply_brief_changes.py` だけが書き換える | analysis-designer・econometrician・mayor・後段の工程 |
| `.design_changes_*.json` | PM（改訂ラウンドは r<n>、首長対応は mayor、列名の解決は columns として受領時に保存） | `apply_brief_changes.py`。改訂の記録として残す |
| `design_review_r{N}.json` | PM（ラウンド N の指摘書（ReviewResult）を受領時に保存） | 走行後に設計の指摘を辿るための記録として残す |
| `design_review_mayor.json` | PM（首長の行動可能性チェック（MayorFeasibilityCheck）を受領時に保存） | 同上 |

計画書を書き換えるのは `apply_brief_changes.py` だけである。承認（計画の確認）の後は、`playbooks/flow.md` の定めにより凍結する。
