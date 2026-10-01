# research_ICD — 外部データ探索の受け渡し

**PM ↔ researcher**

## この文書について

この文書は、外部データ探索で PM と researcher の間を行き来する依頼と返答の形を定める。実施の条件（`external_data_requested` が true の案件だけ行う・候補ゼロでも分析は先へ進む）と手順は `playbooks/flow.md` と `playbooks/external_data.md` が、書類の形は `schemas/research-output.json` が定める。

## 1. PM → researcher：候補探索の依頼

タイミング：要件の確認の承認後・分析設計の前。

```json
{
  "requirements_path": "<OUTPUT_DIR/requirements_summary.json の絶対パス>",
  "data_audit_path": "<OUTPUT_DIR/data_audit.json の絶対パス>"
}
```

researcher はどちらも Read で読む。

## 2. researcher → PM：探索の結果

researcher は ResearchOutput（形は `schemas/research-output.json`）を返す。持ち場は三つ：

- `candidates` — 結合または参照に使える外部データの候補。各候補の8項目は様式が定める。
- `prior_findings` — データではない先行知見。1件は知見の内容・出典名・URL の三点で、URL の無い知見は載せられない（様式が弾く）。
- `note` — 候補ゼロのときの理由や、analysis-designer への所見。

適切な候補が無ければ `candidates` を空にし、`note` で理由を述べる。

## 3. 受け渡しの後

PM は ResearchOutput を `<OUTPUT_DIR>/research_candidates.json` に保存する（実データのファイルは置かない——記録するのは提案と取得手順・URL だけ）。分析設計では、PM はこのファイルのパスを analysis-designer への依頼に付ける（`design_ICD.md` の初回設計の依頼）。どれを使うかの選定は designer が行う。選定後の取得と結合は `playbooks/external_data.md` が定める。
