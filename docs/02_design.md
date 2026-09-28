# Template Design Specification — TEL-C2-113 InterconnectIncidentHandoverAgent

- **Template ID**: TEL-C2-113
- **Agent Class**: `InterconnectIncidentHandoverGraph`
- **Category**: Cat 2
- **Industry**: TEL
- **Source of Truth（Proposal）**: 提案段階の決定記録

> **実装状態**: 実装フェーズ で本書の契約に沿って `src/` / `tests/` / 運用文書を実装済み。
> LLM / KB / DB は注入 seam として残し、実接続は STG フェーズで配線する。
> ファイル別の現状は **`README.md` の「実装状態」節**を参照。

## Position in AgentCore Architecture

| Layer | 本テンプレとの関係 |
|---|---|
| Level 0（`agenticstar` SDK） | **import しない**（`gate-import-isolation` で機械検証） |
| **L1 Base** | **`AgentBaseGraph` を直接継承**（L1-direct）。`agents/base/` は継承しない |
| `shared/` | `shared.services.llm.base_llm.BaseLLM`（型としてのみ）・`shared.utils.audit_logger.emit_trace_event` |

**L1 Base type**: `AgentBaseGraph`（固定 5 ノード backbone）。
内側の domain workflow graph は `BaseGraph` を直接継承する（下記）。
`GraphNode` / inner graph は**継承ではなく構成（composition）**であり、継承欄には含めない。

---

## Architecture Overview

### 3 実行モード

| Mode | エントリ | 目的 | 主な書込 |
|------|---------|------|---------|
| `ingest` | `POST /ingest` | **過去ハンドオーバーの非診断的観点**（引き継ぎ時に確認すべき観点・記録の型）を構造化して KB / DB に蓄積する。**診断・復旧・クローズの結論ラベル、過去インシデントの記録本文、生成済みハンドオーバー草案そのものは蓄積しない**（検索軸・モデル入力から構造的に除外）。 | `handover_perspectives`（DB）＋その検索セーフ射影（KB） |
| `invoke` | `POST /invoke` | 対応中の単一インシデントについて、非定型記録を**承認済みハンドオーバーテンプレートの固定スキーマ**へ意味的に構造化し、実施済みチェック × 影響主張の裏付け照合と未解決/矛盾の抽出、確認質問の起草を行う。 | なし（読み取り + 草案生成） |
| `feedback` | `POST /feedback` | 人間（NOC / パートナーマネージャー）が確定した**非診断的観点**を、明示承認＋監査イベント付きで**追記のみ**（append-only）で記録する。 | `feedback_audit_events`（DB）、承認済み観点（KB） |

`mode` は `state["mode"]` で分岐し、内側 domain workflow graph が条件辺でルーティングする。
**`mode` は URL パスから adapter が決定論的に決める**（リクエストボディの自己申告は受け付けない）。

### Node Configuration（outer backbone — 固定 5 ノード）

| Node | Responsibility | Input State | Output State | Inherits |
|------|---------------|-------------|--------------|----------|
| `initialize` | セッション初期化（framework 既定・注入される） | `user_input` | `session_id`, `node_history` | `InitializeNode`（既定） |
| `pre_process` | 検証済み scope 解決（fail-closed）・入力品質ゲート・**canonical 化（PII マスク）**・テンプレ版整合・mode 検証 | `user_input`, `input_context`, ctx | `validated_input`, `mode`, `incident_ref`, `canonical_payload_ref`, `request_scope`, `input_quality`, `template_check` | `PreProcessNode(FunctionNode)` |
| `main` | `GraphNode` として内側 domain workflow graph を呼ぶ（Stage 1〜4 / mode 分岐） | `validated_input`, `mode`, `canonical_payload_ref` | `handover_draft`, `evidence_map`, `unresolved`, `requires_human_review`, `degradation`, `ingest_result`, `feedback_result`, `status` | `InterconnectHandoverGraphNode(GraphNode)` |
| `post_process` | S-3 出力検査（**出力構造 allowlist・enum 完全一致・PII / credential 再走査**のみ）・`formatted_output` 組立 | `handover_draft`, `evidence_map`, `unresolved`, `requires_human_review`, `degradation` | `formatted_output`, `status` | `PostProcessNode(FunctionNode)` |
| `finalize` | 出力確定（framework 既定・注入される） | `formatted_output` | final output | `FinalizeNode`（既定） |

`register_nodes()` は `super().register_nodes()` を**先頭で呼ぶ**（`initialize` / `finalize` の注入）。
`add_edges()` / `route()` は **override しない**（framework 既定の
`START → initialize → pre_process → main → {route} → post_process → finalize → END`、
main 後に `status` を読んで success→post / retry→pre / それ以外→finalize）。

> **`post_process` の S-3 は provenance を検証しない。** `_extra_security_gate_output(result)` は
> SDK 実機で `(self, result: dict)` シグネチャであり **`state` を受け取らない**ため、
> `source_registry` / canonical_text にアクセスできない。逐語照合・source 実在照合・`support_status`
> 降格は **Stage 4 に一本化**する（post_process が hidden state を持つ設計にしない）。

### 内側 domain workflow graph（`src/graph/domain_workflow_graph.py`）

`InterconnectHandoverGraphNode.get_subgraph()` が返す内側グラフ。

**内側グラフの基底と実装契約（SDK 1.0.1 実機確認・2026-08-20）**:
`InterconnectHandoverWorkflowGraph(BaseGraph)`。`AgentBaseGraph` は固定 5 ノード backbone を強制するため、
Stage 1〜4 + mode 分岐という独自トポロジには `BaseGraph` を直接継承する
（同型 = MunicipalContractVariationEvidenceBriefAgent `ContractVariationWorkflowGraph(BaseGraph)` / Financial Regulatory Consultation Response Evidence Brief Agent）。
`BaseGraph.__abstractmethods__` は実機で
`{name, state_schema, _validate_config, register_nodes, add_edges, route, get_output}` の **7 つ**であり、
1 つでも未実装なら `TypeError` で instantiate できない。

| 実装 | 内容 |
|---|---|
| `name` | `"interconnect_handover_workflow"` |
| `state_schema` | 内側の flat TypedDict（外側 `State` の部分集合 + Stage 中間結果） |
| `_validate_config` | 注入物（`llm` / KB・DB ハンドル / `payload_store`）と、**設計値の全キー**（`recall_top_k` / `min_quote_chars` / `max_quote_chars` / `max_question_chars` / `max_hypothesis_chars` / `max_candidate_causes` / `candidate_causes_disclaimer` / `check_action_markers` / `corroboration_window_minutes`）の存在検証。**宣言があるのにコードが読まないキーを作らない**（config が飾りになる既知の欠陥） |
| `register_nodes` | Stage 1〜4 + `IngestNode` + `FeedbackNode` を登録 |
| `add_edges` | mode 条件辺 + Stage 1→2→3→4 の直列辺 |
| `route` | mode 分岐と `status` による早期終了。**分岐関数の `state` は内側の自 schema で型注釈する**（`AgentState` 注釈だとカスタム欄が射影で消えて誤 route する） |
| `get_output` | 内側の最終 state から `handover_draft` / `evidence_map` / `unresolved` / `requires_human_review` / `degradation` / `ingest_result` / `feedback_result` / `status` を取り出す |

`InterconnectHandoverGraphNode.__init__` で 1 度だけ構築し、**`compile()` を呼んでから**保持する
（未 compile のまま返すと invoke 時に失敗する。毎 execute の新規生成はしない）。
HITL 非採用のため `compile()` に checkpointer は渡さない。

```
                          ┌─ mode=ingest ───→ [IngestNode] ──────────────────────→ END
[inner initialize] → {route by mode} ─┼─ mode=feedback → [FeedbackNode] ───────────────────→ END
                          └─ mode=invoke ──→ [Stage1 CanonicalStructureNode]
                                               → [Stage2 PerspectiveRecallNode]
                                               → [Stage3 HandoverMappingNode（LLM 核）]
                                               → [Stage4 EvidenceVerifyNode]
                                               → END
```

| Inner Node | 主要ロジック | 出力（inner state へ返すキー） |
|---|---|---|
| Stage 1 `CanonicalStructureNode` | `canonical_payload_ref` を **`execute()` 内で解決**し、決定論的に ①`source_registry` 採番 ②`completed_checks` 抽出 ③`timeline` 構築 ④`observed_identifiers` 集計 ⑤`event` の逐語選択 ⑥Stage 2 用の `structured_query` 組立を行う（③〜⑤ の規則は「事実層 3 項目の決定論規則」節）。**意味的な関係判定はしない。解決した本文（canonical_text）を state に書き戻さない**（下記 🔴） | `source_registry`, `completed_checks`, `timeline`, `observed_identifiers`, `event`, `structured_query`, `structure_quality` |
| Stage 2 `PerspectiveRecallNode` | 顧客専用接続の KB を**検証済み scope で fail-closed 検索**し、過去ハンドオーバーの**非診断的観点**のみを想起する。**検索クエリは `structured_query`（構造化ラベルのみ）を使い、記録本文の断片をクエリにしない**。scope 欠落・接続未解決は空集合ではなく `degradation` 付与 | `recalled_perspectives`, `prior_ids` |
| Stage 3 `HandoverMappingNode`（**LLM 核**） | canonical 記録を承認済みテンプレートの固定スキーマへ意味的に構造化し、各 `assertion` の `support_status` を判定、未解決・矛盾を抽出し、**原因候補を列挙し**、確認質問を起草する。**source は `source_registry` の既存ハンドルの選択のみ可**（LLM に source ID を創作させない） | `draft_candidate`, `cited_source_ids` |
| Stage 4 `EvidenceVerifyNode` | 決定論検証（下記「Stage 4 の検証順序」）。**判定層を落としても事実層は返す**（2 層出力） | `handover_draft`, `evidence_map`, `unresolved`, `requires_human_review`, `degradation` |
| `IngestNode` | 非診断的観点を品質ゲート → サニタイズ → 正規化 → 書込。KB へは**許可フィールドのみ** projection。`rejected[]` に理由コードを返す | `ingest_result` |
| `FeedbackNode` | 人間確定の観点を **append-only** で記録（`human_approved: true` + 認証済み `decided_by`）＋監査イベント | `feedback_result` |

> Stage 3 が唯一の LLM ノード。canonical 化・source 採番・逐語照合・降格判定（Stage 1 / Stage 4）は
> LLM に依存しない。**LLM に事実文字列（source ID・時刻・識別子）を創作させない**設計。

> 🔴 **Stage ノードは解決した生本文を state に書き戻さない。** `payload_ref` で out-of-state 化しても、
> Stage が `return {"validated_input": text}` のように本文を返すと State（および checkpoint が有効な
> 構成では checkpoint）に平文で載る。本文は Stage 1 の `execute()` 内で消費し、返すのは
> `source_registry` / `completed_checks` / `structured_query` などの構造化結果のみとする。
> Stage 3 は本文を **プロンプトに渡す**が、**返り値には載せない**（返るのは `draft_candidate` の
> 構造化フィールドと引用のみ）。

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

### canonical 化パイプライン（`pre_process` の責務・順序が意味を持つ）

```
1. mode 検証（URL パス由来）＋ 検証済み scope 解決（認証済み caller → counterpart_carrier_id）
2. リクエスト schema 検証（未知キー / 型違いは 422 reject・adapter 境界）
3. 入力品質ゲート（record ごと・下記しきい値）→ 落ちた record は rejected[] へ、残りで続行
4. canonical 化: 各 record の text に対し
     detect_pii(text) → mask_pii(text, findings)     ← FW 標準をそのまま適用（post-filter しない）
   結果を canonical_text とする
5. canonical_text を payload store に put → canonical_payload_ref（不透明 ref）を State へ
6. 承認済みハンドオーバーテンプレートの版整合（adapter が正本から実体を解決し hash / 承認 / 有効期間を検証）
```

> 🔴 **span オフセットの基準は canonical_text（手順 4 の後）で統一する。** マスクは文字列長を変えるため、
> マスク前のオフセットを使うと逐語照合と引用が壊れる。`source_registry` の `char_offset_range` も、
> Stage 3 が返す `quote` も、Stage 4 の照合も**すべて canonical_text 基準**。マスク前のテキストは
> `pre_process` の `execute()` 内で破棄し、どこにも保持しない。

> 🔴 **payload_ref は数字を含まない形式で発行する。** `uuid4()` を ref に使うと、その断片
> （`0269-4141-9598` など）が `detect_pii` の `phone_jp` / `credit_card` / `my_number_jp` に一致し、
> S-2 ゲートが `user_input` 中の ref を `[MASKED]` に**破壊的に書き換える**（実測 0.582%/envelope）。
> `parts:payload-store` v0.4.0 の `mint_reference()`（uuid4 の 128bit を 1 nibble → 1 文字 `a..p` に
> 符号化した 32 文字）を使う。エントロピーは 128bit のまま、数字を 1 つも含まないため、
> どの数字列パターンとも構造的に一致しえない。**S-2 の検出力は本物の PII に対して一切落ちない。**

### State Definition（`src/schemas/state.py` — `State(AgentState)`）

flat TypedDict（`AgentState` 継承）。**msgpack 可能な primitive / dict / list のみ**。
Pydantic / dataclass / `InvocationContext` は格納しない。**記録本文（自由文）を State に置かない。**

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | 実行モード（`ingest` / `invoke` / `feedback`）。**adapter が URL パスから決定**し `pre_process` が検証して返す | ✅ |
| `incident_ref` | `str` | インシデント案件の**参照 ID**（記録本文は保持しない） | ✅(invoke) |
| `request_scope` | `dict` | **認証済み caller から解決した** KB 区画キー（`counterpart_carrier_id`）。リクエストボディの自己申告は使わない | ✅ |
| `canonical_payload_ref` | `str` | canonical 化済み自由文の**不透明 ref**（`mint_reference()` 形式・letter-only） | ✅(`invoke` / `ingest` / `feedback` の `decision: revise`)。`feedback` の `confirm` / `retire` は自由文を伴わないため **空文字**（ref を発行しない） |
| `validated_input` | `dict` | S-2 通過済みの最小化入力（`incident_ref` / テンプレ版参照 / 記録の件数と種別サマリ / `business_timezone`）。**自由文本文を含まない** | ✅ |
| `template_check` | `dict` | 承認済みテンプレートの版整合結果（`{ok, template_ref, template_version, content_hash_match, mismatches[]}`） | ✅(invoke) |
| `input_quality` | `dict` | 入力品質ゲートの結果（`{ok, accepted_count, rejected: [{record_id, reason_code}]}`） | ✅ |
| `handover_draft` | `dict` | 構造化ハンドオーバー草案（closed schema・2 層構造） | ✅(invoke) |
| `evidence_map` | `list[dict]` | `claim_id → source_ids[]` の決定論マップ | ✅(invoke) |
| `unresolved` | `list[dict]` | 未解決・矛盾（`{unresolved_id, unresolved_kind, source_ids}`） | ✅(invoke) |
| `requires_human_review` | `bool` | 判定層を返せなかったとき `true` | ✅(invoke) |
| `degradation` | `dict` | 縮退エンベロープ（`{reason_codes[], detail_codes[]}`・**両方とも閉じた列挙のみ・自由文を持たない**） | ✅(invoke) |
| `ingest_result` / `feedback_result` | `dict` | 各モードの結果 | —（該当モードのみ） |

---

## Input / Output & Config Contract（executable — 実装フェーズ はこの契約に実装する）

> **実装フェーズ のテスト実装について**: 本節で名指しするテストは、**実装フェーズ で `docs/03_test_spec.md` に
> TC / PB として起票し実装する**。設計フェーズ（本フェーズ）の成果物は本書の契約定義までであり、
> `docs/03_test_spec.md` は scaffold 雛形のまま（テストフェーズ の成果物）。
>
> 🔴 **テスト入力の規約**: ゲート・サニタイザ・抽出器のテストは、**実運用で最も普通の形の入力**で書く。
> 整った形・ラベル付きの形だけで通しても、実際の記録が素通りする実装を緑のまま通してしまう
> （実例: 氏名サニタイザが「氏名: X」と「X様」しか扱えないのに、テストが敬称付きだけを使っていて
> 裸の氏名が全部素通りしていた）。本 AG では、インシデントノートは**句読点も改行も不揃いな自由記述**、
> イベント参照は**時刻表記が RFC3339 と `HH:MM` で混在**、という前提でテスト入力を作る。

### 共通の adapter 契約（3 モード共通）

| # | 論点 | 契約 |
|---|---|---|
| 1 | `mode` の決定 | **URL パス由来**。ボディの `mode` キーは未知キーとして 422 |
| 2 | 未知キー・型違い | **全モード・全階層で 422 reject**（`{"error_code": "invalid_request_key", "keys": [...]}`）。closed schema |
| 3 | scope | `counterpart_carrier_id` は**認証済み caller から解決**。ボディやヘッダの自己申告値は読まない（受け取っても破棄）。解決できなければ 403 |
| 4 | `payload_ref` / `canonical_payload_ref` | **予約キー**。サーバ発行の内部トークンであり、リクエストに現れたら **422 reject**（`error_code: reserved_key`）— #2 の未知キー規則の一部として扱い、「受け取って黙って捨てる」経路を作らない。破棄で済ませると、後段が「ref があるから再スクラブ不要」と早期 return する実装に誘導され、偽装 ref が復活する |
| 5 | 権限 | `/invoke` = `READ_INVOKE`（`VERIFIED_EXTERNAL` 以上）／`/ingest` = `WRITE_INGEST`（**`INTERNAL`**）／`/feedback` = `WRITE_FEEDBACK`（**`INTERNAL`**）。`decided_by` は認証済み `caller_id` で上書き注入 |
| 6 | 決定論性 | `business_timezone` は **adapter 境界で 1 回だけ確定**。グラフ内で `datetime.now()` / `date.today()` を呼ばない（UTC ホストでも JST ホストと同一結果） |

### Marketplace エントリ（`cli.py`）— 2 本目の adapter

`src/api/server.py`（HTTP）と並ぶ 2 本目の entry adapter。AGENTIC STAR の Marketplace runner
（`shared/bootstrap/marketplace_app.py`）は「最新のユーザーメッセージ 1 本」と
`input_context={"conversation_history": [...]}` しか渡さず、`mode` も `request_scope` も
供給しない。共通 adapter 契約（上記）を、HTTP ではなくこの経路で満たすのが `cli.py` の責務。

| 項目 | この経路での扱い | 根拠 |
|---|---|---|
| `mode` | **`invoke` 固定**。adapter が決定論的に決め、呼び出し元の値は上書きする | 共通 #1 の不変条件（自己申告を使わない）を維持 |
| `request_scope` | **deployment 環境変数から解決**（`STG_DEFAULT_SCOPE_ENVS` の宣言を再利用）。起動時に検証し、未設定・空白混じり・改行入りなら **プロセスを起動させない**（fail closed） | 下記「scope の扱いの差分」 |
| `ingest` / `feedback` | **この経路からは公開しない**。書込ノードは `INTERNAL` を要求し、runner は `VERIFIED_EXTERNAL` 固定なので二重に到達不能 | 共通 #3 / §権限 |
| `input_context` 全体 | **受け取った dict はマージせず、adapter が所有するキー（`mode` / `request_scope`）だけで組み直す**。`conversation_history` を含む runner 由来の全キーが落ちる | 下記「会話履歴を State に入れない」 |
| LLM | `config["llm"]` に `LazyAzureOpenAILLM` を注入。接続値は呼び出しごとに `ctx.secrets` 相当（`current_secrets()`）から解決し、インスタンスに保持しない | runner は `provision_secrets()` を `agent_cls()` の**後**に行うため、構築時点では認証済みクライアントを作れない |

#### 🔴 scope の扱いの差分（HTTP 経路との非対称）

HTTP 経路の `request_scope` は **認証済み caller から解決**する（共通 #3・§8 アクセス制御）。
Marketplace runner が供給する caller 情報は `USER_ID` のみで、そこから
`counterpart_carrier_id` を解決する材料が無い。よってこの経路では
**deployment ごとの固定値**に置き換えている。

**運用前提: 1 deployment = 1 相手キャリア。** 登録した Marketplace エージェントは、
環境変数で指定した 1 つの `counterpart_carrier_id` の区画としてのみ振る舞う。
複数キャリアを 1 つの登録で扱うことはできない。

**残るリスク（KB 配線後に顕在化）**: 利用者が別キャリアのインシデントを投げても、
想起される過去観点は固定区画のものになる。現状は KB 未配線で
`degradation.reason_codes: ["kb_scope_unresolved"]` が立つため実害は無いが、
**KB を配線する前に、caller → キャリアの解決経路（platform 側の検証済み scope 供給）を
用意する必要がある**。それまでは 1 キャリア 1 登録の運用で閉じる。

なお `request_scope` は State 上は `input_context` 経由で渡るが、
**呼び出し元が到達できる値ではない** — runner が `input_context` を組み立てるうえ、
adapter が無条件に上書きする。caller 申告の値を認可根拠にすることを禁じる規則が示す
「caller claim を認可根拠にする」形にはなっていない。

#### 会話履歴を State に入れない

S-2 の PII 走査対象は `("user_input", "validated_input", "llm_response")` のみで
`input_context` は含まれない。runner が渡す `conversation_history` は全会話の自由文であり、
そのまま State に載せると **PII マスクも injection 検査も受けない**。本テンプレは
「自由文を State に置かない」（本文は payload-store の ref 経由）を設計の核にしており、
履歴の消費者も存在しないため、**adapter の入口で破棄する**。

破棄は「受け取って pop する」ではなく **`input_context` を adapter が組み直す**形にする
（Esports Tournament Dispute Adjudication Support Agent と同じ形）。マージ形だと将来 runner がキーを増やしたときに素通りするが、
組み直しなら adapter が意図したキー以外は構造的に入らない。invoke 経路が
`input_context` から読むのは `mode` と `request_scope` だけで、`caller_id` /
`decided_at` は feedback 経路専用（この経路は到達しない）。caller 識別子は `ctx` 経由で
`state["caller_id"]` に入るため重複させない。

#### Azure クライアントの環境差

`AzureOpenAIClient` は **配布バンドル同梱の wheel にのみ存在**し、CI が install する
registry 版 wheel には当該モジュールが無い（`anthropic_client` / `openai_client` はある）。
そのため import は `_load_client_class()` に閉じ込め、**接続値の検査を import より先に**行う。
**経路で壊れ方が違うので分けて書く**:

- **HTTP エントリ**: 3 値の欠落 **または client 不在**を起動時に判定し `llm=None` へ縮退
  （`llm_not_configured` 経路）。CI / STG はここに入る
- **Marketplace エントリ**: `provision_secrets()` で 3 値の欠落を検出して **Pod を起動させない**。
  scope と同じく deployment 設定ミスは起動時に落とす（invoke のたびに
  `llm_call_failed` を返す状態を作らない）。client は同梱 wheel に常に存在する

### `/invoke` リクエスト契約

```jsonc
{
  "incident_ref": "INC-2026-0817-014",          // required, str, ^[A-Za-z0-9_-]{4,64}$
  "handover_template": {                        // required（省略不可）
    "ref": "HO-TMPL-INTERCONNECT",              // required, str
    "version": "3.2",                           // required, str
    "content_hash": "sha256:<hex64>"            // required, str
  },
  "records": [                                  // required, 1..max_records(200)
    {
      "record_id": "REC-001",                   // required, str, ^[A-Za-z0-9_-]{1,64}$（一意）
      "record_kind": "incident_note",           // required, enum
      "recorded_at": "2026-08-17T12:40:00+09:00", // required, RFC3339（オフセット必須）
      "text": "..."                             // required, str, 1..max_record_chars(8000)
    }
  ],
  "context": {                                  // optional（省略可・下表の既定値を adapter が注入）
    "business_timezone": "Asia/Tokyo",
    "counterpart_label": "PARTNER-A"
  }
}
```

**「省略可」と書いた以上、以下をすべて定義する**（宣言だけで契約が無い状態を作らない）:

| # | 論点 | 契約 |
|---|---|---|
| 1 | `context` 省略時の既定値 | `business_timezone` = `config.default_business_timezone`（`"Asia/Tokyo"`）／`counterpart_label` = `config.default_counterpart_label`（`"unspecified"`）。**両キーとも `validated_input` の allowlist に収載済み**であり、注入値は allowlist を通る |
| 2 | 未知キー・型違い | 共通契約 #2（422） |
| 3 | 決定論性 | 共通契約 #6 |
| 4 | 権限 | 共通契約 #5（`/invoke` は read。write なし） |
| 5 | 証明テスト（実装フェーズ） | `test_invoke_without_context_uses_declared_defaults` / `test_invoke_rejects_unknown_context_key_422` / `test_business_date_is_fixed_at_adapter_under_utc_host` / `test_client_supplied_scope_is_ignored` |

**`handover_template` は省略不可。** さらに **pin は中身ではない** — adapter が
`ref` + `version` から**正本の実体を解決**し、`content_hash` 一致・承認済み・有効期間内を検証してから
不変シール参照として渡す。**正本に存在しない `ref`/`version`（偽造 pin）は fail-closed**。
不一致・欠落時は意味的照合を行わず、**事実層のみ**を返す（`degradation.reason_codes` に
`template_version_mismatch`・`requires_human_review: true`・`status: success`）。

### `/ingest` リクエスト契約

```jsonc
{
  "perspectives": [                             // required, 1..max_records(200)
    {
      "record_id": "PSP-001",                   // required, str, ^[A-Za-z0-9_-]{1,64}$（一意）
      "perspective_kind": "missing_check",       // required, enum = unresolved_kind と同一集合
      "perspective_text": "...",                // required, str, 1..max_record_chars
      "source_ref": "HO-2026-0611-002"          // required, str（過去ハンドオーバーの参照 ID）
    }
  ],
  "context": { "business_timezone": "Asia/Tokyo" }   // optional（既定値は共通表と同じ）
}
```

| # | 論点 | 契約 |
|---|---|---|
| 1 | 既定値 | `context` は `/invoke` と同一の既定値注入 |
| 2 | 未知キー・型違い | 422（共通 #2）。`perspective_kind` の enum 外も 422 |
| 3 | 決定論性 | 共通 #6 |
| 4 | 権限 | **`INTERNAL`**（KB / DB への書込）。scope は認証済み caller 由来（共通 #3） |
| 5 | 証明テスト（実装フェーズ） | `test_ingest_requires_internal_trust` / `test_ingest_rejects_unknown_perspective_kind_422` / `test_ingest_rejects_diagnostic_conclusion_fields` / `test_ingest_row_reject_returns_reason_code` |

**蓄積してよいのは非診断的観点のみ。** `perspective_text` には診断結論（原因断定・復旧可否・
クローズ判断）を書けない — `IngestNode` が禁止キー allowlist 検査と PII 走査を通し、
違反行は `rejected[]`（`reason_code: diagnostic_content`）として**書き込まない**。
**過去インシデントの記録本文そのものは蓄積対象外**（`perspective_text` は観点の記述であって記録の写しではない）。

出力: `{"mode": "ingest", "gate_kind": "ingest_summary", "ingest_result": {"written_ids": [...], "rejected": [{"record_id", "reason_code"}]}}`

### `/feedback` リクエスト契約

```jsonc
{
  "source_record_id": "PSP-001",                // required, str（対象の観点レコード）
  "decision": "confirm",                        // required, enum: confirm | revise | retire
  "revised_perspective_text": "...",            // optional（decision=revise のときのみ許可）
  "rationale_ref": "HO-2026-0817-014",          // required, str（人間の判断の出所参照）
  "context": { "business_timezone": "Asia/Tokyo" }   // optional
}
```

| # | 論点 | 契約 |
|---|---|---|
| 1 | 既定値 | `context` は共通表と同じ。`revised_perspective_text` を**省略できるのは `decision != "revise"` のときだけ**。`decision == "revise"` で欠落は 422（既定値の注入はしない＝人間の文言を機械が代筆しない） |
| 2 | 未知キー・型違い | 422（共通 #2）。`decision` の enum 外も 422。`decision != "revise"` なのに `revised_perspective_text` があれば 422 |
| 3 | 決定論性 | 共通 #6。`decided_at` はサーバ側で `business_timezone` 基準に確定（クライアント指定不可） |
| 4 | 権限 | **`INTERNAL`**。`decided_by` は**認証済み `caller_id` で上書き注入**（自己申告を監査に使わない） |
| 5 | 証明テスト（実装フェーズ） | `test_feedback_requires_internal_trust` / `test_feedback_rejects_revise_without_text_422` / `test_feedback_decided_by_is_overwritten_by_caller_id` / `test_feedback_is_append_only` |

🔴 **`/feedback` は append-only。** 承認済みハンドオーバーテンプレート（版固定入力）への
書込権限を AG は持たない。feedback は「次版への昇格候補キュー」に積まれるだけで、
テンプレート本体の改訂は **AG の外で人間が行う**。これを守らないと「AI が引き継ぎ基準を
自動更新している」ことになり、版統治が崩れる。

出力: `{"mode": "feedback", "gate_kind": "feedback_summary", "feedback_result": {"record_id", "decision", "recorded_at", "sequence"}}`

### `/invoke` 出力 envelope（closed allowlist — 宣言外キーは S-3 が reject）

出力は **2 層**に分かれる。**事実層は常に返す。判定層は判定に必要な入力が揃ったときだけ返す。**

```jsonc
{
  "mode": "invoke",
  "gate_kind": "handover_draft",
  "incident_ref": "INC-2026-0817-014",
  "template_ref": "HO-TMPL-INTERCONNECT",
  "template_version": "3.2",

  "handover_draft": {
    // ── ① 事実層（**Stage 1 の決定論出力のみ**。LLM を経由しない。判定不能でも常に返す） ──
    "sources": [
      {"source_id": "S-001", "record_id": "REC-001", "record_kind": "incident_note",
       "recorded_at": "2026-08-17T12:40:00+09:00", "char_offset_range": [0, 184]}
    ],
    "timeline": [{"at": "2026-08-17T12:40:00+09:00", "quote": "...", "source_ids": ["S-003"]}],
    "completed_checks": [{"check_id": "C-01", "quote": "...", "source_ids": ["S-003"]}],
    "observed_identifiers": [
      {"identifier": "GW-TKY-03", "source_ids": ["S-001", "S-003"], "occurrence_count": 4}
    ],
    "counterpart_label": "PARTNER-A",              // 入力 context 由来（既定 "unspecified"）
    "event": {"opening_quote": "...", "source_ids": ["S-001"]},   // 決定論選択（下記規則）

    // ── ② 判定層（LLM の意味判断。requires_human_review=true のときは空配列） ──
    //    assertions / unresolved / contradictions / candidate_causes / confirmation_questions
    "assertions": [
      {
        "assertion_id": "A-01",
        "recorded_assertion_quote": "...",        // canonical_text からの逐語引用
        "assertion_source_ids": ["S-001"],
        "support_status": "insufficient",         // enum
        "corroborating_check_ids": [],            // completed_checks の check_id（実在検証あり）
        "evidence_source_ids": []
      }
    ],
    "unresolved": [
      {"unresolved_id": "U-01", "unresolved_kind": "unsupported_assertion", "source_ids": ["S-001"]}
    ],
    "contradictions": [
      {"contradiction_id": "X-01", "contradiction_kind": "timeline_conflict",
       "source_ids": ["S-002", "S-004"]}
    ],
    "candidate_causes": [
      {
        "candidate_id": "H-01",
        "hypothesis_text": "...",                 // LLM 生成・1 文・max_hypothesis_chars 以内
        "origin": "recorded",                     // enum: recorded | inferred
        "basis_source_ids": ["S-001", "S-003"],   // 必須・非空（根拠ゼロの候補は出せない）
        "basis_quotes": [                         // 必須・非空・basis_source_ids と**集合一致**
          {"source_id": "S-001", "quote": "..."}, // literal-copy field（逐語・長さ検査あり）
          {"source_id": "S-003", "quote": "..."}
        ],
        "discriminating_check_id": "C-02",        // optional: 既に切り分けに使える check があれば
        "resolves_by_question_id": "Q-02",        // この候補を確かめる/否定する質問（必須）
        "status": "not_assessed"                  // **固定値**（enum は 1 値のみ）
      }
    ],
    "candidate_causes_disclaimer": "...",         // config 定数の固定文言（S-3 が等値検査）
    "confirmation_questions": [
      {
        "question_id": "Q-01",
        "question_topic": "evidence_request",     // enum
        "ask_target": "counterpart_carrier",      // enum
        "question_text": "...",                   // LLM 生成・1 文・max_question_chars 以内
        "resolves_unresolved_id": "U-01",         // unresolved に実在（決定論検証）
        "resolves_candidate_id": null,            // または candidate_causes に実在。**どちらか一方が必須**
        "why_needed_code": "corroboration_missing", // 決定論導出（LLM は書かない）
        "draft_only": true                        // 固定 true（送信しない）
      }
    ],
    "assessment_complete": true                   // ②が**完全に**返せたときだけ true。①の存在では true にせず、
                                                  // 候補が 1 件でも落ちた場合も false にする
  },

  "evidence_map": [{"claim_id": "A-01", "source_ids": ["S-001"]}],
  "requires_human_review": false,
  "degradation": {"reason_codes": [], "detail_codes": []}
}
```

> 🔴 **「判定できない」と「情報を出せない」は別。** 検証に失敗しても事実層（`sources` / `timeline` /
> `completed_checks` / `observed_identifiers` / `counterpart_label` / `event`）は返す。これらは記録から決定論的に取れる
> もので、LLM の判断にも過去観点にも依存しない。判定層だけを空にして
> `requires_human_review: true` + `degradation` を立てる（`candidate_causes` も判定層なので空配列になる）。
> 利用者は事実層だけでも引き継ぎの土台に使え、
> 不足を補って再度 `/invoke` を呼べる。
> **`assessment_complete` は②からのみ導出する** — 事実層が出ていることを「確認できた」と読ませない。
>
> **縮退時の形**（判定層を返せなかったとき。上例との差分のみ示す）:
> ```jsonc
> {
>   "handover_draft": {
>     "sources": [...], "timeline": [...], "completed_checks": [...],
>     "observed_identifiers": [...], "counterpart_label": "PARTNER-A", "event": {...},
>     "assertions": [], "unresolved": [], "contradictions": [], "candidate_causes": [],
>     "confirmation_questions": [], "candidate_causes_disclaimer": "<config 定数>",
>     "assessment_complete": false
>   },
>   "evidence_map": [],
>   "requires_human_review": true,
>   "degradation": {"reason_codes": ["provenance_verification_failed"],
>                   "detail_codes": ["quote_not_verbatim"]}
> }
> ```
> 判定層の 4 配列は**キーごと消さず空配列で返す**（呼び出し元が形を変えずに読める）。

**構造的に存在しないフィールド**（作れないので断定できない・S-3 が全階層で再帰 reject）:
`root_cause` / `diagnosis` / `fault_location` / `restoration_status` / `incident_closed` /
`recommended_action` / `routing_change` / `sla_breach` / `customer_impact_confirmed` /
`partner_notification_sent` / `approval` / `verdict` /
**`most_likely_cause` / `confidence` / `probability` / `likelihood` / `rank` / `priority`**
（最後の 6 つは `candidate_causes` の導入に伴う追加 — 候補に**順位や確度を付けさせない**ため）。

**enum 定義**（`src/services/contracts.py` の単一定義を code / config / test が共有する）:

| enum | 値 |
|---|---|
| `record_kind` | `incident_note` / `event_reference` / `partner_contact_log` |
| `support_status` | `supported` / `contradicted` / `insufficient` / `unknown` |
| `unresolved_kind` | `unsupported_assertion` / `missing_check` / `ambiguous_correlation` / `missing_timestamp` / `counterpart_input_pending` |
| `contradiction_kind` | `timeline_conflict` / `scope_conflict` / `check_result_conflict` |
| `question_topic` | `evidence_request` / `timestamp_confirmation` / `scope_confirmation` / `check_result_confirmation` / `cause_discrimination` |
| `ask_target` | `counterpart_carrier` / `internal_record` |
| `cause_origin` | `recorded`（記録に書かれていた原因表明） / `inferred`（記録の観測から考えられる候補） |
| `candidate_status` | `not_assessed` の **1 値のみ**（AG がこの候補を判定していないことを構造で表明する） |
| `why_needed_code` | `corroboration_missing` / `timeline_gap` / `scope_undetermined` / `conflicting_records` / `candidate_discrimination_needed` |
| `feedback_decision` | `confirm` / `revise` / `retire` |
| `degradation.reason_codes` | `template_version_mismatch` / `input_quality_rejected` / `llm_unavailable` / `provenance_verification_failed` / `kb_scope_unresolved` |
| `degradation.detail_codes` | `quote_not_verbatim` / `quote_too_short` / `quote_too_long` / `unknown_source_id` / `unknown_check_id` / `support_status_downgraded` / `question_target_missing` / `enum_value_rejected` / `llm_output_unparsable` / `candidate_dropped` / `quote_masked_only` |
| `input_quality.rejected[].reason_code` | `record_too_long` / `record_empty` / `control_char_ratio` / `mojibake_ratio` / `unreadable_ratio` / `binary_payload` / `duplicate_record_id` |
| `ingest_result.rejected[].reason_code` | 上記 + `diagnostic_content` / `instruction_content` / `unknown_perspective_kind` / `missing_source_ref` |

### quote フィールドの契約（literal-copy field）

LLM が返す文字列のうち、**自由記述は `question_text` と `candidate_causes[].hypothesis_text` の 2 種類だけ**。それ以外の文字列フィールドは
すべて **literal-copy field**（canonical_text からの逐語コピー）であり、次の **5 フィールド**が該当する:

`event.opening_quote` / `timeline[].quote` / `completed_checks[].quote` / `assertions[].recorded_assertion_quote` /
`candidate_causes[].basis_quotes[].quote`

> このうち **`assertions[].recorded_assertion_quote` と `candidate_causes[].basis_quotes[]` が LLM の選択**による。他の 3 つは Stage 1 が
> 決定論的に切り出すので、Stage 4 の逐語検査は自明に通る（それでも同じ検査を通す — 実装の取り違えを検出するため）。

| 検査 | 規則 |
|---|---|
| 逐語性 | **マスク後の canonical_text** の部分文字列であること（`quote in canonical_text`）。オフセットも長さもすべてマスク後基準 |
| マスク痕の禁止 | `[MASKED]` **のみ**からなる quote、および `[MASKED]` を除くと `min_quote_chars` 未満になる quote は不可（`quote_masked_only`）。マスクで実質が消えた引用は根拠にならない |
| 長さ下限 | `min_quote_chars`（既定 8）以上。**空文字・1 文字は `in` が常に真で照合が vacuous になる**。`case_payload` 実測では「SIP 503 増加」「利用者影響は未確認」のような日本語の正当な技術記述が 9 文字前後になるため、下限は実測に基づき 8 に較正済み |
| 長さ上限 | `max_quote_chars`（既定 300）以下。**上限が無いと LLM が記録本文を quote 経由で丸ごと出力に写せる**（本文が出力・呼び出し元へエコーする経路）。超過は**切り詰めず** LLM 由来要素だけを `quote_too_long` で drop する（半端に切ると主張の意味が変わる） |
| 出所 | 各 field ごとに**出所 ID の参照先が異なる**ので、下表で明示する。quote はその ID が指す record の `char_offset_range` 内に位置すること |

**出所 ID の参照先（field ごと）**

| field | 出所 ID |
|---|---|
| `event.opening_quote` / `timeline[].quote` / `completed_checks[].quote` | 同じ要素の `source_ids` |
| `assertions[].recorded_assertion_quote` | 同じ要素の `assertion_source_ids` |
| `candidate_causes[].basis_quotes[].quote` | **同じ配列要素の `source_id`**（`basis_source_ids` 全体ではない） |

### Stage 4 の検証順序（決定論・この番号順に適用する）

1. **LLM 出力の構造検査** — 未知キー / enum 外 / 型違いを reject（`enum_value_rejected`）
2. **source 実在照合** — `assertion_source_ids` / `source_ids` / `evidence_source_ids` の全 ID が
   `source_registry` に実在するか（`unknown_source_id`）
3. **quote 検査** — quote 契約の **5 検査**（`quote_not_verbatim` / `quote_too_short` / `quote_too_long` /
   `quote_masked_only` / 出所 ID の範囲外）。LLM 由来の quote（`assertions[].recorded_assertion_quote` /
   `candidate_causes[].basis_quotes[].quote`）は失敗した要素だけを drop する。Stage 1 が決定論で作る
   事実層 quote（`event` / `timeline` / `completed_checks`）が失敗した場合は実装バグとして判定層を落とす
4. **check 実在照合** — `corroborating_check_ids` が `completed_checks` に実在するか（`unknown_check_id`）
5. **`support_status` の接地ゲート**（下記）— affirmative の降格
6. **候補の整合** — `candidate_causes[]` について ①`basis_source_ids` が `source_registry` に実在し**非空**
   ②`basis_quotes` が**非空**で、各要素の `source_id` が `basis_source_ids` に含まれ、`basis_source_ids` の
   **全 ID が少なくとも 1 つの `basis_quotes` 要素から参照されている**（**集合として一致**。1 source に複数 quote は可）
   ③各 `quote` がその `source_id` の record の canonical_text の逐語部分文字列で 3 の検査を通る
   ④`discriminating_check_id` が空でなければ `completed_checks` に実在
   ⑤`resolves_by_question_id` が `confirmation_questions` に実在し、その質問の `resolves_candidate_id` が
   当該候補を指す（**相互参照**）⑥`status == "not_assessed"` ⑦`candidate_id` が**一意**
   ⑧件数 ≤ `max_candidate_causes`。不成立の候補は**その候補だけを落とす**（`candidate_dropped`）— 草案全体は落とさない
7. **候補の順序を決定論で確定** — LLM の出力順を採用せず、各候補の `basis_source_ids` の**最小 `source_id`**
   （`source_registry` の採番順＝記録の時系列順）で並べ直し、同値なら `candidate_id` のコードポイント順。
   **根拠の数・`origin` を並べ替えの軸に使わない**（どちらも「もっともらしさ」のシグナルとして読まれるため）。
   🔴 **`candidate_id` は振り直さない** — LLM が付けた ID をそのまま使う（一意性は 6⑦ で検査済み）。
   振り直すと `confirmation_questions[].resolves_candidate_id` が古い ID を指す回帰が入る。
   **順序は配列の並びだけで表現し、ID には意味を持たせない**
8. **drop した候補に紐づく質問の連動** — 6 で候補を drop したら、その `candidate_id` を
   `resolves_candidate_id` に持つ `confirmation_question` も**同時に drop** する（宙に浮いた質問を残さない）。
   `unresolved` に紐づく質問は影響を受けない
9. **質問の整合**（6〜8 を終えた後に評価する）— 各 `confirmation_question` は `resolves_unresolved_id` と
   `resolves_candidate_id` の**どちらか一方だけ**を持ち（両方 null / 両方非 null は reject）、指す先が
   **6 / 8 を通過して残った** `unresolved` / `candidate_causes` に実在すること（`question_target_missing`）。
   `resolves_candidate_id` を持つ質問は `question_topic: cause_discrimination` かつ
   `why_needed_code: candidate_discrimination_needed` であること。不成立の質問は**その質問だけを落とす**。
   ただし、候補に紐づく質問が不成立の場合、その候補は S-3 の相互参照を満たせないため候補も同時に落とす
10. **`evidence_map` の決定論構築** — LLM の申告ではなく `source_registry` と検証済み claim から構築

**判定層を落とすか、一部だけ落とすか**:

| 手順 | 不成立時の扱い |
|---|---|
| 1〜2・4・10 | **判定層をすべて落とす**（事実層は返す・`requires_human_review: true`） |
| 3 | LLM 由来 quote は**当該要素のみ** drop（`quote_*` → `assessment_complete: false`）。事実層 quote の失敗は実装バグなので判定層をすべて落とす |
| 5 | 判定層は落とさず、当該 `assertion` を `insufficient` へ**降格** |
| 6 | **当該候補のみ** drop（`candidate_dropped` → `assessment_complete: false`） |
| 8 | 連動する**質問のみ** drop |
| 9 | **当該質問のみ** drop（`question_target_missing` → `assessment_complete: false`）。候補に紐づく質問が不成立なら、その候補も同時に drop |

> 🔴 **9 を 6〜8 より前に評価しない。** drop 済みの候補を指す質問が残った状態で整合を見ると
> `question_target_missing` を誤検出し、判定層ごと落ちる。

### `support_status` の決定論的降格と接地ゲート

`support_status` は LLM が選ぶ（意味的な関係判定が本 AG の核であるため）。ただし
**affirmative（`supported` / `contradicted`）は LLM の申告だけでは通さない** — LLM 供給値を
そのまま肯定側の結論に使うと、幻覚された裏付けが「裏付けあり」として素通りする。

| 条件 | 結果 |
|---|---|
| `support_status ∈ {supported, contradicted}` かつ `corroborating_check_ids` が**空** | `insufficient` へ降格 |
| `corroborating_check_ids` に `completed_checks` に**実在しない** ID が含まれる | `insufficient` へ降格（`unknown_check_id`） |
| **接地ゲート**: `support_status ∈ {supported, contradicted}` のとき、assertion と裏付け check の間に**下記 3 経路のいずれの接地も無い** | `insufficient` へ降格（`support_status_downgraded`） |

**接地の 3 経路（いずれか 1 つ成立すれば affirmative を許可する）** — 単一経路（識別子の共有のみ）に
すると、「同一記録の中で時刻と操作結果によって裏付く」「相手ログと内部ログが時刻だけ一致する」といった
**正当な裏付けまで全部 `insufficient` に落ちて核機能が痩せる**。3 経路とも決定論で判定する。

| 経路 | 成立条件 |
|---|---|
| (a) 識別子の共有 | assertion quote と check quote から抽出した**対象識別子トークン**の積が非空。抽出は `[A-Za-z0-9][A-Za-z0-9_-]{2,}` かつ英字と数字を**両方含む**トークンで、**トークン境界一致**（部分文字列一致は禁止 — `GW-TKY-03` が `GW-TKY-031` を接地してしまう） |
| (b) 同一 record 由来 | assertion の `assertion_source_ids` が指す record と、check の `source_ids` が指す record の**積が非空**（同じ記録の中で主張と実施結果が併記されている形） |
| (c) 時刻の共参照 | check の `recorded_at` が、assertion の source record の `recorded_at` を中心とする `config.corroboration_window_minutes`（既定 120）の窓に入る |

- **昇格経路は存在しない**（`insufficient` → `supported` の自動昇格を実装しない）。降格のみの片方向。
- 接地したのがどの経路かは `evidence_map` に記録せず、降格が起きた件数のみ S-4 に出す
  （経路名を出力に載せると「機械が裏付けを認定した」と読まれるため）。
- **ゲートするのは affirmative のみ。** `insufficient` / `unknown` は判断を保留する側なので、
  同じゲートを掛けると過剰ブロックになる。
- 🔴 **降格したら整合を取る** — 降格で `supported` を失った assertion については、対応する
  `unresolved` エントリ（`unresolved_kind: unsupported_assertion`・同じ `source_ids`）が
  存在することを決定論的に確認し、無ければ**決定論的に追加**する。追加もできない場合
  （`unresolved_id` を採番できない等）は判定層を落とす。降格しただけで未解決に現れない状態を作らない。

### `candidate_causes` — 原因候補の列挙（断定はしない）

**なぜ持つか（ユーザー裁定・2026-08-20）**: 原因を断定しないことと、原因の可能性を挙げないことは別。
候補が一切出ないと、受け取った担当者は「次に何を疑えばよいか」を自分でゼロから組み立てることになり、
引き継ぎ資料としての使い勝手が落ちる。**断定を防ぎつつ候補は出す**を構造で成立させる。

**主張の正確な範囲**: 構造で塞げるのは **AG が断定を「出力の形として持てないこと」**であって、
**LLM が書く一文の言い回し**ではない。`hypothesis_text` は自由文なので「原因は X である」と書くことは
できる。それを語彙 denylist で止めにいかない（言い換えで原理的に閉じず、置いた時点で網羅の期待だけが
生まれる — テンプレート標準の既定）。以下の 6 点は**断定の手段を構造から取り除く**もので、言い回しの保証ではない。

**断定の手段を取り除く 6 点**（denylist・語彙フィルタは使わない）:

| # | 制約 | 効果 |
|---|---|---|
| 1 | **`status` は `not_assessed` の 1 値固定**（enum が 1 値しかない） | `confirmed` / `likely` のような値を**作れない**。AG がこの候補を判定していないことを構造で表明する |
| 2 | **順位・確度フィールドを持たない** — `most_likely_cause` / `confidence` / `probability` / `likelihood` / `rank` / `priority` は出力 allowlist に無い。**並び順も「もっともらしさ」と無関係な軸で機械が確定する**（Stage 4 手順 7: 最初の `basis_source_id` の `source_registry` 順＝**記録に出てきた時系列順**） | 「最も可能性が高いのはこれ」を出せない。根拠の数や `origin` で並べると**それ自体が優先順位のシグナルになる**ため、並べ替えの軸から除外している |
| 3 | **`origin` で `recorded` と `inferred` を区別**（混ぜない） | 「記録にそう書いてあった」のか「AG が観測から挙げた」のかが読み手に分かる。責任の所在が混ざらない |
| 4 | **`basis_source_ids` が必須・非空 + `basis_quotes` の逐語検査** | 根拠ゼロの候補を出せない。`inferred` であっても「どの観測がこれを示唆するか」を逐語で示す |
| 5 | **`resolves_by_question_id` が必須** | すべての候補が「これを確かめる／否定するには何を確認すればよいか」に接続する。**候補を出しっぱなしにしない**のが使い勝手の本体 |
| 6 | **件数上限 `max_candidate_causes`（既定 5）＋ 免責固定文言**（config 定数・S-3 が等値検査） | 網羅を装わない。免責は LLM に書かせず config から入れる |

**受容する残余リスク（正直に記録する）**:

- `hypothesis_text` は LLM が書く自由文なので、**構造では「断定的な言い回し」を防げない**
  （「原因は X である」と書くことは可能）。これに対して禁止語 denylist は**置かない**（言い換えで
  原理的に閉じず、置いた時点で網羅の期待だけが生まれる）。緩和は上記 1・2・6 と**人間レビュー**であり、
  **保証ではない**。
- **アンカリング** — 候補を提示すること自体が担当者の判断を誘導する。順位を付けない・`origin` を分ける・
  件数を絞ることで緩和するが、消えない。導入先には「候補は確認の出発点であって推定結果ではない」ことを
  運用ガイドで明示する（申し送り #7）。
- この 2 点は **S-3 の位置づけ（製品安全の緩和策であって適合を確立しない）と同じ扱い**であり、
  最終的な妥当性は NOC / パートナーマネージャーが担保する。

**承認前提との関係（A-7）**: 提案段階の decision とそのレビューは
「corroboration 不足時は相関・原因を**推定せず** source ID 付きの `unresolved` に落とす」と書いている。
`origin: inferred` の候補はこの文言を**意図的に超える**（記録に無い可能性を AG が挙げる）。
これは 2026-08-20 のユーザー裁定による設計変更であり、**Go 前提からの乖離として明示的に記録する**。
ただし ①断定しない ②根拠必須 ③確認質問に接続 ④判定しないことを構造で表明、の 4 点で
「推定結果として提示する」形は避けている。レビューでこの点を説明できるよう本節を根拠とする。

### `degradation` / `requires_human_review` / `status` の関係（分類）

| 事象 | `status` | `requires_human_review` | `degradation` | 出力 |
|---|---|---|---|---|
| 全検証通過 | `success` | `false` | 空 | 事実層 + 判定層（`assessment_complete: true`） |
| 判定層は成立したが KB scope / backend が未解決 | `success` | `false` | `reason_codes: ["kb_scope_unresolved"]` | 事実層 + 判定層。過去観点の想起だけが欠落しているため、判定層の provenance 成立には波及させない（`assessment_complete: false`） |
| 判定層は成立したが**LLM 由来 quote が 1 件以上落ちた**（`quote_*`） | `success` | `false` | `detail_codes: ["quote_*"]` | 事実層 + 判定層。ただし **`assessment_complete: false`** |
| 判定層は成立したが**候補が 1 件以上落ちた**（`candidate_dropped`） | `success` | `false` | `detail_codes: ["candidate_dropped"]` | 事実層 + 判定層。ただし **`assessment_complete: false`**（候補の列挙が不完全なため。他の判定層は有効なので `requires_human_review` は立てない） |
| 判定層は成立したが**質問が 1 件以上落ちた**（`question_target_missing`） | `success` | `false` | `detail_codes: ["question_target_missing"]` | 事実層 + 判定層。ただし **`assessment_complete: false`** |
| 業務的な検証失敗（版不一致・provenance 不成立・LLM 未配線・入力品質で全件 reject） | **`success`** | `true` | `reason_codes` + `detail_codes` | **事実層のみ**（判定層は空配列） |
| S-3 の `SecurityViolationError`（宣言外キー・enum 外・PII/credential 残存） | **`error`** | — | — | なし（**実装バグまたは安全違反**であり、業務的縮退ではない） |
| 上流ノードの例外・内側 graph の error | `error` | — | — | なし（`error_strategy = "propagate"`） |

> 業務的な検証失敗は「AG が正常に働いた結果、判定できないと分かった」ことなので `success` で返す。
> S-3 違反は「出してはいけないものを出そうとした」ことなので `error` で止める。**両者を混同しない。**

### `GraphNode` 境界マッピング（明示・raw pass-through 禁止）

| 方向 | 実装 | 契約 |
|---|---|---|
| 親 → 内側 | `extract_input(state)` | `{"mode", "incident_ref", "canonical_payload_ref", "request_scope", "validated_input"}` の**明示 map を JSON 直列化**。**自由文本文を含めない**（inner の S-2 が inner `user_input` を再走査するため） |
| 内側 → 親 | `merge_output(state, sub_result)` | **実行されたモードのフィールドだけ**を返す（None 埋め禁止）。`node_history` は返さない（reducer 管理）。`error_log` は非空時のみ。inner の `get_output()` が top-level に返す形と、既定 `AgentBaseGraph.get_output()` が `output` にネストする形の**両方を読む** |
| エラー | `error_strategy = "propagate"` | 部分的な草案は安全な結果ではないため、内側 error は親へ伝播させて `status: error` にする |
| HITL | `propagate_hitl = False` | 本テンプレは HITL 非採用（`config.hitl.enabled: false` / `memory_enabled: false` と整合） |
| **上流 error の passthrough** | `execute()` の先頭で上流 error を検出したら `return {}` | 🔴 **`pre_process → main` は無条件辺**。`pre_process` が `status: error` を返しても `main`（`GraphNode`）は走る（`route()` は main の**後**に評価される）。入力品質ゲートや scope 解決で失敗した後に内側 graph を実行させないため、`GraphNode.execute()` を override して上流 error を素通しする（`cat2-graph-skeleton` 部品が持つ upstream error passthrough を使う） |

### outer `get_output()` の override（失敗理由を呼び出し元へ届ける）

`AgentBaseGraph.get_output()` が返すのは `output` / `status` / `trace_id` / `correlation_id` /
`node_history` の **5 キーだけ**で、**`error_log` は含まれない**（1.0.1 実機確認）。
内側 workflow の `get_output()` が `error_log` を返し `merge_output()` が outer state に渡しても、
**outer の `get_output()` で落ちて呼び出し元には `status: error` しか届かない**。
マルチエージェント構成では、どこで詰まったのかが分からない方が問題になる。

`InterconnectIncidentHandoverGraph.get_output()` を override して `error_log` を含める。ただし 2 点必須:

1. **重複排除** — `error_log` は加算チャネル（`Annotated[list[str], operator.add]`）なので、
   1 つの失敗が複数ノードを通ると同じ文字列が並ぶ。
2. **traceback の除去** — framework は未処理例外の traceback を丸ごとこのチャネルに入れる。
   そのまま返すと**ファイルパスとスタックフレームが呼び出し元へ露出する**。
   `split("\nTraceback", 1)[0]` で切る（State 側には残るのでデバッグは可能）。
   provider 例外の生メッセージも同様に、allowlist されたカテゴリコード + 例外型に正規化してから載せる。

**検証は node 単体テストでは不可能** — `agent.invoke()` の戻りに `error_log` が載ることを
end-to-end で確認する（実装フェーズ: `test_invoke_returns_error_log_without_traceback`）。

---

## Framework Utilization

### Shared Components Used

| 用途 | 使うもの | 備考 |
|---|---|---|
| ノード基底 | `framework.nodes.function_node.FunctionNode` | S-1 / S-2 / S-3 を自動で得る |
| 構成ノード | `framework.nodes.graph_node.GraphNode` | main スロット |
| PII 検出 | **`framework.security.detect_pii`** | **`framework.nodes.function_node` から引かない** — 1.0.1 で `detect_credentials` の再エクスポートが消えており、非正典の再エクスポート依存は次版で壊れる |
| PII マスク | `framework.security.pii_masking.mask_pii` | 1.0.1 実機で `framework.security` 直下には無く `pii_masking` サブモジュールにある |
| credential 検出 | `framework.security.detect_credentials_in_value` | S-3 が自動実行（1.0.1 実機で dict/list/tuple を**再帰**走査することを確認済み） |
| 監査 | `shared.utils.audit_logger.emit_trace_event` | 各テンプレ所有ノードの `execute()` で最低 1 回 |
| LLM | `config["llm"]`（`shared.services.llm.base_llm.BaseLLM` 実装）を注入 | **モデル ID・具体クライアントをテンプレに書かない** |

### S-2 設計 — canonical 化と PII（SDK 1.0.1 実測に基づく設計判断）

`FunctionNode._security_gate_input()`（`@final`）は `user_input` / `validated_input` / `llm_response` の
**`str` 値のみ**を `detect_pii` → `mask_pii` する（1.0.1 実機確認・`dict` 値はスキップされる）。
`InitializeNode` 自身も `FunctionNode` なので、**`user_input` は `pre_process` に届く前にマスク済み**。

**実測した問題（2026-08-20・SDK 1.0.1）**: `detect_pii` の `name` パターンは
「連続する 2 語以上の Title Case」に一致する高再現・低精度のヒューリスティックであり、
相互接続インシデント記録の**技術語彙を PII として誤検出してマスクする**。

| 入力 | `detect_pii` の判定 | マスク後 |
|---|---|---|
| `Session Border Controller SBC-OSA-02 で SIP 503 が増加` | `name: "Session Border Controller"` | `[MASKED] SBC-OSA-02 で SIP 503 が増加` |
| `Route Reflector を切り戻し` | `name: "Route Reflector"` | `[MASKED] を切り戻し` |

SDK の `detect_pii` docstring 自身は
*"Callers processing structured text should post-filter `type == "name"` results against a known-safe allowlist"*
と呼び出し側の post-filter を勧めているが、**本テンプレートは post-filter を実装しない**（下記「PII の責務分界」）。

**設計（3 点セット）:**

1. **記録本文を `user_input` / `validated_input`（str）に載せない** — adapter が受信直後に
   payload store へ預け、State には `mint_reference()` 形式の不透明 ref のみを置く。
   `user_input` には PII を含まない制御情報（`incident_ref`・mode・件数）のみが載る。
   （これは PII 対策ではなく **State / checkpoint への生本文の混入を防ぐ**ための措置。§データ統治の層 1）
2. **canonical 化は FW 標準をそのまま適用する** — `pre_process.execute()` 内で
   `detect_pii()` → `mask_pii()` を呼び、結果を canonical_text とする。**findings の除外・追加は一切しない。**
   `name` / `name_jp` / `email` / `phone_jp` / `phone_us` / `ssn` / `my_number_jp` / `credit_card` の
   すべてがマスク対象。
3. **`_extra_security_gate_input()` で「生本文が State に載っていないこと」を検査する** —
   `validated_input` に自由文キーが現れたら `SecurityViolationError`（fail-closed）。
   これは S-2 の**代替ではなく**、S-2 が構造的に届かない経路（payload store）に対する同等制御の追加。

#### PII の責務分界（ユーザー裁定・2026-08-20）

> **PII 検出の精度は framework の責務であり、テンプレート側では補正しない。**
> 検出漏れ（日本語のラベル無し氏名・全角の連絡先）も、**誤検出**（下記）も、どちらも
> テンプレートローカルの辞書や allowlist で埋めない。テンプレート標準の既定「FW 標準 `detect_pii` のみ・
> 自前補完サニタイザを実装しない」と同じ責務境界を、**誤検出側にも適用する**。

**受容する既知の限界（実測・SDK 1.0.1）:**

| 限界 | 実測 | 影響 |
|---|---|---|
| **誤検出**: `name` は「連続 2 語以上の Title Case」に一致する | `Session Border Controller` / `Route Reflector` が `[MASKED]` になる | 英語の装置種別名・プロトコル名を含む記録では、その語が canonical_text から失われる。**識別子は影響を受けない**（`GW-TKY-03` / `SBC-OSA-02` / `SIP 503` / `INC-2026-0817-014` は数字を含むため非検出＝実測）。裏付け照合の主軸は**識別子・時刻・実施内容マーカー**であり、これらは残る |
| **検出漏れ**: 日本語のラベル無し氏名・敬称のみの氏名 | `name_jp` は 氏名/名前/担当者 + 区切り の直後のみ発火 | 記録側の非識別化運用に依存する（申し送り #1） |
| **検出漏れ**: ASCII 前提 | 全角の email / 電話は素通り | 同上 |

これらは**フレームワーク側の課題として `request:coe` 起票候補に記録**し、テンプレートでは回避策を実装しない
（サイレントなワークアラウンドは生産ラインを腐らせる）。導入先には「英語の装置種別名がマスクされうる」ことを
運用ガイドで明示する（申し送り #3）。

### S-3 設計方針 — allowlist 一本化（denylist を最初から持たない）

Travel Supplier Sustainability Claim Evidence Validator の到達点に従い、**禁止語 denylist は実装しない**（事後撤去ではなく最初から作らない）。
自然言語の断定は言い換えで原理的に閉じず、置いた時点で「網羅している」という誤った期待が生まれるため。

`_extra_security_gate_output(result)` が行う検査（**`state` を受け取れないため、分岐に必要な値は
`execute()` が `formatted_output["mode"]` / `["gate_kind"]` に宣言する**。1.0.1 実機でシグネチャ確認済み）:

| # | 検査 | 失敗時 |
|---|---|---|
| 1 | **出力構造 allowlist** — `formatted_output` の全階層で宣言外キーを reject（禁止フィールド一覧は上記） | `SecurityViolationError` → `status: error` |
| 2 | **enum 値の完全一致** — `support_status` / `*_kind` / `question_topic` / `ask_target` / `why_needed_code` / `degradation.reason_codes` / **`degradation.detail_codes`** / `*.reason_code` | 同上 |
| 3 | **`draft_only` 固定検査** — `confirmation_questions[].draft_only` が全件 `true` | 同上 |
| 4 | **文字列長の再検査** — `question_text` ≤ `max_question_chars`・`hypothesis_text` ≤ `max_hypothesis_chars`・いずれも改行なし／literal-copy field ≤ `max_quote_chars`（**上限のみ**。下限・マスク痕・逐語性は canonical_text が要るので Stage 4 の専管） | 同上 |
| 4-b | **`candidate_causes` の構造検査（Stage 4 済みを前提とした最終形の再検査）** — `status` が `not_assessed` 以外／`basis_source_ids` が空／`basis_quotes` が空／件数 > `max_candidate_causes`／`candidate_causes_disclaimer` が config 定数と**完全一致しない**、のいずれかで reject。加えて **`formatted_output` 内で完結する相互参照**を再検査する: `basis_source_ids` ⊆ `sources[].source_id`／**`basis_quotes[].source_id` ⊆ `sources[].source_id`**／**`set(basis_quotes[].source_id) == set(basis_source_ids)`**／`resolves_by_question_id` ∈ `confirmation_questions[].question_id`／`discriminating_check_id` ∈ `completed_checks[].check_id`／**`candidate_id` の一意性**。質問側も同様に **`resolves_candidate_id` ∈ `candidate_causes[].candidate_id`**／**`resolves_unresolved_id` と `resolves_candidate_id` の XOR**／`resolves_candidate_id` を持つ質問は `question_topic: cause_discrimination` かつ `why_needed_code: candidate_discrimination_needed`。**canonical_text を要する逐語照合（逐語性・長さ・マスク痕・出所範囲）は S-3 では行えない**（`state` を持たないため）ので **Stage 4 手順 3 / 6 の専管**とする | 同上 |
| 5 | **PII 再走査** — `formatted_output` の全 string leaf を `detect_pii` にかけ、**種別を問わず finding が 1 件でも残っていたら reject**（allowlist による例外を設けない）。framework の S-3 は credential のみで **PII は走らない**ため、テンプレートが再走査する。canonical_text 由来の quote は既にマスク済みなので、ここで発火するのは LLM が**自由記述フィールド（`question_text` / `candidate_causes[].hypothesis_text`）に書いた場合**だけ | 同上 |
| 6 | **credential 残存** — framework 既定（`detect_credentials_in_value`・再帰）が `_extra_*` の**前**に走る。追加実装はしない | framework が `RuntimeError` |
| 7 | **error envelope の `error_log` 正規化** — `gate_kind: error` のときも早期 return せず、`error_log` から traceback 以降を除去し、絶対パス・`File "` 断片・PII を含む entry は固定カテゴリ `error_log_redacted` に正規化する。そのうえで `formatted_output` 全 string leaf の PII 再走査を行う。これは outer `get_output()` の traceback 除去契約と同じ防御であり、error 経路だけ PII / パスが再露出することを防ぐ | `SecurityViolationError` → `status: error` |

**S-3 は provenance を検証しない**（`state` を持たないため構造的に不可能）。逐語照合・source 実在・
`support_status` 降格は Stage 4 の責務。両者を混ぜると、実装者が hidden field を state 経由で
持ち込む fail-open 設計に逃げる。

**位置づけ**: S-3 は製品安全の緩和策であり、法令遵守や相互接続協定上の適合を確立するものではない。
最終的な引き継ぎ内容の妥当性は人間（NOC / パートナーマネージャー）が担保する。

### S-4 監査（成功経路だけに置かない）

`emit_trace_event(event_type, payload, state)` をテンプレ所有の全ノードの `execute()` で最低 1 回呼ぶ。
`node_start` / `node_complete` / `node_error` は framework が `__call__()` で自動発火するため**重複させない**。
ドメインイベントは **成功経路以外にも置く**:

| イベント | 発火点 |
|---|---|
| `records_canonicalized` | pre_process（マスク件数・reject 件数。**マスクした値そのものは payload に載せない**） |
| `input_quality_rejected` | 品質ゲートで reject した記録がある場合 |
| `template_version_mismatch` | 版整合 NG（偽造 pin を含む） |
| `perspective_recall_degraded` | KB scope 未解決・空集合 |
| `handover_draft_generated` | Stage 3 成功 |
| `provenance_verification_failed` | Stage 4 が判定層を落とした |
| `support_status_downgraded` | 降格が発生した件数（接地ゲート起因を区別） |
| `candidate_causes_emitted` | 候補の件数を `origin` 別に（`recorded` / `inferred`）。**本文は載せない** |
| `candidate_dropped` | 検証で落ちた候補の件数と理由コード |
| `s3_output_rejected` | post_process の allowlist reject |
| `ingest_rows_rejected` / `feedback_recorded` | ingest / feedback |

### Composition Pattern

```
InterconnectIncidentHandoverGraph(AgentBaseGraph)      ← L1 direct（継承）
  ├── PreProcessNode(FunctionNode)
  ├── InterconnectHandoverGraphNode(GraphNode)         ← 構成（composition）
  │      └── InterconnectHandoverWorkflowGraph(BaseGraph)   ← 構成
  │             └── Stage1..4 / IngestNode / FeedbackNode (FunctionNode)
  └── PostProcessNode(FunctionNode)
```

---

## Stage 1 の決定論規則（実装フェーズ が一意に実装できる粒度で定義する）

### `source_registry` の採番

1. 品質ゲートを通過した records を **`(recorded_at, record_id)` の stable sort** で並べる
   （`recorded_at` は RFC3339 をパースした aware datetime で比較。同値なら `record_id` の
   コードポイント順）。
2. 先頭から `S-001`, `S-002`, … と **`S-%03d`** で採番する（1000 件以上は `max_records` で構造的に発生しない）。
3. `char_offset_range` は **canonical_text（PII マスク後）に対する Python `str` のコードポイント
   half-open 区間 `[start, end)`**。基準は **record 単位**（record ごとの canonical_text の先頭が 0）。
   結合テキストのオフセットではない。
4. 同一 `record_id` が 2 件以上あれば全件 reject（`duplicate_record_id`）— 採番の一意性を先に保証する。

### `completed_checks` の抽出条件

`record_kind == "event_reference"` の record のうち、**同一 record 内に次の 3 要素がすべて揃うもの**だけを
check 化する。1 つでも欠ければ check にせず、`structure_quality.missing_elements` に理由コードを積む。

| 要素 | 判定 |
|---|---|
| 時刻 | RFC3339 の日時、または `HH:MM` + タイムゾーン略号（`business_timezone` で解決） |
| 対象識別子 | `[A-Za-z0-9][A-Za-z0-9_-]{2,}` にマッチし、かつ数字と英字を**両方**含むトークン（`GW-TKY-03` / `TG-INT-001` は該当、`SIP` は非該当） |
| 実施内容 | `config.check_action_markers`（`config/config.yaml` に実値で存在）のいずれかを含む |

`check_id` は該当 record を `(recorded_at, record_id)` 順に並べて `C-%02d`。

### 事実層 3 項目の決定論規則

事実層は **Stage 1 の決定論出力のみ**で構成し、LLM を一切経由しない。以下の 3 項目もその規則で作る。

| 項目 | 規則 |
|---|---|
| `timeline[]` | 全 record を `(recorded_at, record_id)` 順に並べ、各 record の canonical_text の**先頭 1 文**（最初の句点 `。` / `.` / 改行まで。無ければ `max_quote_chars` で切る）を `quote` として `{at, quote, source_ids}` を作る。**要約しない**（逐語のみ）。`min_quote_chars` 未満の record はその要素を出さず `structure_quality.short_records` に記録する |
| `observed_identifiers[]` | 全 canonical_text から `completed_checks` と**同じ対象識別子トークン規則**（`[A-Za-z0-9][A-Za-z0-9_-]{2,}` かつ英字と数字を両方含む）で抽出し、`identifier` ごとに出現した `source_ids` と `occurrence_count` を集計。`occurrence_count` の降順、同数なら `identifier` のコードポイント順。**どの識別子が「サービス」か「装置」かは判定しない**（分類は意味判断であり事実層に置かない） |
| `event.opening_quote` | `record_kind == "incident_note"` のうち **`recorded_at` が最も早いもの**（同値なら `record_id` 順）の canonical_text の**先頭 1 文**。該当 record が無い場合、および取り出した先頭 1 文が **`min_quote_chars` 未満または `[MASKED]` 除去後に `min_quote_chars` 未満**の場合は `event` を空 dict にし `structure_quality.no_usable_incident_note` を立てる（`timeline[]` の短文処理と同じ扱い）。**要約でも生成でもなく逐語選択**（だから `summary_quote` ではなく `opening_quote` と呼ぶ） |

> `service_label` は置かない。「どれがサービス名か」の判定は意味判断であり、事実層に置くと
> **決定論の層に LLM 相当の判断が紛れ込む**。相手先の呼称は入力 `context.counterpart_label`
> （利用者が渡した値・既定 `"unspecified"`）をそのまま返す。

> **この抽出は「意味的判断」ではない。** 3 要素の**共起**という構造条件のみで決まり、LLM に依存しない。
> 抽出漏れは `unresolved_kind: missing_check` として判定層に現れうるが、事実層の
> `completed_checks` は常にこの決定論規則の結果である。

---

## 人間関与の設計（HITL 非採用）

`config/config.yaml` は `memory_enabled: false` / `hitl.enabled: false`（**コメントではなく実値で宣言**）。
根拠は 提案書 の HITL Gate（MunicipalContractVariationEvidenceBriefAgent の 4 問テスト・全 No）。

**再呼び出しのループ（正常系）**: 判定層を返せないときは事実層 + `requires_human_review: true` +
`degradation.reason_codes` を返して `status: success` で終わる。利用者は記録を補って**再度 `/invoke` を呼ぶ**。
グラフ内 suspend を行わないため `interrupt()` は 1 箇所も存在せず、
PB-7 は **auto-waived — non-HITL**（scaffold stub は exact-name のまま残す。stub は
`config/config.yaml` の `hitl.enabled` を読む最新版であることを確認済み）。

---

## データ統治（永続化）

### 何を蓄積し、何を蓄積しないか

| 対象 | 扱い | 理由 |
|---|---|---|
| **承認済みハンドオーバーテンプレート**（固定スキーマ） | **KB に蓄積しない。`/invoke` の版固定入力**（`ref` + `version` + `content_hash`・adapter が正本から解決） | 組織が版管理して配布する統制物であり、AG が学習・蓄積する対象ではない。「どの版を適用したか」の再現性が要る（FinancialOutsourcingDueDiligenceEvidenceBriefAgent と同型） |
| **過去ハンドオーバーの非診断的観点** | `/ingest` + `/feedback` で KB / DB に蓄積（**これが蓄積型 moat**） | 「この種のインシデントでは切り戻し時刻の記録が抜けやすい」といった観点は組織の資産で、蓄積して初めて価値が出る |
| **過去インシデントの記録本文 / 生成済みハンドオーバー草案 / feedback の生本文** | **蓄積しない・embedding しない・モデル入力に渡さない** | scaffold Issue のガードレール（診断結論の間接漏洩の遮断） |
| **過去の `support_status` / 判定結論** | **想起しても継承しない** — Stage 2 の射影に含めず、Stage 3 のプロンプトにも入れない | 過去案件の判定を今回の判定に持ち込むと、`supported` が「前もそうだったから」で成立してしまう。渡すのは観点テキストと source ID のみ。実装フェーズ で `test_recalled_perspectives_carry_no_support_status`（プロパティテスト）として固定する |
| **今回のインシデント記録本文** | payload store（短命 TTL・scope + session 束縛・`mint_reference()` 形式の ref）。**永続化しない** | 機微本文の out-of-state 化（テンプレート標準） |
| **`/ingest` / `/feedback` で受け取る観点の自由文** | **サニタイズ後の 2 値のみ永続化**（`_audit` = DB 正本 / `_safe` = KB projection）。**入力そのままの値はどこにも保存しない** | 下記「2 値に分離」。`detect_pii` の既知の検出漏れが生で堆積する経路を作らない |

### スコープ分離

- **顧客分離は接続レベル**。顧客ごとに物理的に別 KB / DB へ接続し、認証済み caller → 認可顧客 →
  provision 済み接続で解決する。`tenant_id` 列で区切るマルチテナントではない。
- **顧客内の区画キーは 1 軸 `counterpart_carrier_id`**（相手キャリア単位）。
  相互接続の記録・観点は相手事業者との協定に紐づく機微であり、A 社との記録から得た観点を
  B 社向けの引き継ぎに混ぜてはならない。**per-request 必須・欠落は fail-closed**（省略可能にしない）。
  値は**認証済み caller から解決**し、クライアント自己申告は使わない。
  → **確定（ユーザー裁定・2026-08-20）**。
- KB の検索セーフ射影は **allowlist**（観点テキスト・source ID・provenance・`recorded_period` のみ）。
  denylist は使わない。

### 防御は 2 層 — `payload_ref` は層 1 しか守らない

| 層 | 守る対象 | 手段 |
|---|---|---|
| 層 1: 実行時の隔離 | 実行中の State（および checkpointer を有効化した構成では checkpoint） | 記録本文の `payload_ref` 間接化・Stage が本文を state に書き戻さない |
| 層 2: 永続 KB / DB の統治 | **AG 自身が所有し貯め続けるテーブル** | 書込前サニタイズ・アクセス制御・保持期間・訂正削除・監査（下記） |

**`payload_ref` を入れたことを「機微対策は済んだ」と読まない。** 層 1 は「今回の実行で本文が State に
載らない」ことしか保証せず、「AG が KB に何を貯め続けるか」は一切統治しない。

| 統治項目 | 本 AG の設計 |
|---|---|
| 書込前サニタイズ | `ingest` / `feedback` の**全書込経路**を同一のサニタイズ関数に通す。**fail-closed** — サニタイズに失敗したら 1 件も書かない（「できなかったので原文のまま保存」はしない）。**入力そのままの自由文は DB にも KB にも永続化しない**（下記「2 値に分離」） |
| アクセス制御 | 顧客専用接続（物理分離）＋ `counterpart_carrier_id` 区画。読み書きとも認証済み caller から解決した scope のみ |
| 保持期間 | 導入先ポリシーで設定（申し送り #2）。AG は無期限保持を前提にしない。**保持対象はサニタイズ済みの `_audit` / `_safe` のみ**（生の自由文は保持対象として存在しない） |
| 訂正・削除 | `/feedback` の `decision: retire` で論理的に無効化（**append-only** — 過去の記録を書き換えず、無効化の事実を追記する）。物理削除は運用手順（申し送り #2） |
| 監査 | 全書込に `emit_trace_event`（`ingest_rows_rejected` / `feedback_recorded`）＋ `feedback_audit_events`（`decided_by` は認証済み `caller_id`） |

### KB / DB に載る自由文は 2 値に分離する — **生の自由文はどこにも永続化しない**

`perspectives[].perspective_text` と `/feedback` の `revised_perspective_text` は**人間が書く自由文**で、
KB projection に載れば **次回の Stage 3 プロンプトに載る** — PII 漏洩だけでなく
**prompt injection の永続経路**になる。1 つの入力を 2 値に分け、**どちらもサニタイズ後の値**とする。

| 値 | 保存先 | 内容 |
|---|---|---|
| `perspective_text_audit` | **DB 原記録（AG 所有テーブルの正本）** | `detect_pii` → `mask_pii` を通した値。監査ログ・エクスポート用。**入力そのままの値はここにも保存しない** |
| **`perspective_text_safe`** | **KB projection（モデル可視はこれだけ）** | `_audit` に加えて ①`max_record_chars` で切詰 ②NFKC 正規化 ③**命令文パターン**（`ignore previous` / `以後の指示` / `システムプロンプト` 等）を検出したら **KB に書かず 400 相当で reject**（無言で落とさない・`reason_code: instruction_content`） |

> 🔴 **`*_raw`（入力そのまま）は持たない**（2026-08-20・レビュー指摘への対応）。
> 一般的な 3 値分離パターンは `*_raw` を「監査・法的説明責任のための正本」として DB に残すが、
> **本 AG では採らない**。理由:
>
> 1. **設計の一貫性** — 「`ingest` / `feedback` の全書込経路を同一のサニタイズ関数に通す・fail-closed」と
>    宣言しながら `*_raw` を素通しで保存するのは矛盾。永続層の統制境界が実質的に無効になる。
> 2. **既知の検出漏れが生で残る** — `detect_pii` は日本語のラベル無し氏名・全角の連絡先を検出しない
>    （§S-2「PII の責務分界」に実測付きで記録）。`*_raw` を持つと、**FW が拾えなかった PII が
>    AG 所有 DB に無変更で堆積する**。KB projection から外していてもモデル隔離にしかならず、
>    永続データの統治にはならない。
> 3. **ユーザー裁定との整合** — 「PII は framework の仕事であり、テンプレート側では補正しない」
>    （A-5）。**FW がマスクした結果だけを扱う**のがこの裁定の一貫した帰結であり、
>    FW が見落とした生データを意図的に貯めるのは裁定の趣旨と逆になる。
> 4. **保守アセットを増やさない** — `*_raw` を残す道（別統制のソースシステムとして
>    目的限定・アクセス境界・暗号化・保持削除・fail-closed 規則を定義する）は、我々が保守する
>    統制装置を増やす。本 AG が蓄積するのは相互接続の**非診断的な観点**であり、生の原文を
>    法的説明責任のために保持しなければならない領域ではない。
>
> **代償（正直に記録）**: 監査の正本がマスク済みの値になるため、「マスクによって失われた情報が
> 争点になる照会」には答えられない。導入先がその要求を持つ場合は、**AG の外の記録系**
> （元のインシデント管理システム）を正本とする運用にする（申し送り #8）。

**安全に保持できない入力の扱い（fail-closed）**: サニタイズ経路が例外で終わった場合、および
命令文パターンを検出した場合は、**その行を 1 件も書かずに `rejected[]` へ理由コードを返す**。
「サニタイズできなかったので原文のまま保存」は行わない。書込は
`_audit` と `_safe` の**両方が揃ったときだけ**成立する（片方だけの書込を作らない）。
この書込境界は `src/services/perspective_ledger.py` の内側 1 箇所に閉じる。
`ingest_node` / `feedback_node` は生の `perspective_text` / `revised_perspective_text` だけを渡し、
caller 側で `*_audit` / `*_safe` をラベル付けして渡す API は持たない。ledger が内部で
`detect_pii` → `mask_pii` と `_safe` projection を生成する。

モデル可視値は**構造化 JSON の値として**プロンプトに渡し、地の文に連結しない。

**実装フェーズ の受入テスト**（本節の policy を covered にする）:
- `test_kb_projection_never_receives_raw_free_text` — 「PII / 他区画の識別子 / 命令文 / 長さ超過」の
  4 パターンを `/ingest` と `/feedback` に投げ、KB projection に原文が到達しないことを assert
- `test_ingest_ledger_writes_only_audit_and_safe_text_values` /
  `test_feedback_ledger_writes_only_audit_and_safe_text_values` — **DB 側にも入力そのままの値が存在しない**
  ことを assert（`_audit` / `_safe` のどちらにも、既知の PII を含む入力が残らないことを確認する）
- `test_ledger_callers_cannot_label_text_as_audit_or_safe` — caller が `*_audit` / `*_safe` として
  任意の文字列を渡せないことを assert
- `test_sanitiser_failure_writes_nothing` — サニタイズ例外時に `_audit` / `_safe` のどちらも
  書かれないことを assert（変異検査で有効性を確認する）

> `ingest_result.rejected[].reason_code` に `instruction_content` を追加する（enum 表に反映済み）。

### 想起 backend の暫定実装に関する注意

Stage 2 の検索クエリは `structured_query`（構造化ラベル）を第一とするが、暫定 backend が
**部分一致（LIKE 等）で `perspective_text_safe` を検索する構成**を取る場合、日本語は空白で区切られないため
**語分割だけでは 0 件になる**（実務の入力は `本番組ではサプリメントの効能を…` の形）。その構成を選ぶなら
①7 文字以上の語のみ 2-gram 展開 ②最低一致数を**絶対値**で置く（比率にしない）
③`LIKE` パターンの `%` `_` `\` をエスケープし `ESCAPE` を付ける、が必要になる。
**正本 backend（全文検索 / ベクトル）に接続した時点で不要になる暫定措置**なので、実装フェーズ では
port 契約テストを先に固定し、暫定実装の作り込みは最小にする。

---

## 入力形式

| 形式 | 可否 |
|---|---|
| JSON（`records[].text` / `perspectives[].perspective_text`） | ✅ v1 の唯一の入力経路 |
| **PDF** | ❌ 受け付けない（パーサを持たない＝攻撃面を持たない） |
| **Excel（xlsx）** | ❌ 同上 |
| バイナリ / Base64 貼り付け | ❌ 品質ゲートで reject |

**入力品質ゲート** — ファイル入力口を持たないだけでは足りず、**貼り付けられたテキストそのもの**を検査する。

| 検査 | config キー | 既定 | reason_code |
|---|---|---|---|
| 長さ | `max_record_chars` | 8000 | `record_too_long` / `record_empty` |
| 制御文字比率（`\t\n` を除く C0/C1） | `max_control_char_ratio` | 0.01 | `control_char_ratio` |
| U+FFFD（置換文字）比率 | `max_replacement_char_ratio` | 0.005 | `mojibake_ratio` |
| 可読文字比率（英数・和文・句読点） | `min_readable_char_ratio` | 0.80 | `unreadable_ratio` |
| 先頭バイト列がバイナリ形式のマジックバイト（`%PDF` / `PK\x03\x04` / `\x89PNG` 等）に一致 | — | — | `binary_payload` |
| `record_id` の重複 | — | — | `duplicate_record_id` |

reject された記録は `input_quality.rejected[]` に `reason_code` 付きで返し、**残りの記録で処理を続ける**
（全件 reject なら事実層も空になるため `requires_human_review: true` + `input_quality_rejected`）。

---

## 適用部品一覧（parts library）

> 実装フェーズ は `parts/tools/apply_part.py` で写経適用し、`verify_part.py` の IDENTICAL / ADAPT 状態を保つ。
> 部品が覆う骨格を再発明しない。

| part-id | version | 適用先 | adaptation_points の埋め方 |
|---|---|---|---|
| `cat2-graph-skeleton` | 0.1.6 | `src/nodes/main_node.py` | `subgraph_factory` = `InterconnectHandoverWorkflowGraph`／`_parent_config` = `llm` / `kb` / `payload_store` ＋ `_validate_config` が検証する設計値の全キー（`recall_top_k` / `min_quote_chars` / `max_quote_chars` / `max_question_chars` / `max_hypothesis_chars` / `max_candidate_causes` / `candidate_causes_disclaimer` / `check_action_markers` / `corroboration_window_minutes`）／`extract_input` = 上記の明示 map（**自由文を含めない**）／`mode_specs` = 3 モードの昇格フィールド／`error_strategy = "propagate"`／`propagate_hitl = False`／`known_failure_codes` = `degradation.reason_codes` の allowlist |
| `output-envelope` | 0.1.2 | `src/nodes/post_process_node.py` | `format_invoke` = 2 層 envelope／`required_fields_by_mode` = invoke: `{mode, gate_kind, incident_ref, template_ref, template_version, requires_human_review, degradation}`／`gate_kind_by_mode` = `{invoke: "handover_draft", ingest: "ingest_summary", feedback: "feedback_summary"}`／`max_field_chars` = `None`（長さ検査は S-3 検査 #4 が担当） |
| `evidence-gate` | 0.3.1 | `src/nodes/inner/evidence_verify_node.py` | `gate_kind` = `handover_draft`／`cited_ids_key` = `cited_source_ids`／`retrieved_records_key` = `source_registry`／`evidence_map_key` = `evidence_map`／`record_id_keys` = `("source_id",)`／`claim_source_keys` = `("assertion_source_ids", "source_ids", "evidence_source_ids")`／`passthrough_keys` = `("handover_draft", "evidence_map", "unresolved", "requires_human_review", "degradation")` |
| `llm-draft-stage` | 0.2.0 | `src/nodes/inner/handover_mapping_node.py` | `config['llm']` 注入／`build_prompt` = テンプレスキーマ + enum allowlist + **要素 1 件の実例**を含むプロンプト（空配列だけを示すと LLM が何も返せない既知欠陥の先回り）／`build_fallback` = LLM 未設定時の決定論縮退（判定層を空にし `degradation.reason_codes = ["llm_unavailable"]`）／`build_evidence` = `cited_source_ids` |
| `scoped-retrieval` | 0.3.0 | `src/nodes/inner/perspective_recall_node.py` | `scope_keys = ("counterpart_carrier_id",)`／`backend` = 顧客専用 KB 接続の Protocol 実装／`normalize_query` = **ingest 書込側と同一関数**／`top_k` = `config.recall_top_k`(5)／`TOP_LEVEL_SAFE_KEYS` / `METADATA_SAFE_KEYS` = 観点テキスト・source ID・provenance・`recorded_period` のみ（`support_status` 相当は**含めない**）／クエリは `structured_query` を使う |
| `ingest-row-pipeline` | 0.4.0 | `src/nodes/inner/ingest_node.py` | `required_fields` = `perspective_text` / `perspective_kind` / `source_ref`／`CheckpointScrubPolicy.safe_keys` = ID・enum・日付のみ（**allowlist**）／`scope_keys = ("counterpart_carrier_id",)` |
| `payload-store` | 0.4.0 | `src/services/payload_store.py`（写経） | `ttl_seconds` = `config.payload_ttl_s`(900)／`scope_keys = ("counterpart_carrier_id",)`／`clock` はテストで単調増加を注入／**ref は `mint_reference()`（letter-only）** |
| `entry-adapter` | 0.4.0 | `src/api/server.py` | `SCOPE_KEYS = ("counterpart_carrier_id",)`／`STG_DEFAULT_SCOPE_ENVS` = `STG_DEFAULT_COUNTERPART_CARRIER_ID`／`Operation` 対応: `/invoke`→`READ_INVOKE`・`/ingest`→`WRITE_INGEST`・`/feedback`→`WRITE_FEEDBACK` |
| `feedback-intake` | 0.2.0 | `src/nodes/inner/feedback_node.py` | `scope_keys` 同上／`payload_ref_key` = `canonical_payload_ref`／`decided_by` は**認証済み `caller_id` で上書き注入** |
| `accumulation-ledger` | 0.1.1 | `src/accumulation_ledger/`（写経部品・runtime 未使用）／`src/services/perspective_ledger.py`（TEL 独自 seam） | `src/accumulation_ledger/` は部品同梱と conformance tests を保持するが、本 AG の standalone 既定は `# PART:` マーカーを持たない独自 `InMemoryPerspectiveLedger` を使う。理由は、本 AG が `*_raw` を持たない 2 値永続化を採用し、ledger 内の単一書込境界で raw input を `detect_pii` → `mask_pii` して `_audit` / `_safe` だけを生成する必要があるため。caller から `*_audit` / `*_safe` を受け取らない。runtime 置換する DB/KB seam もこの no-raw 2 値契約を維持する |

---

## config/agent.yaml・config/config.yaml の設計値

### `config/agent.yaml`（静的マニフェスト — AgentRegistry 契約）

| key | 値 | 備考 |
|---|---|---|
| `id` | `TEL-C2-113` | |
| `name` | `InterconnectIncidentHandoverAgent` | **空白を含めない**（registry key 要素） |
| `namespace` | `tel` | 近縁 TEL テンプレの実値規約に合わせる |
| `version` | `0.1.0` | |
| `enabled` | `true` | |
| `category` | `Cat 2` | `gate-cat-consistency` が ID / README と照合 |
| `generation_mode` | **`llm`** | Stage 3 が LLM 主権。`deterministic` ではない |
| `industry` | `TEL` | |
| `base_type` | **`AgentBaseGraph`** | |
| `class` | `src.graph.graph.InterconnectIncidentHandoverGraph` | **単一のドット付きパス**。設計フェーズ で実クラス名へ改称済み（identity 置換）。`Graph` は同一オブジェクトの別名として残し、scaffold 既定の `src/api/server.py` の import を壊さない |
| `required_trust_level` | `VERIFIED_EXTERNAL` | |
| `requires.secrets` | `[]` | **起動時に必要な秘密は無い**（LLM API キーは server.py が `.get()` で読み、無ければ縮退する。ここに載せると起動不能になる） |
| `requires.extras` | `["openai"]` | 両エントリ（HTTP / Marketplace）が `LazyAzureOpenAILLM` 経由で `AzureOpenAIClient` を構築し `langchain_openai` を import するため |

### `config/config.yaml`（実行時パラメータ — 全キーが実値で存在する）

| key | 既定値 | 用途 |
|---|---|---|
| `max_retry` / `timeout_s` | `3` / `30` | scaffold 既定 |
| `memory_enabled` / `hitl.enabled` | `false` / `false` | HITL 非採用（**実値で宣言**。PB-7 の条件付き skip がこれを読む） |
| `default_business_timezone` | `Asia/Tokyo` | adapter が 1 回だけ確定する基準 TZ |
| `default_counterpart_label` | `unspecified` | `context` 省略時の注入値 |
| `max_records` / `max_record_chars` | `200` / `8000` | 入力上限 |
| `max_control_char_ratio` / `max_replacement_char_ratio` / `min_readable_char_ratio` | `0.01` / `0.005` / `0.80` | 品質ゲート |
| `min_quote_chars` / `max_quote_chars` | `8` / `300` | 逐語照合の下限（vacuous 防止）と上限（本文エコー防止）。`case_payload` 実測で 9 文字前後の正当な日本語技術記述があるため、下限は 8 に較正済み |
| `max_question_chars` | `120` | `question_text` の上限 |
| `max_hypothesis_chars` | `160` | `candidate_causes[].hypothesis_text` の上限 |
| `max_candidate_causes` | `5` | 原因候補の件数上限（網羅を装わない） |
| `candidate_causes_disclaimer` | **`config/config.yaml` の値が正本**（docs には転記しない — 二重管理で乖離するため） | 候補に添える免責。**LLM に書かせず config から入れ、S-3 が等値検査**する |
| `recall_top_k` | `5` | 過去観点の想起件数 |
| `payload_ttl_s` | `900` | payload store の TTL |
| `check_action_markers` | `["実施", "切り戻し", "復旧確認", "試験", "疎通確認", "設定変更", "rollback", "test", "verified"]` | Stage 1 の `completed_checks` 抽出語彙 |
| `corroboration_window_minutes` | `120` | 接地ゲートの経路 (c) の時間窓 |

> これらは**テンプレ利用者が自環境で調整する前提の初期値**。すべて config 化してあり、
> コードに定数として埋め込まない。**本表の全キーが `config/config.yaml` に実値で存在する。**

---

## S-1 Trust Level

| ノード | `required_trust_level` | 根拠 |
|---|---|---|
| `PreProcessNode` | `VERIFIED_EXTERNAL` | 顧客記録を扱う。匿名呼び出しを通さない |
| `InterconnectHandoverGraphNode` | `VERIFIED_EXTERNAL` | **`GraphNode` は `BaseNode` 直サブクラスで `FunctionNode` の宣言強制が効かない** — 宣言しないと ANONYMOUS 既定になる |
| `PostProcessNode` | `VERIFIED_EXTERNAL` | |
| inner Stage 1〜4 | `VERIFIED_EXTERNAL` | LLM コストが発生する Stage 3 を含む |
| inner `IngestNode` / `FeedbackNode` | **`INTERNAL`** | KB / DB への書込を伴う |

`TrustLevel` は文字列 enum（`ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL`）で、int 比較はできない。
全 `FunctionNode` サブクラスがクラス本体で明示宣言する（未宣言は import 時 `TypeError`・`gate-trust-level-check`）。

---

## Import Isolation Confirmation

- `from agenticstar` / `import agenticstar` を**書かない**（Level 0 直 import 禁止）
- `agents.base` からの import を**書かない**（廃止 L2）
- 他テンプレートの `src/` を import しない
- 検出器は **`framework.security`** から引く（`framework.nodes.function_node` からの再エクスポートに依存しない）
- 部品の import identity は **`src.<part>`** に統一する（top-level import は非 editable 経路で壊れる）

---

## Design-entry 受入条件の反映（設計レビュー指摘 Medium・2026-07-24）

| Finding | 反映先 |
|---|---|
| Restaurant Allergen-Menu Change Handover Agent と重複しない受入境界を明記（本 AG は *interconnect incident records の event/correlation/source-backed handover state* を正本とし、一般的な障害報告 / handover Markdown 生成は扱わない） | 入力契約が `handover_template`（版固定）+ `records[]`（3 種の enum）に閉じており、出力は `source_registry` に紐づく `handover_draft` のみ。**自由な Markdown 文書生成の口を持たない**（出力 allowlist に `markdown` / `document` 相当のキーが存在しない）。Restaurant Allergen-Menu Change Handover Agent は決定論 reconcile が権威で LLM は曖昧点の要約補助だが、本 AG は Stage 3 の意味的構造化・関係判定が主権であり、Stage 1/4 の決定論は採番と検証に限定される |
| corroboration 不足時は相関・原因を推定せず source ID 付き `unresolved` に落とす | `support_status` の**決定論的降格と接地ゲート**、および `unresolved[]`（`unresolved_kind` + `source_ids` 必須）。`root_cause` / `diagnosis` は出力スキーマに存在しない。降格したら対応する `unresolved` の存在を決定論的に保証する。**⚠️ caveat（2026-08-20 ユーザー裁定）**: 「**診断結論としては**推定しない」は維持するが、**未判定の確認候補 `candidate_causes` は別枠で列挙する**。これは Finding の文言を意図的に超える設計変更であり、§`candidate_causes` に根拠と受容リスクを記録した |

---

## SDK 実機確認（AgentCore SDK **1.0.1** — 2026-08-20 実施）

> 設計が依拠する framework/shared のシンボルを、CI が実際に install する版
> （CI の `AGENTCORE_WHEEL_SPEC = agenticstar-agentcore[anthropic]==1.0.1`）で introspection した結果。

| 確認項目 | 実測結果 | 設計への反映 |
|---|---|---|
| `InvocationContext` の実フィールド | `correlation_id` / `session_id` / `thread_id` / `parent_trace_id` / `caller_id` / `caller_trust_level` / `secrets` / `hitl_allowed`。**`tenant_id` / `org_id` は存在しない** | 区画キーは ctx から取らず、adapter が認証済み caller から解決して `request_scope`（State）へ渡す |
| `AgentBaseGraph.get_output()` | `{"output": state.get("formatted_output") or state.get("result"), "status", "trace_id", "correlation_id", "node_history"}` | カスタム欄は透過しない → **`post_process` が `formatted_output` に畳み込む** |
| `AgentBaseGraph.__abstractmethods__` | `{"name"}` のみ | outer は `name` + `register_nodes` の override で足りる |
| `BaseGraph.__abstractmethods__` | `{name, state_schema, _validate_config, register_nodes, add_edges, route, get_output}` の **7 つ** | inner はこの 7 つを全実装（未実装は `TypeError`） |
| S-2 の走査対象 | `_PII_SCAN_FIELDS = ("user_input", "validated_input", "llm_response")`。`str` 値のみ（`dict` はスキップ） | 記録本文を str フィールドに載せない設計（§S-2） |
| S-2 の injection ブロック対象 | `_INJECTION_BLOCKING_FIELDS = ("user_input", "validated_input")`。実測で通常の運用記録（「復旧確認は必ず実施してください」）は finding ゼロ、明示的な指示上書き文のみ high 判定 | 運用記録の自然な命令形で誤 reject されないことを確認 |
| `detect_pii` の実挙動 | `name`（Latin Title Case 2 語以上）が **`Session Border Controller` / `Route Reflector` を誤検出**。`name_jp` / `email` / `phone_jp` は正しく検出。`INC-2026-0817-014` / `GW-TKY-03` / `SIP 503` は非検出 | **補正しない**（PII は FW の責務・§S-2「PII の責務分界」）。識別子が非検出であることが、裏付け照合が成立する根拠 |
| S-3 の credential 走査 | `detect_credentials_in_value` は `dict` / `list` / `tuple` を**再帰**して全 string leaf を走査（**1.0.1**。1.0.0 はトップレベル `str` のみだったので版で挙動が違う） | ネストへの credential 密輸は framework が塞ぐ。ただし **PII は S-3 で一切走らない**ので、テンプレが `_extra_security_gate_output` で再走査する |
| 検出器の import 経路 | `framework.security.detect_pii` / `detect_credentials_in_value` は 1.0.1 に存在。`mask_pii` は `framework.security.pii_masking` | `framework.nodes.function_node` からの再エクスポートに依存しない |
| `_extra_security_gate_output` の引数 | `(self, result: dict)` — **`state` を受け取らない** | 分岐値は `execute()` が `formatted_output["mode"]` / `["gate_kind"]` に宣言。provenance 検証は Stage 4 に一本化 |
| `emit_trace_event` のシグネチャ | `(event_type: str, payload: dict, state: dict) -> None` | 3 引数で呼ぶ |
| `GraphNode.execute()` | `subgraph.invoke(user_input, session_id=ctx.session_id, ctx=ctx)`。`extract_input()` の戻り値が inner の `user_input` になる | inner の S-2 が inner `user_input` を再走査する → **`extract_input` に自由文を含めない**根拠 |
| `AgentStatus` | `pending` / `success` / `retry` / `error` / `timeout` / `awaiting_human` / `cancelled` | `status` は必ず `AgentStatus.X.value`（文字列）で返す |
| PB-7 scaffold stub | `_CONFIG_PATH` は `config/config.yaml` を参照（最新版） | `hitl.enabled: false` を config.yaml に実値で置けば正しく auto-skip する |

---

## ユーザビリティ設計レビュー（修正した点）

| 観点 | 当初案 | 修正後 |
|---|---|---|
| 入力 | 利用者に `assertions` / `completed_checks` を事前に構造化させる | **記録をそのまま渡せる**（`record_id` / `record_kind` / `recorded_at` / `text` の 4 欄のみ）。構造化は AG の仕事であり、利用者に前処理を要求しない |
| 出力 | 検証失敗時は草案を返さない | **2 層出力に変更**。判定できなくても事実層（`sources` / `timeline` / `completed_checks` / `observed_identifiers` / `event`）は返す。「評価できないから何も見せない」は、履歴に依存しない事実まで捨てることになり、AG の価値が「データが全部揃った顧客だけのもの」になる |
| 出力 | 原因の可能性を一切出さない | **`candidate_causes` を追加**（ユーザー裁定 2026-08-20）。断定は 6 つの構造制約で防ぎつつ、「次に何を疑い、何を確認すればよいか」を根拠付きで返す。候補が無いと受け手がゼロから組み立て直すことになり、引き継ぎ資料として使い勝手が落ちる |
| 出力 | `support_status` だけを返す | **未解決を解くための確認質問（草案）まで返す**。`ask_target` で「相手キャリアに聞く」か「社内記録を確認する」かを分離し、受け取った担当者が次に何をすべきか分かる |
| 出力 | 全項目が同じトーンで並ぶ | `unresolved` / `contradictions` を独立フィールドとして分離し、埋まった欄と埋まらなかった欄を一目で区別できるようにした（空欄は「保証できない」と正直に言っている印であり、それ自体が価値） |
| 対話 | 検証失敗時に何が足りないか分からない | `degradation.reason_codes` / `detail_codes` + `input_quality.rejected[].reason_code` を**閉じた列挙**で返し、利用者は不足を補って**再度 `/invoke` を呼ぶ**だけでよい |
| エラー | 品質ゲートで 1 件でも reject されたら全体を止める | **reject された記録を除いて処理を続け**、`rejected[]` で理由を返す |

---

## Design Decision Record

### 先回りセット照合（設計段階のレビュー指摘 先回りセット）

> **docs 起草の前に**照合した（独立レビュー・変更作成より前）。該当なしも記録する。

| # | 先回り項目 | 該当 | 対処 |
|---|---|---|---|
| 1 | provenance を「ID 実在」で終わらせない | ✅ 該当 | ①**逐語引用**（canonical_text の literal 部分文字列）＋ **長さ下限 `min_quote_chars=8`** ②`corroborating_check_ids` の実在照合と**接地ゲート付き降格** ③**双方向**（claim→source の健全性＋`evidence_map` を `source_registry` から決定論構築） ④`source_id` は `^[A-Za-z0-9_-]{1,64}$` ⑤PB に「**実在するが無関係な source_id を引用**」ケースを入れる。**数値の NFKC + 漢数字正規化は該当なし** — 本 AG は数値を構造化フィールドに持たず、時刻は RFC3339 の機械パースのみ |
| 2 | 永続 KB を持つなら統治契約を design 入場条件に | ✅ 該当 | 許可/禁止フィールド（KB 射影 allowlist・診断結論と記録本文と `support_status` を除外）・**区画キー `counterpart_carrier_id` を per-request 必須 / 省略不可 / 欠落は fail-closed**・顧客分離は接続レベル・保持と削除は導入先ポリシー（運用ガイド）・S-4 監査イベント・**永続層に対する PB テスト**（PII / credential 非索引化、区画 A→B の不可視性）。**`tenant_id` + `org_id` の 2 軸は不採用**（顧客分離は接続レベルのため） |
| 3 | PoC 検証計画を proposal に | ❌ 該当なし | **当プロジェクトは PoC を実施しない**（既定）。販売実現性は提案書に「未検証」と正直に記録し、買い手の具体化は事業側のマターとして分離した |
| 4 | 継承欄は継承クラスのみ | ✅ 該当 | 冒頭の **L1 Base** 欄は `AgentBaseGraph` のみ。`GraphNode` / inner `BaseGraph` は「構成」として Architecture 節に記載 |
| 5 | 外部ソース確認は実施日を明記 | ❌ 該当なし（一部適用） | **外部 API / 外部データソースに依存しない**。代わりに **SDK 実機確認の実施日（2026-08-20・1.0.1）**を明記 |
| 6 | 入力の allowlist と品質ゲート | ✅ 該当 | ①マジックバイト判定 ②**パーサを持たない選択**（PDF / Excel 非対応）③**索引に入るテキストそのものに品質ゲート**（制御文字・U+FFFD・可読文字比率・長さ・ID 重複） |

### 裁定記録 — Marketplace 経路の scope（2026-08-24・ユーザー）

| 論点 | 判断 | 反映先 |
|---|---|---|
| Marketplace 経路の `request_scope` を deployment 環境変数の固定値にすることの是非。本設計は「**認証済み caller から解決**」を 8 箇所で不変条件として宣言しており、この置換はその緩和にあたる | **(A) 暫定的に許容して進める**（「まずは A」＝暫定。KB 配線時に再検討する） | 本節 + 該当レビューコメント |

**許容の根拠**: 現状は KB 未配線で、想起は常に `degradation.reason_codes: ["kb_scope_unresolved"]`
に落ちるため、誤区画のデータが出力に混入する経路が存在しない。Marketplace runner が供給する
caller 情報は `USER_ID` のみで、そこから `counterpart_carrier_id` を解決する材料が無い
（caller 申告の scope を認可根拠にすることは禁止されている）。

**再検討のトリガー（この条件を満たす前に本項を再裁定すること）**:
- 実 KB を配線するとき。固定 scope のまま配線すると、利用者が別キャリアのインシデントを
  投げても固定区画の過去観点が返る
- platform 側に検証済みの caller→キャリア解決チャネルが用意されたとき

**それまでの運用前提**: 1 deployment = 1 相手キャリア（運用ガイド §AGENTIC STAR に記載）。

### 既知知見の照合（設計に効いたもの）

| 知見 | 該当 | 反映 |
|---|---|---|
| `uuid4` の ref は S-2 が破壊書換する | ✅ | payload_ref は `mint_reference()`（letter-only）— canonical 化パイプライン節 |
| S-2 は execute 前に PII をマスクする（`InitializeNode` も `FunctionNode`） | ✅ | 記録本文を `user_input` に載せない設計・raw が要る処理は adapter 側で完結。**マスクの内容には手を入れない**（PII は FW の責務・ユーザー裁定 2026-08-20） |
| S-3 出力ゲートの走査範囲は版で違う（1.0.0 トップレベルのみ / 1.0.1 再帰）。**PII は S-3 で走らない** | ✅ | S-3 検査 #5 でテンプレが PII を再走査 |
| 1.0.1 は `function_node` からの検出器再エクスポートを外した | ✅ | import は `framework.security` から |
| LLM 供給値で決定論的に絞り込むと偽の肯定に到達する | ✅ | **`supported` / `contradicted` の接地ゲート**（識別子トークンの共有をトークン境界一致で要求）。ゲートは affirmative のみ |
| 版固定の統制物は KB でなく `invoke` の版固定入力 | ✅ | `handover_template` を版固定入力に。**pin は中身ではない**（adapter が正本から解決・偽造 pin は fail-closed）。feedback は append-only + 候補キュー |
| LLM に本文を渡すとモデル経由で State へ還流する | ✅ | quote に **`max_quote_chars` 上限**を設け、超過は切り詰めずに判定層を落とす。Stage 3 の返り値に本文を載せない |
| Stage ノードが解決した生本文を state に書き戻すと漏れる | ✅ | Stage 1 は本文を返さない・Stage 2 の検索は `structured_query`（構造化ラベルのみ） |
| 「判定できない」と「情報を出せない」は別 | ✅ | **2 層出力**（事実層は常に返す・`assessment_complete` は判定層からのみ導出） |
| 設計フェーズ の runtime 変更は identity 置換限定＋README に実装状態 | ✅ | クラス改称と re-export のみ（11 行）・README に「実装状態」節（ファイル別対応表） |
| PB-7 の skip ヘルパーが `agent.yaml` を読む版は移行で偽の緑になる | ❌ 該当なし | scaffold の stub が `config/config.yaml` を読む最新版であることを実機確認 |
| `AgentBaseGraph.get_output()` は `error_log` を返さない | ✅ | outer で `get_output()` を override（重複排除 + traceback 除去）— 検証は end-to-end |
| `payload_ref` は層 1 しか守らない（永続 KB の統治は別） | ✅ | 2 層統治の表を追加（書込前サニタイズ fail-closed / 保持期間 / 訂正削除 / 監査） |
| `pre_process → main` は無条件辺（pre が error でも main が走る） | ✅ | `GraphNode.execute()` で上流 error を passthrough（`GraphNode` 境界マッピング表に追記） |
| センシティブ実値は S-3 を通る前に checkpoint される（checkpointer 有効時） | ⚠️ 条件付き | 本テンプレは `memory_enabled: false` で checkpointer を attach しないため、現構成では発生しない。**checkpointer を有効化する場合は、`formatted_output` の `quote`（canonical 済みだが業務機微を含む）も checkpoint に載ることを前提に再評価する**（運用ガイド に申し送り） |
| ゲートのテストは「実運用で最も普通の入力」で書く | ✅ | 実装フェーズ のテスト入力規約として明記（下記） |
| KB に載る人間の自由文は PII だけでなく **prompt injection の永続経路** | ✅ | `perspective_text` を `_audit` / `_safe` の **2 値**に分離し、モデル可視は `_safe` のみ。命令文パターンは KB に書かず reject。**一般的なパターンの `*_raw`（入力そのまま）は採らない** — 理由は §「2 値に分離」（レビュー指摘対応・2026-08-20） |
| S-3 allowlist の免除条件に `.*` を書くとチャネルになる | ❌ 該当なし | 本設計の S-3 は免除条件を持たない（全フィールドが宣言済みキーの完全一致で検査される） |
| 日本語の想起は空白分割では機能しない（2-gram + 最低一致数） | ⚠️ 条件付き | クエリは `structured_query` が第一だが、暫定 backend が部分一致型なら該当。注意点を明記し、作り込みより port 契約テストを優先する |

### ユーザー判断の反映（2026-08-20・設計レビュー）

| # | 論点 | 判断 | 反映先 |
|---|---|---|---|
| A-1 | 原因の扱い（断定の排除 vs 可能性の列挙） | **原因の断定はしない。ただし原因候補の列挙はする** — 「候補が一切出ないと AG の使い勝手が悪い」。`origin: inferred`（記録に無い可能性）も含めて出す | `candidate_causes[]` を判定層に新設（§`candidate_causes`）。**断定の手段を構造から取り除く 6 点**（断定フィールド・確度・順位を持てなくする。自由文の言い回しは保証しない）／禁止フィールドに順位・確度系 6 つを追加／Stage 3・4 の責務と検証／enum `cause_origin`・`candidate_status`／config 3 キー／**承認前提（decision の「原因を推定しない」）からの意図的乖離として記録** |
| A-4(ii) | 顧客内の区画キーの軸数 | **(A) `counterpart_carrier_id` の 1 軸**を採用 | §データ統治「スコープ分離」／適用部品一覧の `scope_keys`（`scoped-retrieval` / `ingest-row-pipeline` / `payload-store` / `entry-adapter` / `feedback-intake` / `accumulation-ledger`）／共通 adapter 契約 #3 |
| A-5 | `detect_pii` の誤検出（英語技術語彙のマスク）への対応 | **対応しない。PII 検出の精度は framework の責務であり、テンプレート側で補正しない**（誤検出側にも「FW 標準のみ」の責務境界を適用） | §S-2「PII の責務分界」を新設し post-filter と `config/technical_term_allowlist.yaml` を**撤去**／S-3 の PII 再走査も allowlist 例外なしに変更／受容する限界を表で明示し `request:coe` 起票候補として記録／申し送り #3 |

### 既定適用（正典 §2 — 聞かずに適用した決着済み判例）

| 項目 | 適用した既定 | 出典 |
|---|---|---|
| S-3 の denylist | **実装しない**（allowlist 一本化・最初から作らない） | §2-4 / Travel Supplier Sustainability Claim Evidence Validator |
| PII サニタイザ | FW 標準 `detect_pii` のみ。**日本語補完の自前実装をしない** | §2-4 / Outsourcing SLA Deviation RCA Rebuttal-Brief Agent B-5 |
| 入力形式 | PDF / Excel を受け付けない | §2-4 |
| 顧客分離 | 接続レベル（物理分離）。`tenant_id` 列区切りではない | §2-4 / Customs Shipment Document Completeness Validator・Return Fraud Investigation Memo Drafting Agent |
| checkpoint スクラバ・業務キー | allowlist | §2-4 |
| 運用パラメータ初期値 | 利用者が自環境で調整する前提の初期値として決め打ち（config 化） | §2-3 |
| 顧客の前提データ保有 | 承認済みテンプレート・過去記録の保有は**当然の前提**。薄い/無い場合の縮退動作を書く | §2-6 |
| 未配線シーム | LLM / KB / DB は注入シームのまま 実装フェーズ → STG に進む。運用ガイド に実配線時の解消事項を明記 | §2-7 |
| LLM 注入 | `config["llm"]` のみ。モデル ID・具体クライアントを書かない | §2-5 |
| `agent.yaml` 実値化 | `base_type` = `AgentBaseGraph`・`class` = 実クラス名・README の `{{}}` 全実値化 | §2-5 |
| DocGen の出力設計 | **穏当設計**（LLM が構造化 JSON と限定された自由文を書き、逐語引用 + evidence_map で人間レビューを支援）。構造化描画は採らない | §2-2 / CTDClinicalSummaryAgent・SDSRevisionEvidenceAgent |
| 確認質問の生成 | **LLM 生成を維持**し、疑問文型 allowlist のような過剰防御を入れない。`question_topic` / `ask_target` / `why_needed_code` の enum 化＋長さ上限＋PII 再走査で閉じる | JAMemberInquiryIntakeAgent |

### 運用ガイドへの申し送り（運用ガイド本体は後段ステージの成果物 — 本設計フェーズでは作成しない）

> 本設計書から「運用ガイドに記載する」と書くだけでは参照が空振りする。**書くべき中身を本表で確定**し、
> 運用ガイド本体は STG フェーズで作成する。この表が申し送りの正本。

| # | 申し送り事項 | 内容 |
|---|---|---|
| 1 | 入力の非識別化前提 | `detect_pii` は日本語のラベル無し氏名・敬称のみの氏名・全角の連絡先を検出しない。加入者識別子と個人連絡先を記録に含めない運用を導入先の前提として記載する |
| 2 | KB の保持期間・訂正削除 | 保持期間は導入先ポリシーで設定。`decision: retire` は論理無効化であり物理削除は運用手順で行う |
| 3 | PII 検出の既知の限界 | `detect_pii` の誤検出により**英語の装置種別名・プロトコル名（`Session Border Controller` 等）がマスクされうる**こと、および日本語のラベル無し氏名・全角連絡先が検出されないことを明示する。いずれも FW 側の課題でテンプレートでは補正しない（`request:coe` 起票候補） |
| 4 | 未配線シームの実配線 | LLM / KB / DB は注入シーム。実配線時に解消すべき事項（Stage 2 の想起が空を返す前提、`llm_unavailable` 縮退の解除）を記載する |
| 5 | checkpointer を有効化する場合 | 現構成は `memory_enabled: false`。有効化するなら `formatted_output` の `quote`（canonical 済みだが業務機微を含む）が checkpoint に載ることを前提に再評価する |
| 6 | 想起 backend の暫定実装 | 部分一致型 backend を使う場合の日本語 2-gram の必要性と、正本 backend 接続で解消される旨 |
| 8 | 監査正本の所在 | AG 所有 DB に残るのは**マスク済みの `_audit`** であり、マスクで失われた情報が争点になる照会には答えられない。生の原文を正本として要求される場合は、**AG の外の記録系（元のインシデント管理システム）**を正本とする運用にする |
| 7 | 原因候補の読み方 | `candidate_causes` は**確認の出発点であって推定結果ではない**こと、`origin` の `recorded` / `inferred` の違い、順位は付いていないこと、断定的な言い回しが混じりうること（構造では防げない残余）を明示する |

### 未確定事項（実装フェーズ へ持ち越さない・本設計フェーズ で閉じる）

| # | 事項 | 状態 |
|---|---|---|
| 1 | ~~区画キーの軸数~~ | **確定（ユーザー裁定・2026-08-20）= `counterpart_carrier_id` の 1 軸**。未確定事項から外し、下記「ユーザー判断の反映」に移動 |
| 3 | `min_quote_chars = 8` / `max_quote_chars = 300` | **実測較正済み**。`case_payload` の records では正当な引用断片が 9 文字前後になるため、vacuous 防止を維持しつつ短い日本語技術記述を弾かない下限として 8 に確定 |
| 4 | `check_action_markers` の語彙 | `config/config.yaml` に**実値で存在済み**。実装フェーズ で実記録に対する抽出率を**テストで確認**し、導入時に較正する |
| 5 | LLM / KB / DB の実接続 | 未配線（注入シーム）。scaffold / CI が実接続手段を提供していないため（正典 §2-7）。運用ガイド に実配線時の解消事項として明記する |
| 6 | `pyproject.toml` の `[tool.ruff.lint.per-file-ignores]`（`src/graph/graph.py` / `tests/unit/test_main_node.py` の F401） | **設計フェーズ 限定の暫定**。両ファイルは scaffold master 由来で、設計フェーズ の差分を最小に保つため per-file 抑止した（前例 = SupplyDataQualityPrecedentAgent）。**実装フェーズ で実装に置き換えたら該当エントリを削除する** |
