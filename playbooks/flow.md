# Playbook: 実行手順（駅 1〜11）

案件を進める手順の本体。決まりは `../CLAUDE.md` が定める。本書と食い違うときは `CLAUDE.md` に従う。

工程7つとゲート4つを一本の道に並べ、1〜11 の通し番号を振る。番号は見出しの順序表示であり、文中の相互参照は名前で行う（あとから間に足せば番号は振り直すが、名前の参照は壊れない）。

## 1. 要件整理

**受付の準備。** まず分析層の根の実値を取る。`$LAYER_DIR` はシェルの中でしか展開されないので、Read や課長への提示には、ここで得た実値を使う（`CLAUDE.md` → 動かすときの約束）：

```bash
echo "$LAYER_DIR"
```

**設定の確認（案件を始める前・毎回・最初に1回）。** 上で得た実値を使って、レビュー往復・追加分析件数の上限（`<層の根>/config.json`）を確認したいかを課長に尋ねる：

```
分析を始める前に、レビュー往復・追加分析件数の上限設定を確認しますか？

1. 確認する — 現在の設定値と意味をお見せし、必要なら変更します
2. すぐに始める — 現在の設定のまま進めます
```

「すぐに始める」なら、そのまま下記「受付」に進む（設定はそのまま使う）。

「確認する」なら、`<層の根>/config.json`（実値に置き換えた絶対パス）を Read で読み、各パラメータの現在値と `description` を平易な日本語で提示する（システム語彙のキー名も併記する——課長が今後の指示で名指しできるように）：

```
【現在の設定】
- 分析設計のレビュー往復の上限（design_max_rounds）: 3
  [description を平易に言い換えたもの]
- 結果検証のレビュー往復の上限（verification_max_rounds）: 2
  [同上]
- 探索的追加分析の件数上限（max_result_additions）: 2
  [同上]

変更したい項目があれば教えてください。無ければこのまま始めます。
```

課長がチャットで変更を指示したら、対象キーの `value` だけを、`<層の根>/config.json`（実値の絶対パス）に対して Edit で書き換える（`description` は書き換えない）。書き換えた**あとに必ず**機械検査を行う：

```bash
bash "$LAYER_DIR/tools/run" harness_config.py
```

exit 0 なら、stdout の JSON（解決後の設定値）を課長にそのまま見せて確認を取り、下記「受付」に進む。exit 2 なら（範囲外の値・型の誤りなど）、画面の理由をそのまま課長に見せ、値を直すか元に戻すかを尋ねる——自分の判断で丸めない・黙って直さない（`CLAUDE.md` → 動かすときの約束）。満足のいく設定になるまで、この編集と検査を繰り返してよい。

**受付。** その `<層の根>/input`（＝受付箱。課長が開始前に CSV を置く場所）を Read し、利用可能なファイルの**ファイル名のみ**を課長に提示する（懸念の有無が未確定のうちは中身を読まない）。ここで分岐する：

- **空だったら** — 採番も移動もせず、データを置くよう平易に案内して待つ：「分析するデータが見つかりません。`<層の根>/input` に CSV を置いてから、もう一度お知らせください。」（`<層の根>` は実値に置き換えて示す。課長はこのフォルダを手で開く）
- **複数ファイルがあったら** — 推測しない。ファイル名を並べ、どれが今回の分析対象かを課長に確認してから先へ進む。
- **1つだけなら** — そのまま採番へ。

案件名は**人間に尋ねない**（入口の質問を増やさない）。ツールが `YYMMDD_NN` 形式で機械的に採番する——同日の既存フォルダを飛ばして空いている最小の連番を取るので、フォルダ名のパースに失敗しない（＝名前が壊れていても落ちない）：

```bash
TODAY="$(date +%y%m%d)"
NN=1
while [ -e "$LAYER_DIR/projects/${TODAY}_$(printf '%02d' "$NN")" ]; do NN=$((NN + 1)); done
CASE_NAME="${TODAY}_$(printf '%02d' "$NN")"
CASE_DIR="$LAYER_DIR/projects/$CASE_NAME"
mkdir -p "$CASE_DIR/input" "$CASE_DIR/output" "$CASE_DIR/external"
mv "$LAYER_DIR/input/"* "$CASE_DIR/input/"
echo "$CASE_NAME"
echo "$CASE_DIR"
```

**この出力（`CASE_NAME` と `CASE_DIR` の実値）を案件中ずっと保持する。** 以降の全パスはこれを基準に解決する：

```bash
OUTPUT_DIR="$CASE_DIR/output"
```

移動は**コピーではない**——受付箱を空に保ち、次の案件が常にクリーンに始まるようにする。中身を読まない操作なので、機密の宣言（下記の入口の2問）より前に実行してよい。

保存先を一言で告げる（システム語彙を出さない・質問しない）：

```
今回の成果物は projects/260717_01 に保存します。
```

意味のある名前を付けたくなったら、**走行が終わったあとにフォルダをリネームすればよい**（走行後にこのフォルダを参照するツールは無い）。

計測ログをリセットし、要件整理の計測を開始する：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py reset --file "$OUTPUT_DIR/.phase_timing.jsonl"
bash "$LAYER_DIR/tools/run" phase_timer.py start --phase "requirements" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

**入口の2問。このセキュリティ判断は人間が宣言する。** AI がデータを覗いて懸念の有無を判断することはしない——覗いた時点で行データが API に乗り、「懸念がないか確かめるために覗く」こと自体が矛盾になるからだ。したがって入口で人間に明示的に尋ね、**明示の「懸念なし」が無い限り懸念あり（`security_concern=true`）として扱う**（迷ったら安全側に倒す既定）。入口の2問を確認・記録する：

1. **`security_concern`**：「このデータに個人情報・機密の懸念はありますか？」人間が宣言する。明示的に「なし」と言われない限り `true`（懸念あり）。
   - `true`（懸念あり）→ 監査＋マスキングを実施し、生データは API に出さない。
   - `false`（懸念なし）→ サブエージェント監査・マスキングはスキップ。構造プロファイルは `profile_data.py` が取得し、PM は生CSVを直接読まない。
2. **`external_data_requested`**：「外部のオープンデータ調査は必要ですか？」`false` なら外部データ調査をスキップする。

次に `agents_ICD/schemas/requirements-summary.json` の全フィールドをヒアリングで埋める：`policy_question` / `target_audience` / `data_description` / `pii_columns`（懸念ありのみ候補。確定は要件の確認。懸念なしは `[]`）/ `actionability_definition` / `constraints`。`security_concern` と `external_data_requested` も RequirementsSummary に記録する。

**データ把握（`security_concern` で分岐）。** 要件の確認の前のデータ把握は、入口で人間が宣言した `security_concern` で分岐する。

**懸念あり（`security_concern=true`・既定）— 監査を data-analyst に委譲：** **元（マスク前）CSV** のパスを JSON で `data-analyst` に渡す。**あなた（PM）は生データを読まない。**

```json
{ "request_type": "data_audit", "csv_paths": ["<案件フォルダ input/ 配下の絶対パス>"] }
```

`data-analyst` は実行を `data-scientist` に委譲し、DataAuditResult（`agents_ICD/schemas/analysis-output.json#/$defs/DataAuditResult`）を返す。監査出力は列名・dtype・分布から導いた PII 列候補を含む（生の行値は見ない）。

**懸念なし（`security_concern=false`）— PM が `profile_data.py` で構造プロファイルを取得：** 監査委譲は行わない。**あなた（PM）は生CSVを直接 Read しない。** 代わりに機械的スクリプト `profile_data.py` を `tools/run` 経由で呼び、stdout の DataAuditResult JSON（`quality_issues=[]`）を受け取る（CSV/xlsx 両対応・構造のみ出力。行値は stdout に出ない）：

```bash
bash "$LAYER_DIR/tools/run" profile_data.py --input "$CASE_DIR/input/<filename>"
```

stdout の JSON をパースして DataAuditResult（`agents_ICD/schemas/analysis-output.json#/$defs/DataAuditResult` 準拠：`n_rows`/`n_cols`/`columns`/`missing_counts`/`duplicate_rows`/`quality_issues=[]`）とデータ概要を得る。**後段のマスキングも行わない**（`pii_columns` は `[]` のまま）。監査サブエージェントは起動しない。懸念なしでもこの DataAuditResult が得られるので、要件の確認での提示・分析実行への `data_audit` 持ち回り（後述）は従来どおり機能する。`profile_data.py` は構造（shape・列名・欠損数・重複数）だけを出力するので、人間が誤って懸念なしと宣言しても生の行値が API に乗らない（宣言ではなくアーキテクチャで担保）。

どちらの分岐でも、得られた DataAuditResult を `$OUTPUT_DIR/data_audit.json` に Write で保存する。以後の工程は、この結果を依頼文に貼らず、このファイルのパスで渡す（受け手が Read で読む）。

## 2. 要件の確認（ゲート）

RequirementsSummary を作成し、データ概要（懸念ありは監査によるデータ品質レポート）と併せて課長に提示する：

```
【要件サマリー】
- 自治体名: [municipality_name]
- 政策課題: [policy_question]
- レポート対象読者: [target_audience]
- 分析で答えたい問い: [analysis_questions — 箇条書き]
- 使用データ: [filename(s)]
- 個人情報候補列（監査結果より）: [DataAuditResult.quality_issues の PII 候補]
- 行動可能性: [actionability_definition]
- 制約: [constraints]

【データ品質レポート】
[DataAuditResult を要約：行数・列数・欠損・重複・品質指摘・PII候補]
```

ゲート提示の直前に計測区間を開く：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py gate-open --gate "requirements" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

> **人間の承認が必要**
> 問い：「この要件で合っていますか？」
> - 懸念あり：「個人情報列を確認・最終確定してから先へ進みます。」確定した `pii_columns` を RequirementsSummary に記録する。
> - 懸念なし：`pii_columns=[]` のまま（PII 列の確定作業なし）。要件のみ確認する。
>
> **自由記述の欄の扱い（`security_concern=true` で、かつ監査の報告に自由記述らしき列の指摘があるときだけ）。** 指摘された列ごとに、次の文面で課長に選ばせる：
>
> ```
> 自由記述の欄（〔列名〕）が見つかりました。個人情報のマスキングは列ごとの置き換えなので、自由記述の中に書かれた名前・地名・連絡先までは消せません。この欄の扱いを選んでください。
>
> 1. 分析に使う——文章はそのまま AI に送られ、要約や分類に使われます
> 2. 分析に使わない——この欄は列ごと伏せ字にします
> ```
>
> 「分析に使わない」を選んだ列は、確定する `pii_columns` に加える（以降は既存のマスキング経路に乗る。新しい仕組みは作らない）。「分析に使う」を選んだ列は、そのままにする。どちらを選んだ場合も、列ごとの選択結果（列名と、分析に使うかどうか）を RequirementsSummary の `free_text_columns` に記録する。`security_concern=false` の案件と、監査が自由記述らしき列を指摘しなかった案件では、この確認は行わない（`free_text_columns` は書かない）。
>
> 課長が確認するまで先（外部データ調査・分析設計）に進まない。`security_concern` / `external_data_requested` / 確定した `pii_columns` を RequirementsSummary に記録する。記録を終えた RequirementsSummary を `$OUTPUT_DIR/requirements_summary.json` に Write で保存する。以後の工程は、このファイルのパスで渡す。

課長が応答した瞬間にゲート区間を閉じ、要件整理の計測を閉じる：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py gate-close --gate "requirements" --file "$OUTPUT_DIR/.phase_timing.jsonl"
bash "$LAYER_DIR/tools/run" phase_timer.py end --phase "requirements" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

## 3. 外部データ調査（`external_data_requested` が真のときだけ）

手順は `playbooks/external_data.md` の「外部データ調査」を読む。宣言が `false` なら丸ごとスキップして分析設計へ進む。

## 4. 分析設計

ラウンドカウンタ初期化の前に分析設計の計測を開く：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py start --phase "design" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

あなたは AnalysisBrief を自分で起草しない。`analysis-designer`（設計・改訂、収束を主導）と `econometrician`（方法論レビュー）の間の設計ループを仲介する。ラウンド上限は `tools/round_counter.py` が機械的に強制する——現在ラウンドの真実の源はあなたの記憶ではなく状態ファイル。計画書も同じで、本文は `$OUTPUT_DIR/analysis_brief.json` のファイルにあり、書き換えるのは `apply_brief_changes.py` だけ——あなたは直接編集しない。依頼と返答の形は `agents_ICD/design_ICD.md`。

**`round_counter` の失敗を握り潰さない。** `phase_timer` の「記録して続行・終了番号無視」は計測専用の扱いで、ここには**適用しない**。`round_counter` が失敗したら（0 以外で終わる・stdout に JSON が無い）、`.design_round_state.json` を手書きしない。ラウンド数を自分の記憶で代替しない——どちらも上限の保証を静かに無効にする。止まって、終了番号と画面の説明を課長に提示し、直ってから続ける（`CLAUDE.md` → 動かすときの約束）。

**カウンタの初期化。** 上限は `config.json` の `design_max_rounds` で定める（省略時は組み込み既定）：

```bash
bash "$LAYER_DIR/tools/run" round_counter.py init \
  --state-file "$OUTPUT_DIR/.design_round_state.json"
```

**初期設計。** `analysis-designer`（Agent）に `step: "design"` で委譲する。渡すのは、元 CSV の場所（csv_path）と出力先（output_dir）、要件のまとめとデータ検査結果のパス（外部データ調査を行った案件は、候補一覧 research_candidates.json のパスも）。初版 AnalysisBrief を受け取り、`$OUTPUT_DIR/analysis_brief.json` に Write で保存して、様式を確認する：

```bash
bash "$LAYER_DIR/tools/run" validate_icd.py \
  analysis-brief "$OUTPUT_DIR/analysis_brief.json"
```

計画書の全体が依頼と返答に載るのは、この一回だけである。以後の往復では、designer も econometrician もこのファイルを読み、改訂は変更点の一覧で受け渡す。派生・リコードが要る分析では、designer は `target_variables` に**派生後の列名**（例 `settlement_type`）を書き、その作り方を `rationale` に散文で記す（生のマスク対象列を分析変数として名指ししない）。これらの派生列は前処理で実体化される（`analysis-designer.md` → 派生列の書き方）。

**設計の改訂ループ（各ラウンドをあなたが駆動）：**

1. カウンタをインクリメントし `at_cap` を読む：

   ```bash
   bash "$LAYER_DIR/tools/run" round_counter.py increment \
     --state-file "$OUTPUT_DIR/.design_round_state.json"
   ```

   JSON 出力 `{"round": N, "max_rounds": M, "at_cap": bool}` をパースする。このラウンドの計測マーカーを打つ（N はパースした `round` 値）：

   ```bash
   bash "$LAYER_DIR/tools/run" phase_timer.py mark --label "design_round_N" --file "$OUTPUT_DIR/.phase_timing.jsonl"
   ```

2. `econometrician`（Agent）に委譲する。渡すのは、計画書・要件のまとめ・データ検査結果の3つのパスと、`is_final_round`（= `at_cap`）。2ラウンド目以降は、前ラウンドの ReviewResult と、それに designer が返した changes も付ける——econometrician は、まず前回の指摘への対応が十分かを確かめる。ReviewResult を受け取り、受け取ったまま `$OUTPUT_DIR/design_review_r{N}.json` に Write で保存する（N はこのラウンドの番号。結果検証の `verification_review_r{N}.json` と同じ扱い——走行後に設計の指摘を辿れるようにする）。

3. `analysis-designer`（Agent）に `step: "revise"` で委譲する。渡すのは、計画書のパス・今回の ReviewResult・要件のまとめとデータ検査結果のパス・`is_final_round`（= `at_cap`）。DesignDecision（verdict ＋ changes ＋ revision_notes）を受け取る——計画書の全体は返ってこない。受け取った changes を `$OUTPUT_DIR/.design_changes_r<N>.json` に Write で保存し（N はこのラウンドの番号）、計画書に反映する：

   ```bash
   bash "$LAYER_DIR/tools/run" apply_brief_changes.py \
     --brief "$OUTPUT_DIR/analysis_brief.json" \
     --changes "$OUTPUT_DIR/.design_changes_r<N>.json"
   ```

   終了番号 0＝反映した。1＝断られた——画面の理由をそのまま designer に返し、同じラウンド内で**一度だけ**書き直しを求める（カウンタは進めない）。書き直しでも断られたら、止めて理由を課長に提示する。2＝ツールが動かなかった——止めて原因を提示する（`CLAUDE.md` → 「動かすときの約束」のとおり）。changes が空のラウンドでも、この保存と実行は省かない（何も変えず、様式の確認だけが走る）。

4. **収束判定：**
   - `decision.verdict == "done"` → ループ終了。`analysis_brief.json` の現在の中身が最終。
   - `at_cap == true`（最終ラウンド完了）→ ループ終了。"done" でなくても現在の中身が最終。
   - それ以外 → 冒頭（カウンタの加算）に戻り次ラウンドへ。

5. **最終ラウンド規約：** `at_cap == true` のとき、`econometrician` と `analysis-designer` の両方に「これが最終ラウンド——これ以上のラウンドは無いので、今ラウンドで承認可能な状態に仕上げること（designer は `verdict: "done"` を返す）」と伝える。

**首長の行動可能性チェック（設計収束後・1回・ループなし）。** designer↔econometrician の往復が収束した**後**、`mayor`（Agent）を **1回** 呼び、確定 AnalysisBrief の行動可能性を評価させる。首長は「この分析結果が出たとして、行政は具体的に何ができるか」を問う役割で、**分析の正確性・統計的妥当性は判断しない**（それは econometrician の領分）。

- 入力：計画書と要件のまとめのパス（`brief_path`・`requirements_path`。首長が Read で読む。`actionability_definition` は要件のまとめの中）。受け取りは `mayor-feasibility-check.json`（`action_required` / `feasibility_assessment` / `comments` / `overall_note`）。受け取ったら、そのまま `$OUTPUT_DIR/design_review_mayor.json` に Write で保存する（設計段階の指摘を走行後に辿れるようにする。様式は変えない）。
- 計測（任意・非ブロッキング）：呼び出しを `task-start`/`task-end --task "mayor_feasibility"` で軽く挟んでよい（分析実行の task 名前空間と同じ作法。深入りしない）。
- **首長の呼び出しは round_counter の往復に数えない**（往復の外で1回コメントするだけ）。

返ってきた `action_required` で分岐する：

- `false` → そのまま前処理へ進む。
- `true` → 首長の `comments` を **`analysis-designer`（Agent, `step: "revise"`）に渡す**（改訂の依頼と同じ形で、ReviewResult の代わりに首長の `comments` を載せる。econometrician の review が無くても designer が対応を判断する）。**対応の主導権はデザイナー**（被レビュー側＝「収束の主導権は分析デザイナーが持つ」と整合）：
  - 自分で直せる → designer が変更点の一覧（DesignDecision）を返す。`$OUTPUT_DIR/.design_changes_mayor.json` に保存し、手順3と同じやり方で反映して、その計画で前処理へ進む。
  - econometrician の再チェックが要ると designer が判断 → PM は**既存 round_counter の枠で1ラウンド消費**して（カウンタの加算 → `at_cap` 確認 → econometrician review → designer revise という通常の改訂ループを1回）対応する。**首長専用の新しいカウンタ・ループは作らない。** 既に round 上限（`at_cap=true`）に達している場合は追加 review をせず、designer の判断で最新案を確定する（最終ラウンド規約と同じ）。
- 首長対応後の計画で前処理へ進む。

> **区別の要点：** 首長コメント自体は round を消費しない。designer が首長コメントを受けて econometrician 再チェックに回す場合**のみ**、それが既存 round_counter の1ラウンドを消費する（round_counter の機構は変えない＝この枠を使うだけ）。

設計ループ収束・首長チェック完了後、分析設計の計測を閉じる：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py end --phase "design" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

**手動配置の先読み案内（確定 brief の `external_data` に `manual` 型が含まれるときのみ）。** 設計が確定した時点で、手動取得が必要になるソースを人間に**予告**する。狙いは、直後の「取得」（fetch → 配置依頼）を待たずに、人間が**ダウンロードだけ先に始められる**ようにすること。配置先フォルダはこの時点ではまだ存在しない——作成するのは直後の `fetch_external.py` であり、**具体的なフォルダ案内は「取得」のテンプレで行う**（ここでは案内しない・パスを推測しない）。

```
【手動配置が必要になるソースのお知らせ（先読み）】
- 対象ソース: [manual 型の source_name を列挙]
- 取得手順の先読み: [各ソースの manual_instructions の要点を1行ずつ]
- いまダウンロードだけ先に始めていただけます。保存名は自由です（リネーム不要）。
- 配置先フォルダはこの直後にご案内します（フォルダはツールが作成します）。
```

## 5. 前処理

> **狙い：** 派生変数を生データから作る集中前処理を計画の確認の前に1回だけ行い、共有 `preprocessed_input.csv` を作る。さらに、承認しようとしている変数が前処理後に**分析に使える状態か**を `check_survival.py` で機械的に検査し、結果を計画の確認で人間に見せる。あなた（PM）は自分でマスク／前処理しない——所有は data-analyst。

前処理委譲の直前に計測を開く（非ブロッキング・exit code 無視）：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py start --phase "preprocess" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

計画書は分析設計で `$OUTPUT_DIR/analysis_brief.json` に保存済みである（`check_survival.py --brief` はこのファイルを読む）。ここでの書き出しはしない。

**この直後・`data-analyst` への前処理の委譲の前に、外部データのサブ工程を条件付きで実行する。** `brief.external_data` が**空・不在**のときはサブ工程をすべてスキップし、下記「前処理の委譲」へ直行する（`external_merges` を付けない＝完全後方互換）。`brief.external_data` が**非空**のときの取得・構造把握・列名解決は `playbooks/external_data.md` の「取得」「外部ファイルの構造把握」「外部列名の解決」を読む。

**前処理の委譲。** `request_type=preprocess` で `data-analyst` に渡す。計画書とデータ検査結果は、中身ではなくパスで渡す。`merge` のエントリがあれば「外部列名の解決」で組み立てた `external_merges` を、`reference` のエントリがあれば `external_references` を追加する。該当が無いフィールドは**付けない**：

```json
{
  "request_type": "preprocess",
  "brief_path": "<$OUTPUT_DIR/analysis_brief.json の絶対パス>",
  "data_audit_path": "<$OUTPUT_DIR/data_audit.json の絶対パス>",
  "external_merges": [ "...「外部列名の解決」の仕様（merge エントリがあるときのみ）..." ],
  "external_references": [ "...（reference エントリがあるときのみ）..." ]
}
```

`external_references` の構築詳細・DS による参照値抽出は `playbooks/external_data.md` の「参照値の抽出」を読む。

`data-analyst` は内部で（順序が derive-before-mask を保証する）：DS に派生を委譲 → `brief.pii_columns` が**非空なら** `mask_pii.py` を派生済みCSVに実行（懸念あり）／**空ならマスクをスキップ**（懸念なし）→ 最終 `preprocessed_input.csv` に `check_survival.py` を実行。返り値は `{ "preprocessed_csv_path": "<絶対パス>", "survival_result": { "overall_status": <str>, "results": [...] }, "reference_values_path": "<絶対パス・参照値を抽出したときのみ>" }`。`overall_status` は3値：`all_usable`（全変数 ok）／`needs_resolution`（`not_found` あり・潰れ無し）／`has_degenerate`（`degenerate` が1つ以上）。各 `results[*].status` も3値（`ok`／`degenerate`＝列はあるが潰れた／`not_found`＝列が照合不能）。

`external_merges` を渡した場合、`data-analyst` は derive の**後**（結合キー自体が派生列でありうるため）に同じ前処理タスク内で DS に結合を実行させる（左結合固定・`validate="m:1"`・リネーム後に結合）。以降の mask → survival → 下記「列名の解決」は既存記述のまま——外部変数は designer の規約により `variables_of_interest` に含まれるので survival の検査対象に自動的に乗り、結合できなかった行は NaN として現れて被覆率が機械測定される（DS/DA の自己申告ではなく計測値）。

前処理完了後、計測を閉じる：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py end --phase "preprocess" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

**列名の解決（`needs_resolution` のとき・規定の工程——「機転」ではない）：**

survival_result が `needs_resolution`（`not_found` あり・`degenerate` 無し）のとき、PM は以下を**規定の手順として**行う（曖昧マッチングをコードに入れない代わりに、列名解決という判断を PM が担うことを機構化する。「機械化するのは形式的性質＝完全一致のみ。列名解決は LLM に委ねる」という境界に従う）：

1. `not_found` となった各変数について、`$OUTPUT_DIR/data_audit.json` の**正確な列名**と照合し、実列名への書き換えを**変更点の一覧として自分で作る**——`field_changes` で `variables_of_interest` を、`task_changes` で各 task の `target_variables` を書き換え、`reason` には「列名の解決」と、誤った表記→実列名を書く（設計内容は変えない——列名表記のみ）。
2. 一覧を `$OUTPUT_DIR/.design_changes_columns.json` に Write で保存し、計画書に反映してから、`check_survival.py` を**再実行**する：

   ```bash
   bash "$LAYER_DIR/tools/run" apply_brief_changes.py \
     --brief "$OUTPUT_DIR/analysis_brief.json" \
     --changes "$OUTPUT_DIR/.design_changes_columns.json"
   bash "$LAYER_DIR/tools/run" check_survival.py \
     --csv "$OUTPUT_DIR/preprocessed_input.csv" \
     --brief "$OUTPUT_DIR/analysis_brief.json"
   ```

   反映を断られたら、画面の理由に従って一覧を直す（この一覧はあなた自身が書いたものである）。直せないときは止めて課長に提示する。前処理を作り直す必要は無い。計画書の変数名だけが変わるため data-analyst に再委譲せず、survival チェックのみ再走でよい——ただし派生列名そのものが変わる場合は前処理の委譲からやり直す。
3. 再実行で `all_usable`（または `not_found` の解消）を確認してから計画の確認に進む。
4. 列名解決でも残る `not_found`（実列名が本当に存在しない）や、解決の結果 `degenerate` が現れた場合は、握り潰さず計画の確認で人間に顕在化する。

これは、見つからない列を PM がその場の機転で辻褄合わせする構造を、規定された列名解決の工程に置き換えるものである（黙って質が落ちる事故を防ぐ）。

## 6. 計画の確認（ゲート）

人間（自治体の政策担当者）には「生存」「潰れた」等の内輪用語を使わず、**承認した変数が前処理を経て分析に使える状態か**という目的に即した平易な日本語で提示する。`needs_resolution` は PM が内部で列名解決を済ませる工程なので、人間には解決後の状態（通常は「すべて分析に使えます」）を提示する。

```
【分析ブリーフ】
- 政策課題: [policy_question]
- 実施する分析（優先順）:
  1. [task.name] — [task.method] — [task.rationale]
  2. [task.name] — [task.method] — [task.rationale]
- 注目する変数: [variables_of_interest]
- 個人情報列（除外）: [pii_columns]
- 設計の経緯: [何ラウンド回ったか / 最終 verdict / 主な改訂点]

【承認変数が分析に使えるかの確認（前処理後）】
- all_usable（列名解決後も含む）:
  「承認いただいた変数は、すべて分析に使える状態です。」
  （列名の食い違いを内部で解決した場合は補足程度に留めてよい——名前解決の内部処理を
    人間に見せる必要はない。）
- has_degenerate（degenerate あり——本物の問題）:
  「一部の変数は、データ上ひとつの値しかない（または回答が無い）ため、分析に使えません。」
  と平易に説明し、対象変数を列挙する：
  - [variable] — データ上ひとつの値しかない／回答が無いため分析に使えない

【外部データ】（external_data が非空のときのみ）
- [source_name]（提供: [provider]）
  - 取得: [direct_url → URL・取得日時 ／ manual → 人間が配置・検証済み（採用ファイル名）]（manifest より）
  - 出典・利用条件: [usage_terms]
  - 結合できた件数: [意図名] — [non_null_count] / [n_rows] 件
    （行結合したソースのみ。比較用の基準値として使うソースには結合件数は無い）
  - 形式の注記（manifest の format_mismatch が非 null のときのみ）:
    「想定は [expected_format] 形式でしたが、配置されたファイルは [実際の拡張子] 形式でした。
      そのまま読み込んでいます。」（情報提示のみ——**ブロックしない**）

【比較に使う参考値】（参照値を抽出したときのみ・reference_values.json から転記）
- 参考：[値の名前] [値]（[出典]・[時点]）
  例：「参考：全国の若年人口比率 12.3%（住民基本台帳・令和7年）」
- 時点や粒度が手元データとずれている場合はその場で一言添える
  例：「※ 全国値は令和2年時点です（今回の調査は令和7年）。」
- どの分析で使うかを平易に1行
  例：「この値は『年代別の定住意向』の比較基準として使います。」
- 値・出典・時点は `reference_values.json` から**機械的に転記**する（推測しない・丸めない）。
  JSON そのものやモード名（内部語彙）は人間に見せない。
- 取得できなかった・未解決のソースがある場合:
  - [source_name] — [理由：許可リスト外／ファイル未配置／フォルダ内にファイルが複数／
    列が特定できない 等]
    影響する変数: [...] ／ 影響するタスク: [target_variables に当該変数を含む task 名]
  - 選択肢: (1) ファイルを手動入手して、案内したドロップフォルダ（manifest の path）の直下に
                1つだけ置き、この工程を再実行
            (2) 外部データなしの設計に改訂（却下 → 設計の改訂ループ）
            (3) 中断
```

**外部データの提示のしかた：** external_data が非空のときのみ上記【外部データ】節を追加する（空・不在なら節ごと省く）。結合件数は survival_result（外部意図名の `non_null_count`）と PREPROCESS_JSON の `n_rows` から**機械的に転記**する（自己申告ではなく計測値）。「取得」で自動解決も縮退もしなかった取得失敗・未解決列は、ここで理由つきで顕在化する（設計は変えない——人間がゲートで再取得・ローカルのみ設計への改訂・中断を選ぶ）。manual 型の失敗理由は `manual_file_missing`（フォルダにファイルが無い）と `manual_multiple_files`（フォルダ直下にファイルが複数あり採用を推測しない）の2種——どちらも平易な日本語に置き換えて提示する。採用済み manual ファイルの `format_mismatch` が非 null のときは、上記のとおり平易な日本語で一言注記する（**情報提示のみでブロックしない**——読み込みは拡張子ではなく `external_merges.format` に従う）。

**`has_degenerate` のときは分析に使えない変数とその理由を必ず顕在化する。** 握り潰さない。これは列名解決では解消しない本物の問題である（`CLAUDE.md` → 動かすときの約束）。人間はそのまま承認しても、分析設計に差し戻してもよい（下記の「却下」→ 設計の改訂ループへ戻す経路で差し戻す）。

ゲート提示の直前に計測区間を開く：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py gate-open --gate "plan" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

> **人間の承認が必要**
> 問い：「この分析計画で進めてよいですか？（分析に使えない変数があればご確認ください）」
> 課長が確認するまで分析実行のため `data-analyst` を呼ばない。
> ▶ 承認時、ゲート区間を閉じ（下記）、分析実行の**最初の行動**として計測を開く（委譲の前に発火）。
> ▶ 却下／差し戻し時は、分析設計の「設計の改訂ループ」へ戻して brief を直し、前処理（AnalysisBrief の保存から）をやり直す。

課長が応答した瞬間にゲート区間を閉じる：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py gate-close --gate "plan" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

## 7. 分析実行

**まず**、計画の確認の承認の瞬間・あらゆる委譲の前に計測を開く：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py start --phase "execution" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

承認済みの計画で、`request_type=execute` を `data-analyst` に渡す。計画書とデータ検査結果は、中身ではなくパスで渡す——`data-analyst` は走行ごとに新しいサブエージェントで監査を記憶していないが、持ち越すのは PM ではなくファイルである。`data-analyst` は `data_audit.json` を Read で読み、AnalysisOutput の必須 `data_audit` フィールドに写す（懸念ありは監査の結果、懸念なしは `profile_data.py` の出力。どちらも要件整理で保存済み）：

```json
{
  "request_type": "execute",
  "brief_path": "<$OUTPUT_DIR/analysis_brief.json の絶対パス>",
  "data_audit_path": "<$OUTPUT_DIR/data_audit.json の絶対パス>",
  "reference_values_path": "<前処理で受け取った絶対パス・参照値があるときのみ>"
}
```

参照値を抽出した走行では、前処理で `data-analyst` が返した `reference_values_path` を**そのまま**同梱する（他の受け渡しと同じく機械的転記——自分で組み立てない）。`data-analyst` はこれを `external_refs` が非空のタスクにだけ `REFERENCE_VALUES:` マーカーで渡す（`agents_ICD/pipeline_ICD.md` → マーカー規約）。参照値が無い走行ではフィールド自体を付けない。

前処理は完了済みなので、`data-analyst` は独立分析タスクを `data-scientist` へ**並行委譲**し（各タスクは共有 `preprocessed_input.csv` を読むだけ——ここでマスクも再派生もしない。dispatch ledger により二重ディスパッチを防止。詳細は `data-analyst.md`）、AnalysisOutput を `$OUTPUT_DIR/analysis_output.json` に保存して、保存先のパスと**要点一覧**を返す——各タスクの発見（key_finding）の平易文と主張の強さ（causation_claim）、skipped_tasks、critical な指摘の有無。JSON の全体は返さない。返った瞬間——結果レビューや結果の確認の前——に計測を閉じ、ゲートの人間時間が分析実行の区間に入らないようにする：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py end --phase "execution" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

## 8. 結果検証

`data-analyst` から、保存先のパスと要点一覧を受け取る。`report-writer` に回す前に、まず機械チェックを行い、続いて計量経済の専門家による**結果検証ループ**を回す。設計時の econometrician は計画しか見ておらず、lint は語彙照合しかしない——**実行結果に統計の目が入る場面はここだけ**である。

**機械チェック：**

1. `skipped_tasks` を確認——未完タスクを課長に知らせる。
2. `has_critical_issues` を確認——true なら `severity="critical"` の `validity_notes` をすべて顕在化。課長の了承なく進まない。
3. `lint_results.py` を実行する：

   ```bash
   bash "$LAYER_DIR/tools/run" lint_results.py \
     --analysis-output "$OUTPUT_DIR/analysis_output.json"
   ```

> **結果検証の大原則：** 結果検証でできるのは「**直す・弱める・確かめる・次を示す・（札つきで）探索する**」の五つだけである。「**確証的な主張を、結果を見てから強める**」ことは何があっても行わない。統計の唯一の禁じ手（結果を見た後に主張を盛る／納得のいく数字が出るまで切り口を変え続ける）の入口にしない。プロトコルは `agents_ICD/verification_ICD.md`。

承認済みブリーフは凍結する。計画の確認で人間が承認した analysis_brief.json は事前登録の記録であり、結果検証では書き換えない。探索的追加のタスク仕様は VerificationResponse に載り、結果は AnalysisOutput に札つきで載る。

> **ペンの分担：** econometrician は指摘書のみ・designer は判断のみ（どちらもファイルに触らない）。**AnalysisOutput を書き換えるのは data-analyst だけ**（組んだ者が直す）。PM は伝言とツール実行のみ——成果物の扱いは `playbooks/pm.md` → 成果物の扱い のとおり。

**ベースライン退避（ループ開始前に必ず）。** 照合（`check_result_lock`）の比較対象を確保する。`data-analyst` が保存した `analysis_output.json` をコピーする（あなたは中身を編集しない）：

```bash
cp "$OUTPUT_DIR/analysis_output.json" "$OUTPUT_DIR/.verification_baseline.json"
```

退避直後に結果検証の計測を開く：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py start --phase "verification" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

**カウンタの初期化（結果検証専用の状態ファイル）。** 上限は `config.json` の `verification_max_rounds` で定める（省略時は組み込み既定）。`round_counter` は `--state-file` で状態ファイルを受ける——分析設計の `.design_round_state.json` を**上書きしない**：

```bash
bash "$LAYER_DIR/tools/run" round_counter.py \
  init --phase verification --state-file "$OUTPUT_DIR/.verification_round_state.json"
```

`round_counter` が 0 以外で止まったら、握り潰さない。状態ファイルを手書きしない（分析設計と同じ規律。`CLAUDE.md` → 動かすときの約束）。

**検証ループ（各ラウンドをあなたが仲介）：**

1. カウンタをインクリメントし `at_cap` を読む（`is_final_round` に反映）：

   ```bash
   bash "$LAYER_DIR/tools/run" round_counter.py \
     increment --state-file "$OUTPUT_DIR/.verification_round_state.json"
   ```

2. `econometrician`（Agent）に `step: "results_review"` で委譲する（様式は `agents_ICD/verification_ICD.md` §1）。大きな JSON はパスで渡す：`brief_path`・`analysis_output_path`・`data_audit_path`・lint の要約・`reference_values_path`（参照値があるときのみ）。ReviewResult を受け取り `$OUTPUT_DIR/verification_review_r{N}.json` に保存する。

3. **指摘なしで通過の判定：** `must_fix` がゼロかつ designer に回す提案が無ければループ終了（結果の確認へ）。

4. **前提違反の検査：** `must_fix` があるのに `fix_type` を欠くコメントが混ざっていたら、機械的な前提違反として `econometrician` に**一度だけ**再要求する（5型に乗らない強制指示は照合を素通りするため）。

5. `analysis-designer`（Agent）に `step: "respond_to_results_review"` で委譲する（様式は verification_ICD §2）：ReviewResult ＋ `analysis_output_path` ＋ `brief_path`。VerificationResponse を受け取り `$OUTPUT_DIR/verification_response_r{N}.json` に保存する。`action: "push_back"` は**記録する**（未解決 must_fix として結果の確認の併記へ）。

6. 適用が必要なら（accept された must_fix・探索的追加）`data-analyst`（Agent）に `request_type: "revise_output"` で委譲する（様式は verification_ICD §3）。`data-analyst` が再実行分を `data-scientist` に委譲し、AnalysisOutput を再組立して同じパスに保存し、パスと更新後の要点一覧を返す。

7. **`revise_output` が返るたびに照合を実行する：**

   ```bash
   bash "$LAYER_DIR/tools/run" check_result_lock.py \
     --baseline "$OUTPUT_DIR/.verification_baseline.json" \
     --current "$OUTPUT_DIR/analysis_output.json"
   ```

   exit 0＝合格。exit 1（違反）＝止める。違反行をそのまま課長に提示し、自分で結果ファイルを直して通そうとしない（`CLAUDE.md` → 動かすときの約束）。検査式そのものの誤りが示せる場合だけ、その旨を添えて課長の判断を仰ぐ。exit 2＝ツールが動かなかった。原因を提示する。

8. **収束判定：**
   - `verdict == "done"` → ループ終了。
   - `at_cap == true`（最終ラウンド完了）→ ループ終了。
   - それ以外 → 冒頭（カウンタの加算）に戻り次ラウンドへ。

**毎案件1パス**：結果の確認の承認後にこのループへ再突入しない。

**スコープ外の must_fix。** 前処理のやり直しを要する修正（派生列のバグ等）はこのループで扱わない。**未解決として結果の確認の提示に含める**——人間は却下経路で分析設計へ差し戻せる。握り潰さない。

ループ終了時、結果の確認の `gate-open` より**前**に計測を閉じる：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py end --phase "verification" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

## 9. 結果の確認（ゲート）

data-analyst の要点一覧（各タスクの発見と主張の強さ・スキップ・critical の有無。改訂があれば最新のもの）をもとに、発見を平易な日本語で要約する。加えて、**検証の結果を平易語で一行ずつ添える**（該当するものだけ。システム語彙は出さない——`fix_type` / `must_fix` / `origin` / `causation_claim` 等はハーネス内部の語であり、人間には帰結だけを示す）：

```
【専門家による結果検証】
- 指摘なし（クリーン通過時はこの一行のみ）
- 検証により表現を弱めた項目: [task名を平易に]
- 検証で見つかった誤りを修正し再実行した項目: [...]
- 頑健性の確認を行った項目: [...]（結論は維持／表現を弱めました）
- 検証の過程で追加した探索的分析: [...]
  （仮説生成として扱います。不要なら削除を指示してください）
- 専門家と設計者で見解が分かれた点: [...]（両論を併記します）
- 今回の仕組みでは対応できない指摘: [...]
  （対応するには設計からやり直しが必要です）
```

見解が分かれた点（designer の `push_back`）は、**どちらかに寄せず両論を併記**して人間に決めてもらう。

ゲート提示の直前に計測区間を開く：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py gate-open --gate "results" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

> **人間の承認が必要**
> 問い：「この結果でレポート作成に進んでよいですか？追加分析が必要であればお知らせください。」
> ▶ 承認時、ゲート区間を閉じ（下記）、レポート作成の**最初の行動**として計測を開く（ReportSpec 構築の前に発火）。

課長が応答した瞬間にゲート区間を閉じる：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py gate-close --gate "results" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

**人間が探索的追加の削除を指示した場合。** `data-analyst` に `request_type: "revise_output"` を `delete_tasks` つきで委譲し（あなたは結果ファイルを編集しない）、`check_result_lock.py` を再実行してから結果の確認を継続する。削除は人間が指示したときだけ——あなたの判断で結果を落とさない。

**次の一手の持ち越し。** `fix_type: "upgrade_path"` の項目は今回の結果を変えないが、レポート作成で ReportSpec の `next_steps` に写す（写し方は `report-spec.json` の定めのとおり）。

## 10. レポート作成

> **狙い：書き込みは `report-writer` が所有し、自己検証する。** `report-writer` は Bash を持ち、`data-scientist` が PNG/CSV を書くのと同方式で——受け取った `output_path`（絶対パス）へ、`tools/run` 経由の python で本文を書き、同じ python 内で読み戻して実在・文字数・`##` 見出し数を確認する。拒否しているのはこのプロジェクトの仕組みではなく Claude Code である——サブエージェントが `Write` ツールで作る `.md` のうち、名前が `report`・`summary`・`findings`・`analysis` で始まるものが、中身に関係なく拒否される（`report_draft.md` も `analysis_log.md` も当たる）。bash 経由 python の書き込みは拒否されない（実走行で確認済み）。**あなた（PM）は書き込みもフォールバックもしない**——下記は二重チェックの読み戻しのみ。

**まず**、結果の確認の承認の瞬間・ReportSpec 構築の前に計測を開く：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py start --phase "report" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

ReportSpec（`agents_ICD/schemas/report-spec.json`）を構築し `report-writer` に渡す：

- `analysis_output_path` — `$OUTPUT_DIR/analysis_output.json` の絶対パス（report-writer が Read で読む）
- `key_message` — 最重要の key_finding（data-analyst の要点一覧から選ぶ。不明なら課長に尋ねる）
- `municipality_name`, `target_audience` — 要件の確認より
- `output_path` — `"$OUTPUT_DIR/report_draft.md"` を**解決済みの絶対パスとして**渡す（必須。案件フォルダ名は走行ごとに変わるため `report-writer` は自力で解決できない）。`report-writer` はこの値を組み立て直さず、そのまま書き込み先に使う。
- `references` — いずれかのタスクの `rationale` が先行知見を引用しているときのみ付ける。`$OUTPUT_DIR/research_candidates.json` を Read で開き、`prior_findings` から引用された知見を一字も変えずに写す（要約しない・URL を作らない）。引用が無ければ欄ごと付けない。
- `next_steps` — 結果検証の指摘に `fix_type: "upgrade_path"` があったときのみ付ける。各指摘の target・suggestion・rationale を task_name・action・reason に写す。無ければ欄ごと付けない。

**`report-writer` が書き込みを所有する。** `report-writer` は Bash+python で `report_draft.md` を絶対パスに書き、同 python 内で読み戻し検証してから、ReportDraft JSON（`write_confirmed` は書き込み＋読み戻しが成功したら `true`）と本文の両方を返す。あなたはレポート本文を書かない——本文は下記の**読み戻し突き合わせ**に使う。

`report-writer` が返った後（あなたは書き込まない・肩代わりもしない）：

1. **`ReportDraft.write_confirmed` を確認する。** `report-writer` 自身の bash 書き込み＋読み戻しが成功していれば `true`。
2. **二重チェックで読み戻す。** 受付で解決した `OUTPUT_DIR`（＝案件フォルダの `output/`）の絶対パス（`"$OUTPUT_DIR/report_draft.md"`）を `Read` で読み、実在を確認する。
3. **突き合わせる：**
   - 読み戻した本文が `report-writer` が返した本文と整合するか、
   - `ReportDraft.output_path` が ReportSpec で渡した絶対パスと一致するか、
   - `ReportDraft.sections_present` がファイル内の `##` 見出しと整合するか、
   - `ReportDraft.word_count` が妥当か、
   - `ReportDraft.causation_claims_used` の各 task が、data-analyst の要点一覧で `causation_claim="causal_evidence"` とされているか（因果語チェック）。
4. **レポート本文の機械検査。** 読み戻したファイルに `lint_results.py` をかける（結果検証と同じ引数に `--report` を足した形）：

   ```bash
   bash "$LAYER_DIR/tools/run" lint_results.py \
     --analysis-output "$OUTPUT_DIR/analysis_output.json" \
     --report "$OUTPUT_DIR/report_draft.md"
   ```

   `--report` は本文の因果表現を照合する（`causation_claim` が `causal_evidence` でないタスクの記述に因果を含意する言い回しが混じっていないか）。警告が出たら握り潰さない——該当行をそのまま最終確認ゲートの提示に含め、直すかどうかは人間に決めてもらう。**あなた（PM）は本文を書き換えない**（直す場合の委譲先は `report-writer`）。
5. **全部 OK → 完了。** ReportDraft の `sections_missing` と `causation_claims_used` を確認してから課長に提示する。
6. **`write_confirmed` が `false` / ファイル不在 / 本文不一致 のいずれか → 最終確認で人間に顕在化する。** `report-writer` が報告したパスと理由（python の書き込み失敗・読み戻し不一致など）をそのまま見せ、`report-writer` が返した本文をインラインで提示する（内容を失わないため）。**あなたは肩代わりの書き込みをしない**——書けなかった原因を隠さず人間に渡す。

**首長レポートレビュー（書き込み検証後・最終確認の前・1回・ループなし）。** report-writer がレポートを書き、あなたが上記の書き込み検証（読み戻し）を終えた**後**、人間の最終確認の**前**に、`mayor`（Agent）を**1回**呼んで非専門家レビューを受ける。首長は「伝わるか」「行動に繋がるか」を非専門家目線で見る役割で、**分析の正確性は判断しない**。

- 入力：レポートのパス（`$OUTPUT_DIR/report_draft.md`）と要件のまとめのパス（`requirements_path`）。首長が Read で読む（`target_audience` / `actionability_definition` は要件のまとめの中）。受け取りは `mayor-review.json`（`action_required` / `verdict` / `comments` / `overall_note`）。
- 計測（任意・非ブロッキング）：`task-start`/`task-end --task "mayor_review"` で軽く挟んでよい。

返ってきた `action_required` で分岐する。**ここでは PM が首長コメントの採否を判断する**（分析設計では対応をデザイナーが判断したが、レポート作成の採否判断は PM が持つ。report-writer は清書役で、首長コメントを反映するかどうかは PM が決める）：

- `false`（approve）→ そのまま最終確認へ。
- `true`（revise）→ **PM がコメントの採否を判断する**：
  - 採用すべき指摘 → `report-writer` に「首長がこう指摘しているので該当箇所を修正せよ」と**1回だけ**修正委譲する（首長の `comments` を ReportSpec に添えて渡す）。**あなた（PM）自身はレポートを書き換えない**（`playbooks/pm.md` → 成果物の扱い）。report-writer が修正版を書いたら、上記と同じ書き込み検証（読み戻し突き合わせ）を再度行ってから最終確認へ進む。
  - 採用しない（首長の指摘が的外れ／正確性を損なう等）→ そのまま最終確認へ進み、「首長からこういう指摘があったが、〇〇の理由で反映しなかった」を人間に添えて提示する。
- **ループは作らない：** report-writer への修正委譲は最大1回。修正版に首長が再度コメントする往復はしない（最終品質は最終確認の人間確認が担保する）。

上記の検証（および首長レビュー・採用時の1回修正）後、レポート作成の計測を閉じる：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py end --phase "report" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

## 11. 最終確認（ゲート）

最終確認も人間承認区間なので挟む（課長の読了時間をゲートとして記録）。提示の直前に計測区間を開く：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py gate-open --gate "final" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

> **人間の承認が必要**
> 問い：「レポートを確認してください。問題なければ最終承認をお願いします。」

課長が応答した瞬間にゲート区間を閉じる：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py gate-close --gate "final" --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

最終承認後、計測サマリを出力し課長に提示する：

```bash
bash "$LAYER_DIR/tools/run" phase_timer.py report --file "$OUTPUT_DIR/.phase_timing.jsonl"
```

続けて、報告書を PDF と Word でも保存するかを課長に尋ねる。スキル `report-export` を、
案件名を引数にして使う（尋ね方・書き出し方・失敗したときの扱いはスキルに従う）。
報告書の中身は変わらないので、承認のゲートとしては扱わず、計測区間は開かない。

**最後に、成果物の場所を一言添えて締める**（案件の最後の行動。受付で告げた場所と同じものを、完了時にもう一度）：

```
成果物は projects/260717_01/output に保存しました。
```
