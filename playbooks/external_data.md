# Playbook: 外部データ（`external_data_requested=true` のときのみ読む）

外部データの手順の本体。決まりは `../CLAUDE.md` が定め、食い違うときは `CLAUDE.md` に
従う。ゲート提示のテンプレート（【外部データ】【比較に使う参考値】の節と提示のしかた）は
`playbooks/flow.md` の「計画の確認」にある。

## 外部データ調査

**入口で人間が `external_data_requested=false` と宣言した場合は外部データ調査を丸ごと
スキップする**（計測も researcher 委譲もしない）。`true` のときだけ以下を実施する。

実施する場合のみ計測する（スキップなら計測もスキップ）：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py start --phase "research" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

要件の確認の後（かつ `external_data_requested=true`）、`researcher` に公開オープンデータの
候補探索を委譲する。パイプラインを決してブロックしない——候補ゼロでも先へ進む。
渡すのは、要件のまとめとデータ検査結果のパス
（`requirements_path`・`data_audit_path`。researcher が Read で読む）。`ResearchOutput`
（`agents_ICD/schemas/research-output.json`）を受け取る。
`$OUTPUT_DIR/research_candidates.json`（絶対パス）に保存する
（提案のみ。外部データ本体は保存しない）。分析設計では、このファイルのパスを
analysis-designer への依頼に付ける（採否は designer が決める）。プロトコルは `agents_ICD/research_ICD.md`。

研究ステップ終了時（開始後スキップした場合も）に外部データ調査の計測を閉じる：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py end --phase "research" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```


## 前処理 — 外部データのサブ工程

### 取得

**`brief.external_data` が非空のときのみ。** fetch 計画は PM が手書き
せず、`fetch_external.py --from-brief` がブリーフの `external_data` から内部生成する（URLが
LLM の手書き転記を一度も通らない）：

```bash
bash "$LAYER_DIR/tools/run" fetch_external.py \
  --from-brief "$OUTPUT_DIR/analysis_brief.json" \
  --out-dir "$CASE_DIR/external" \
  --manifest "$OUTPUT_DIR/external_manifest.json"
```

終了コードで分岐する：

- **exit 0** → 全ソース利用可。「外部ファイルの構造把握」へ。
- **exit 1** → `external_manifest.json` を読み、失敗ソースを分類する。
  - `manual_file_missing`（人間の配置待ち）→ 人間に配置を依頼する。**配置先は当該エントリの
    `path`（ツールが作成済みのドロップフォルダの絶対パス）を機械的に転記する**——打ち直さない・短縮
    しない・言い換えない（フォルダ名は `src_ef4135a64096` のような不透明な文字列になりうる
    ため、転記ミスがそのまま「配置したのに見つからない」事故になる）。提示テンプレ：

    ```
    【[source_name] のファイル配置をお願いします】
    - 配置先フォルダ: [manifest エントリの path を機械的に転記]
    - このフォルダの直下にファイルを **1つだけ** 入れてください。
    - ファイル名は自由です（リネーム不要——ダウンロードしたままの名前で構いません）。
    - サブフォルダの中は見ません。
    - 取得手順: [manual_instructions]
      ※ 手順の文中に保存名・保存先の指定があっても、**上のフォルダ案内が優先**します。
    ```

    配置の報告を受けたら**同コマンドを再実行**（冪等：既取得分は cached）。
  - `manual_multiple_files`（フォルダ内に複数ファイル）→ どれを採用すべきかツールは推測
    しない。当該エントリの `manual_files_found` をそのまま提示し、フォルダ直下に
    **1つだけ**残すよう人間に依頼する（配置先は同じく `path` を機械的に転記）。整理の報告を
    受けたら**同コマンドを再実行**。
  - それ以外の失敗（`host_not_allowed`・HTTP エラー等）は **PM が自動で解決も縮退も
    しない**——失敗ソースと理由を記録し、flow.md「計画の確認」の「外部データの提示のしかた」
    に従って計画の確認の提示に含める（設計は変えない。
    外部変数は survival で `not_found` / `degenerate` として現れるが、原因が取得失敗である
    ことを PM が計画の確認で明示する）。
- **exit 2** → ハーネス障害として停止し、人間に報告する（自動リトライしない）。

### 外部ファイルの構造把握

**成功ソースのみ。** CSV の外部ファイルには
`profile_data.py` を実行し実列名を得る（構造のみ・行値を読まない。実際の CLI 引数は
`--help` で確認して使う）。CSV 以外（xlsx/json）は PM での構造把握を省略し、「外部列名の解決」では
意図名をそのまま流してよい（不一致は survival が捕捉する。その旨を計画の確認で一言注記）。

### 外部列名の解決

**事前工程・flow.md「前処理」の「列名の解決」と同じ判断規律。** エントリは
`use_mode` で二手に分かれる——**`merge` のエントリだけが結合仕様に進む**。

`reference` のエントリは結合しない（結合キーが無い＝それがこのモードの存在理由）。列名解決も
`external_merges` への記入も**行わず**、代わりに前処理の委譲で `external_references` として渡す。
参照値は分析CSVの列にならないため survival の検査対象でもない（`variables_of_interest` に
入れないのは designer 側の規約）。

`merge` の各エントリについて、`external_keys` と `variables_to_use` を外部プロファイルの実列名に
解決する。`local_keys` は、ローカル監査の実列名に一致するか、brief が定義する派生列名なら
そのまま（派生は DS が作る）。**もっともらしい一致が見つからない外部列は推測で埋めず**、当該
変数を「未解決」として記録し計画の確認で顕在化する。解決結果から `external_merges` 仕様を
組み立てる：

`path` は **manifest エントリの `path` をそのまま転記**する（manifest の `path` は既に絶対パス。
自分で組み立てない・打ち直さない）。`manual` 型のファイルは人間が付けた任意の名前でドロップ
フォルダ内に置かれるため、案件フォルダの `external/` 直下の予測可能なパスには存在しない——
ファイル名の真実の源は manifest だけ。

```json
"external_merges": [
  {
    "source_name": "...",
    "path": "<manifest エントリの path をそのまま転記（既に絶対パス）>",
    "format": "csv|xlsx|json",
    "local_keys": ["..."],
    "external_keys_actual": ["..."],
    "columns": [ { "actual": "<実列名>", "rename_to": "<briefの意図名>" } ],
    "granularity": "<JoinSpec.external_granularity>"
  }
]
```

## 参照値の抽出

`external_references` の各エントリは brief と manifest から**機械的に転記**して組み立てる
（`path`・`sha256` は manifest が真実の源——自分で組み立てない・打ち直さない）：

```json
"external_references": [
  {
    "source_name": "<brief エントリの source_name>",
    "path": "<manifest エントリの path をそのまま転記（既に絶対パス）>",
    "sha256": "<manifest エントリのハッシュ>",
    "variables_to_use": ["<brief の意図名をそのまま>"],
    "granularity_and_period": "<brief エントリの granularity_and_period>"
  }
]
```

DS はこれを受けて、指定された変数だけを集計値として抽出し
`$OUTPUT_DIR/reference_values.json`（出典ハッシュ・粒度と時点つき）を書く。抽出は
**前処理で一度だけ**——全タスクが同じ数値を同じファイルから読む。`data-analyst` は
その絶対パスを `reference_values_path` として返す。**この値を分析実行まで保持する**
（分析実行で `external_refs` が非空のタスクに渡す）。
