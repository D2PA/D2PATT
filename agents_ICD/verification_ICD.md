# verification_ICD — 結果検証の受け渡し

**PM ↔ econometrician ↔ analysis-designer ↔ data-analyst**

## この文書について

この文書は、結果検証の往復で行き来する依頼と返答の形を定める。この工程の目的と守り（結果を見た後に主張を強めない・承認済み brief の凍結・収束の決まり）と駆動の手順は `playbooks/flow.md` → 結果検証が、書類の形は `schemas/` 配下の JSON Schema が定める。

この工程では、大きな書類はパスで渡し、受け手が Read で読む（依頼文に埋め込まない）。

## 1. PM → econometrician：結果レビューの依頼（各ラウンド）

```json
{
  "step": "results_review",
  "brief_path": "<OUTPUT_DIR/analysis_brief.json の絶対パス>",
  "analysis_output_path": "<OUTPUT_DIR/analysis_output.json の絶対パス>",
  "data_audit_path": "<OUTPUT_DIR/data_audit.json の絶対パス>",
  "lint_summary": "<lint_results.py の重要指摘の要約。無ければ「指摘なし」>",
  "reference_values_path": "<絶対パス。参照値があるときのみ>",
  "is_final_round": false
}
```

`is_final_round` には、ラウンドカウンタの `at_cap` をそのまま写す。econometrician は ReviewResult（形は `schemas/review-result.json`。must_fix の項目に付ける `fix_type` の5型もそこが定める）を返す。

## 2. PM → analysis-designer：対応の依頼（各ラウンド）

```json
{
  "step": "respond_to_results_review",
  "review": { "...": "ReviewResult 全体" },
  "analysis_output_path": "<OUTPUT_DIR/analysis_output.json の絶対パス>",
  "brief_path": "<OUTPUT_DIR/analysis_brief.json の絶対パス>",
  "is_final_round": false
}
```

designer は VerificationResponse（形は `schemas/verification-response.json`。各指摘への対応と、探索的に追加するタスクの仕様を載せる）を返す。

## 3. PM → data-analyst：出力改訂の依頼（適用が要るときのみ）

タイミング：2. の応答の受領後、適用すべき対応（受け入れられた must_fix・探索的追加・結果の確認で人間が指示した削除）があるとき。

```json
{
  "request_type": "revise_output",
  "analysis_output_path": "<OUTPUT_DIR/analysis_output.json の絶対パス>",
  "claim_downgrades": [ { "task_name": "...", "new_claim": "descriptive|correlation_only", "reason": "..." } ],
  "annotations": [ { "task_name": "...", "text": "<caution へ書き添える限界>" } ],
  "rerun_tasks": [ { "task_name": "<既存と同一>", "fix_type": "error_refix|robustness_run", "instruction": "..." } ],
  "added_tasks": [ { "...": "VerificationResponse.added_tasks の AnalysisTask 仕様をそのまま転記" } ],
  "delete_tasks": ["<人間が削除を指示した探索的追加の task_name>"],
  "data_audit_path": "<OUTPUT_DIR/data_audit.json の絶対パス>",
  "reference_values_path": "<絶対パス。参照値があるときのみ>"
}
```

該当の無い欄は付けない。data-analyst は再実行分を data-scientist へ委譲し、編集を適用し、AnalysisOutput を組み立て直して同じパスに保存し、パスと更新後の要点一覧を返す。適用の決まり（同じ task_name の更新・追加タスクへの札・主張は下げる方向にだけ適用）は `.claude/agents/data-analyst.md` が定める。

## 4. ラウンドカウンタ

結果検証は、分析設計とは別の状態ファイルを使う（設計の状態を上書きしない）。収束の判定は `playbooks/flow.md` → 結果検証が定める。上限ラウンド数は `config.json` の `verification_max_rounds` で定める（省略時は組み込み既定）。

| 操作 | タイミング |
|---|---|
| `init --phase verification --state-file <OUTPUT_DIR>/.verification_round_state.json` | ループ開始時 |
| `increment --state-file <同上>` | 各ラウンド冒頭（1. の前） |
| `status --state-file <同上>` | 必要時 |

## 5. 機械検問

data-analyst の revise_output が返るたびに、PM はループ開始前のベースラインと現在の結果を突き合わせる：

```bash
bash "$LAYER_DIR/tools/run" check_result_lock.py \
  --baseline "$OUTPUT_DIR/.verification_baseline.json" \
  --current "$OUTPUT_DIR/analysis_output.json"
```

0 以外で終わったときの扱いは `CLAUDE.md` → 「動かすときの約束」のとおり（止めて、道具の説明を人間に見せる）。

## 6. 保存先

| 成果物 | パス |
|---|---|
| ループ開始前のベースライン | `<OUTPUT_DIR>/.verification_baseline.json` |
| ラウンド N の指摘書（ReviewResult） | `<OUTPUT_DIR>/verification_review_r{N}.json` |
| ラウンド N の対応（VerificationResponse） | `<OUTPUT_DIR>/verification_response_r{N}.json` |
| ラウンドカウンタ状態 | `<OUTPUT_DIR>/.verification_round_state.json` |
