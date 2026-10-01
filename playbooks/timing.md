# Playbook: 計測の対応表（参照用）

計測（`phase_timer`）の対応表とチェックリスト。計測は**非ブロッキング**——呼び出しが失敗しても工程を止めず、終了番号を無視して続行する（`round_counter`・`mask_pii` の「握り潰さない」とは別扱い。計測は分析の正しさに影響しないため）。

- `start`/`end` の工程名：`requirements`, `research`, `design`, `preprocess`, `execution`, `verification`, `report`。
- **各工程は `start` と `end` の両方を打つ。** 人間ゲート直後に再開する `start` が最も漏れやすい：計画の確認の直後の `execution` start、結果の確認の直後の `report` start。ゲート承認後の**最初の行動**が次工程の `start` 発火。同様に `data-analyst` が返った瞬間（結果の確認の提示の前）に `execution` を `end` で閉じ、ゲートの人間時間が分析実行に混入しないようにする。
- 設計の改訂ループでは各ラウンド境界に `bash "$LAYER_DIR/tools/run" phase_timer.py mark --label "design_round_N" --file "$OUTPUT_DIR/.phase_timing.jsonl"` を打つ。
- 首長の呼び出しは既存の task 名前空間で軽く計測してよい（任意・非ブロッキング）：行動可能性チェックは `task-start`/`task-end --task "mayor_feasibility"`、レポートレビューは `--task "mayor_review"`。呼び出しの形は上の行と同じで、`--file "$OUTPUT_DIR/.phase_timing.jsonl"` まで揃える。深入りしない（計測は分析の正しさに影響しない）。
- **各人間ゲートを `gate-open` / `gate-close` で挟む**（人間の思考時間を区間として記録）。ゲート提示直前に `gate-open --gate "<id>"`、人間が応答した瞬間（承認に基づき行動する前）に `gate-close --gate "<id>"`。gate id：`requirements`（要件の確認）/`plan`（計画の確認）/`results`（結果の確認）/`final`（最終確認）。ゲートの内容・問い方は一切変えない（計測のみ）。
- 最終確認の後に `bash "$LAYER_DIR/tools/run" phase_timer.py report --file "$OUTPUT_DIR/.phase_timing.jsonl"` を打ち、課長に提示する。

漏れ確認用チェックリスト（合計 `start`×7 / `end`×7）：

| 工程 | `start` 発火 | `end` 発火 |
|------|------------|-----------|
| `requirements` | 要件整理（案件開始） | 要件の確認の承認後 |
| `research` | 外部データ調査（実施する場合のみ） | 外部データ調査の末尾 |
| `design` | 分析設計 | 首長の行動可能性チェック後 |
| `preprocess` | 前処理の委譲の直前 | `data-analyst` が survival_result を返した時 |
| `execution` | 分析実行 — **計画の確認の後の最初の行動** | `data-analyst` が返った時（結果の確認の前） |
| `verification` | 結果検証 — ベースライン退避の直後 | 検証ループ終了時（結果の確認の `gate-open` より前） |
| `report` | レポート作成 — **結果の確認の後の最初の行動** | `report-writer` の検証・首長レビュー完了後 |

**ゲート対チェックリスト（合計 `gate-open`×4 / `gate-close`×4 — 4ゲートすべてが対を打つ）：**

過去の走行では最後のゲートの `gate-close` しか発火せず、前半のゲートが閉じられなかった（人間の思考時間がエージェント計算に混入）。**4つのゲートはすべて `gate-open` と `gate-close` の両方を必ず打つ。** 提示の直前に `gate-open`、人間が応答した瞬間（承認に基づき行動する前）に `gate-close`。最も漏れやすいのは前半2ゲートの `gate-close`（次工程へ進む前に閉じ忘れる）——承認を受けたら次の行動の前にまず `gate-close` を打つ。

| gate id | `gate-open` 発火 | `gate-close` 発火 |
|---------|-----------------|------------------|
| `requirements` | 要件サマリー提示の直前 | 課長が応答した瞬間（`requirements` の `end` の前） |
| `plan` | 分析ブリーフ＋使用可否の提示の直前（前処理完了後） | 課長が応答した瞬間（`execution` の `start` の前） |
| `results` | 結果サマリ提示の直前 | 課長が応答した瞬間（`report` の `start` の前） |
| `final` | レポート提示の直前 | 課長が応答した瞬間（`report` 出力の前） |

`gate-close` は計測のみで、ゲートの承認フロー・問い方は一切変えない。閉じ忘れると `report` の「人間ゲート合計」「エージェント計算時間」の分離が崩れる。
