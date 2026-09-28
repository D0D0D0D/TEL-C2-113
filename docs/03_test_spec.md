# Test Specification — TEL-C2-113

## Test Strategy
- 対象: `InterconnectIncidentHandoverGraph` の outer 5-node backbone、inner workflow、3 mode adapter、S-2/S-3/S-4 境界。
- 方針: LLM / KB / DB は注入 seam の fake / in-memory 実装で置換し、テストはネットワークに出ない。
- 実行コマンド: `.venv/bin/python -m pytest tests/ -q --tb=line`

## Framework Compliance Tests

| TC-ID | Test | 実装 | Result |
|---|---|---|---|
| TC-01 | State contract: flat TypedDict / msgpack-safe | `tests/proof_of_boundary/test_state_safety.py` | PASS |
| TC-02 | `SecurityViolationError` fires on invalid output | `tests/unit/test_domain_contracts.py::test_s3_rejects_prohibited_output_key` | PASS |
| TC-03 | No JWT/Credential in State | `tests/proof_of_boundary/test_state_safety.py` + credential scan対象 | PASS |
| TC-04 | `InvocationContext` construction only via `from_state()` inside nodes | `tests/unit/test_domain_contracts.py::test_invocation_context_not_constructed_inside_nodes` | PASS |
| TC-05 | No duplicate lifecycle events in `execute()` | `tests/unit/test_domain_contracts.py::test_execute_methods_do_not_emit_lifecycle_events` | PASS |
| TC-06 | S-2 `_security_gate_input()` not overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` | PASS |
| TC-07 | S-3 `_security_gate_output()` not overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` | PASS |
| TC-08 | `required_trust_level` declared and S-1 enforced | `tests/proof_of_boundary/test_pb_invoke_order.py` | PASS |
| TC-09 | S-2 domain hook rejects unsafe State shape | `PreProcessNode._extra_security_gate_input` + `test_preprocess_state_contract_excludes_raw_free_text` | PASS |
| TC-10 | S-3 domain hook validates allowlist / enum / PII | `PostProcessNode._extra_security_gate_output` + `test_s3_rejects_prohibited_output_key` | PASS |
| TC-11 | S-4 domain event emitted in owned execute paths | PB-6 lifecycle + domain unit/integration tests | PASS |

## Domain / Adapter Tests

| ID | Test | 実装 | Result |
|---|---|---|---|
| D-01 | `/invoke` context 省略時に `config` 既定値を使う | `tests/integration/test_api_contract.py::test_startup_wires_services_and_invoke_without_context_uses_defaults` | PASS |
| D-02 | `/invoke` unknown context key は 422 | `tests/integration/test_api_contract.py::test_invoke_rejects_unknown_context_key_422` | PASS |
| D-03 | `/ingest` は INTERNAL trust 必須 | `tests/integration/test_api_contract.py::test_ingest_requires_internal_trust` | PASS |
| D-04 | `/ingest` diagnostic content は write せず rejected | `tests/integration/test_api_contract.py::test_ingest_internal_runner_and_diagnostic_reject` | PASS |
| D-05 | `/feedback` revise without text は 422 | `tests/integration/test_api_contract.py::test_feedback_rejects_revise_without_text_422` | PASS |
| D-06 | `validated_input` に自由文本文を残さない | `tests/unit/test_domain_contracts.py::test_preprocess_state_contract_excludes_raw_free_text` | PASS |
| D-07 | Stage 4: 空 quote は vacuous 照合せず当該 assertion だけ drop | `tests/unit/test_domain_contracts.py::test_stage4_drops_vacuous_assertion_quote_without_fact_only` | PASS |
| D-08 | Stage 4: grounding 不足の affirmative は `insufficient` 降格 | `tests/unit/test_domain_contracts.py::test_stage4_support_status_downgrades_without_grounding` | PASS |
| D-09 | `candidate_causes` は ID 維持・not_assessed・順位/確度なし | `tests/unit/test_domain_contracts.py::test_candidate_constraints_preserve_id_and_have_no_rank_or_confidence` | PASS |
| D-10 | `startup()` で `wire_services()` が実行される | `TestClient(app)` integration | PASS |
| D-11 | live LLM check 用 fixture が正規 invoke payload で Stage 3 へ到達 | `tests/unit/test_domain_contracts.py::test_live_llm_fixture_reaches_stage3_with_fake_llm` | PASS |
| D-12 | error envelope の `error_log` は traceback / PII を正規化してから S-3 PII 再走査 | `tests/unit/test_domain_contracts.py::test_s3_error_envelope_redacts_pii_and_traceback` | PASS |
| D-13 | Stage 3 prompt は質問 XOR 契約を指示し、実入力の literal quote を例示 | `tests/unit/test_domain_contracts.py::test_live_fixture_prompt_contract_and_literal_example` | PASS |
| D-14 | live fixture 相当の LLM 判定層は KB 空振りでも `requires_human_review: false` で通過 | `tests/unit/test_domain_contracts.py::test_live_like_llm_judgment_layer_survives_kb_empty` | PASS |
| D-15 | unlinked question は fact-only に落とさず局所 drop | `tests/unit/test_domain_contracts.py::test_invalid_unlinked_question_is_dropped_without_fact_only` | PASS |
| D-16 | 短い quote を含む LLM 出力は該当 assertion / basis_quote だけ drop し、残りの判定層を維持 | `tests/unit/test_domain_contracts.py::test_stage4_drops_only_short_quote_items_and_keeps_remaining_layer` | PASS |
| D-17 | `/ingest` / `/feedback` の永続 ledger は raw 自由文列を持たず、node 経由でも内部で `_audit` / `_safe` をサニタイズ生成する | `tests/unit/test_perspective_ledger_privacy.py` | PASS |

## Proof-of-Boundary Tests

| PB-ID | Boundary | Test | Result |
|---|---|---|---|
| PB-1 | BaseNode → EventEmitter | `tests/proof_of_boundary/test_pb_invoke_order.py` | PASS |
| PB-2 | State serialization / credential-like fields | `tests/proof_of_boundary/test_state_safety.py` | PASS |
| PB-3 | L1 template boundary → external service seam | Fake LLM / in-memory KB and DB in `tests/unit/test_domain_contracts.py` | PASS |
| PB-4 | Import isolation | `tests/proof_of_boundary/test_import_isolation.py` | PASS |
| PB-5 | Checkpoint safety | `memory_enabled: false` / `hitl.enabled: false` のため auto-waived | SKIP |
| PB-6 | Invoke execution order | `tests/proof_of_boundary/test_pb_invoke_order.py` | PASS |
| PB-7 | HITL interrupt propagation | `hitl.enabled: false`; exact-name scaffold stub remains | SKIP |

## Execution Summary

- Last local run: `455 passed, 3 skipped`
- Command: `.venv/bin/python -m pytest tests/ -q --tb=line`
- Proof boundary run: `37 passed, 3 skipped`
- Network: not used

## 実 LLM 1 回転（push 前検証）

> モックは完成品を返すため、プロンプトが構造化出力を導けるかを検証しない。push 前に実 LLM を
> 注入シームへ刺して 1 回 invoke し、**中身のある出力が出ること**を確認する（必須手順）。

| 項目 | 結果 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | ローカル CLI 経由（`BaseLLM` 実装を `config["llm"]` シームへ注入）。本番は `LazyAzureOpenAILLM`（Azure OpenAI）で、これは検証用シーム |
| 入力 | `tests/conftest.py` の `case_payload`（正規 invoke パケット・承認済みテンプレート pin + 実運用寄りの自由記述 records 3 件） |
| 結果 | `status: success` / `requires_human_review: false` |
| 判定層（LLM の成果） | `assertions` 5 件・`unresolved` 4 件・`candidate_causes` 1 件・`confirmation_questions` 5 件・`evidence_map` 5 件 — **いずれも非空** |
| 事実層（決定論） | `sources` / `timeline` / `completed_checks` / `observed_identifiers` / `event` すべて生成 |
| `degradation` | `reason_codes: ["kb_scope_unresolved"]`（KB 未配線由来・設計上の想定内）／`detail_codes: []`（quote drop なし） |
| 候補の非断定性 | `origin: inferred` / `status: not_assessed` / 根拠 2 source / 確認質問に接続。本文も「〜の可能性があります」で断定していない |

**この検証で見つけて直した欠陥**（モックでは一切露呈しなかったもの）:

1. **判定層が丸ごと落ちる（1 回目: `question_target_missing`）** — プロンプトが
   `confirmation_questions` の XOR 契約（`resolves_unresolved_id` / `resolves_candidate_id` の
   どちらか一方）を指示形で伝えていなかった。あわせて Stage 4 の適用順（質問整合は候補 drop の後）を整理。
2. **判定層が丸ごと落ちる（2 回目: `quote_too_short`）** — 同型 2 巡目だったため対処を厚くせず前提を疑い、
   設計側を 2 点変更した: ①**quote 検査の失敗は当該要素のみ drop**（判定層全滅をやめる。candidate /
   question と同じ方式に揃えた）②`min_quote_chars` を **12 → 8** に較正（実測: 記録本文 45〜65 字に対し
   正当な引用断片が 9 字前後。12 字は正当な短い技術記述を弾いていた）。
3. **`candidate_causes` が 0 件** — プロンプトのスキーマが `"candidate_causes": []` と空配列だけを示し、
   かつ候補生成の**指示形の文が無かった**（制約文のみ）。要素 1 件を含むスキーマ例と指示形の文を追加。

再発防止として prompt contract テスト（`tests/unit/test_domain_contracts.py`）で、スキーマ例が非空であること・
指示形の文が含まれることを assert している（変異検査で有効性を確認済み）。

### AGENTIC STAR Marketplace 経路 + 実 Azure OpenAI（2026-08-21）

Marketplace エントリ（`cli.py`）は HTTP エントリ（`src/api/server.py`）と別の adapter なので、
そちらで通っていることは Marketplace 経路の証明にならない。runner と同じ呼び出し形
（`invoke(message, ctx=..., input_context={"conversation_history": [...]})`）で 1 回転させた。

| 項目 | 結果 |
|---|---|
| 実施日 | 2026-08-21 |
| 使用 LLM | **Azure OpenAI `gpt-5.4-mini`**（`LazyAzureOpenAILLM` → `AzureOpenAIClient`・接続値は `ctx.secrets` 相当の束縛プロバイダから解決） |
| 入力 | `tests/conftest.py` の `case_payload` と同一の正規 invoke パケット |
| 結果 | `status: success` / `mode: invoke` / `gate_kind: handover_draft` / `requires_human_review: false` |
| 出力 | `handover_draft` の 13 キーが生成され非空（`timeline` 3 件・`completed_checks` 1 件・`observed_identifiers` 1 件 ほか） |
| `degradation` | `reason_codes: ["kb_scope_unresolved"]`（KB 未配線由来・設計上の想定内） |
| entry adapter | `mode` / `request_scope` を adapter が注入することを確認。scope 環境変数が未設定なら**起動時に fail closed** |
| 入力契約 | 素のチャット文字列（非 JSON）は従来どおり `status: error` で拒否 — 契約は緩めていない |

image 内でも `cli.py` の import・`compile()`・`config["llm"]` への `LazyAzureOpenAILLM` 注入・
5 ノード登録を確認済み（`docker run` で実行）。

#### AGENTIC STAR DEV 環境での実機 invoke（2026-08-21）

ローカル検証と同一パケットを、実際に登録した Marketplace エージェント経由で 1 回転させた。

| 項目 | 結果 |
|---|---|
| 実行環境 | AGENTIC STAR DEV（Agent ID `tel-c2-113` / image `agtstracr.azurecr.io/tel-c2-113:0.1.0`） |
| 結果 | `status: success` — `mode: invoke` / `gate_kind: handover_draft` / `requires_human_review: false` |
| 事実層 | `sources` 3 件（`char_offset_range` つき）・`timeline` 3 件・`completed_checks` 1 件（C-01）・`observed_identifiers` 1 件（GW-TKY-03 / 2 回） |
| 判定層 | `assertions` 4 件（A-03 のみ `supported`、他は `insufficient`）・`unresolved` 3 件・`candidate_causes` 2 件・`confirmation_questions` 2 件・`evidence_map` 4 件 |
| 非断定性 | 候補は 2 件とも `status: not_assessed`、`discriminating_check_id: null`、本文は「〜可能性があります」。`candidate_causes_disclaimer` は config の定数と逐語一致 |
| `degradation` | `reason_codes: ["kb_scope_unresolved"]`（KB 未配線由来・設計上の想定内） |

この実行で確認できた、ローカルでは検証不能だった 3 点:

1. Pod が起動し `cli.py`（Marketplace entry adapter）が走ること。`mode` / `request_scope` の注入が
   実環境でも成立し、`invalid_mode` / `scope_missing_or_invalid` にならないこと
2. Pod から Azure OpenAI への egress が通り、判定層が実際に生成されること
3. `get_output()` の dict が runner 側で JSON 化されてチャットに配信されること


#### 🔴 エラー系の実測 — runner が理由を捨てる（2026-08-25）

2026-08-21 の実機 invoke は**正常系のみ**だった。Marketplace 呼び出し形
（`invoke(message, ctx=..., input_context={"conversation_history": [...]})`）で
**エラー系**を実 Azure OpenAI 接続下で回した結果:

| ケース | 結果 |
|---|---|
| 起動時 — scope 環境変数 未設定 | ✅ `RuntimeError: scope 環境変数が未設定または不正: ['STG_DEFAULT_COUNTERPART_CARRIER_ID']` |
| 起動時 — `AZURE_OPENAI_DEPLOYMENT` 欠落 | ✅ `RuntimeError: Azure OpenAI 設定が不足` (`provision_secrets` で検出) |
| invoke — 素のチャット文字列（非 JSON） | `status: error` / **`output: null`** / `error_log: ["PreProcessNode: invalid_json"]` |
| invoke — JSON だが必須キー欠落 | `status: error` / **`output: null`** / `error_log` に理由 |

入力契約は正しく効いている（拒否している）。問題は**その先**にある。

**`shared/bootstrap/marketplace_app.py` は `error_log` を一度も読まない**:

```python
status = result.get("status")
if status != AgentStatus.SUCCESS.value:
    raise RuntimeError(
        f"{type(agent).__name__} invocation did not succeed: status={status!r}"
    )
```

`get_output()` が返すのは `output` / `status` / `trace_id` / `correlation_id` / `node_history` /
`error_log` だが、runner は `status` だけを見て例外にし、**`error_log` の
`PreProcessNode: invalid_json` は捨てられる**。実行は `pod_runtime.final(status="failed")` になる。

**帰結**: AGENTIC STAR のチャット欄に普通の文章を打った利用者は、
**なぜ弾かれたのか・何を入力すべきかを知る手段が無い**。この AG は JSON パケットを要求するので、
チャット UI では**これが最も起きやすい操作**である。

**実機で確定（2026-08-25 18:0x JST・AGENTIC STAR DEV / User Page）**: 予想していた
「理由が読める」「汎用エラーに丸められる」のどちらでもなく、**チャット UI が固まる**。

| 手順 | 観測 |
|---|---|
| `tel-c2-113` のチャットに `この障害の引き継ぎ資料を作って`（非 JSON の平文）を送信 | — |
| 直後 | `リクエストを理解中...` |
| 約20秒後 | `エージェント TEL-C2-113 : InterconnectIncidentHandoverAgent がタスクを実行中です。` / `Thinking...` |
| **20分以上経過後** | **`Thinking...` のまま。エラー表示なし・タイムアウト表示なし・終端イベントなし** |

`RuntimeError` のテキストは**出ない**。汎用エラーにも**丸められない**。**何も配信されない**。
入力欄は「メッセージを編集、再入力してください。」のまま送信不可（生成停止ボタンのみ）で、
利用者はこの会話を捨てて新規チャットを開く以外に復帰手段が無い。

ローカル実測では非 JSON 入力は `PreProcessNode` で即座に `invalid_json` になり LLM を呼ばないため、
この 20 分は**エージェントが計算している時間ではない**。runner が非 success で例外を投げた後、
終端イベントが配信されていないと解釈するのが自然。

**同型事象が同日に他 2 名から報告されている**（2026-08-25）:
- 11:13 — 「組み立て中に失敗した際、その理由が ChatUI に届かない。コンテナは 5 分間隔で再起動」
  → プラットフォーム側の不具合として起票
- 15:43 — 「The chat UI freezes, and we're not sure how to identify the root cause」

15:08 にプラットフォーム側から当該起票への修正回答があり、**エージェントの組み立て（コンストラクタ／
シークレットのプロビジョニング／compile）を Platform runner の callback 境界の内側で実行する**
よう修正済み・新バージョンを近日リリース、とされている。

🔴 **本件は組み立て経路ではないことを対照実験で確定させた（2026-08-25）。**

同じ Pod / 同じ日に、**正規の JSON パケット**（2026-08-21 の成功時と同一の入力）を新規チャットで送信:

| 入力 | 結果 |
|---|---|
| 非 JSON の平文（`この障害の引き継ぎ資料を作って`） | **20分以上 `Thinking...` のまま。終端イベントなし** |
| 正規 JSON パケット（8/21 と同一） | **約40秒で成功。完全な `handover_draft` が返却** |

正規パケットが成功する以上、**コンストラクタ／シークレット provisioning／compile はすべて正常**である。
したがって固まるのは **invoke 後の非 success 経路**に限られる —
`status: error` を受けて `marketplace_app.py` が `RuntimeError` を投げる箇所で、
**終端イベントが配信されない**。

**🔴 帰結（wheel 実体を読んで訂正・当初は「当該起票の修正では塞がらない」と書いていた）**:

この `raise` は **すでに `callback()` の内側にある**。同梱 wheel
（`agenticstar_agentcore-1.0.1`、`shared/bootstrap/marketplace_app.py`、
sha256 `24651705982fed2630b97dc4ac42c8cbd109d9d4f2b079616c9e5810338f1d72`）で確認:

- `callback()` は 122〜167 行。非 success の `raise RuntimeError` は **152 行目**＝内側
- 同ファイルの docstring（84〜86 行）が明記している —
  「raises `RuntimeError` when the agent's own result status is not `success`
  — **the runner converts that into the sole terminal failure event**」

つまり **契約上は終端イベントになるはずの経路**であり、当該起票が「正しく報告される側」の
対比として挙げている in-callback 経路そのものである。にもかかわらず**実機では何も届かなかった**。

したがって当該起票の修正（**組み立て**を境界の内側へ移す）は本件と**直交**する。
本件は「境界の内側なのに終端イベントが届かない」であり、切り分けは 2 通りある:

1. `COMPLETION_FAILURE` は emit されているが、**User Page が描画していない**（UI 側の欠落）
2. 例外が docstring どおりに**変換されていない**（runner 側の欠落）

**どちらかは当方では判別できない** — `agenticstar_platform` は同梱 wheel に **0 エントリ**で
（image ビルド時に PyPI 解決）、Pod ログにも到達手段が無い（下記 Admin の項）。プラットフォーム側の確認事項。

**Admin コンソールでは判別できなかった**（この切り分けに対照実験が要った理由）:
監査ログ＝管理操作のみ（ログイン・エージェント登録）／LLM監査ログ＝全社 0 件
（framework が `TelemetryAccess` を呼んでいないため。2026-08-20 の別報告と一致）／
アクセスログ＝egress ガードレールで最新が 2026-08-01／レポート＝利用者アンケート。
**実行中・失敗したエージェント実行の状態を見る画面が無い。**

**副次観測（本件とは別）**: 正規パケットの 2 回の実行で、**決定論層は完全に一致**した
（`sources` 3 件・`timeline` 3 件・`completed_checks` C-01・`observed_identifiers` GW-TKY-03 ×2）。
一方 **LLM 層は変動**した（`assertions` 4 件 → 1 件 / `candidate_causes` 2 件 → 1 件）。
決定論層の再現性は担保されており、変動は意味的構造化ステージに閉じている。

**対処しなかった理由**: `status` を `success` に変えて理由を `output` に載せれば表示はできるが、
それは**入力契約を偽ること**であり、`deploy-stg` の `agent_status_success` 判定や
レビューの非断定性検査の前提を壊す。runner 側が `error_log` を surface するのが筋なので、
テンプレ側では変更せず所見として記録する（プラットフォーム側の確認事項）。
