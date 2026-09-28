"""AGENTIC STAR Marketplace エントリポイント（Pod の CMD が実行する）。

役割は `src/api/server.py` と同じ **entry adapter**。`docs/02_design.md` が
定める不変条件（mode は adapter が決定論的に決める／request_scope は
呼び出し元の自己申告を使わない）を、HTTP ではなく Marketplace 経路で満たす。

Marketplace runner が渡すのは「最新のユーザーメッセージ 1 本」と
`input_context={"conversation_history": [...]}` だけで、`mode` も
`request_scope` も入らない（`shared/bootstrap/marketplace_app.py`）。
そのため adapter が両方を固定で載せる:

- `mode` は `invoke` 固定。読み取り専用エントリのみを公開する。
  `ingest` / `feedback` は書込であり `INTERNAL` trust を要求するため、
  この経路からは到達させない（HTTP 経路のまま）
- `request_scope` は entry-adapter 部品が既に持つ「read-only エントリ限定の
  既定 scope」と同じ仕組みで、環境変数から解決する（`STG_DEFAULT_SCOPE_ENVS`）

LLM は `config["llm"]` の注入シームに `LazyAzureOpenAILLM` を刺す。runner は
`agent_cls()` を引数なしで生成するため、Graph 側の既定として持たせる。
"""

from __future__ import annotations

import os
from typing import Any

from framework.schemas.invocation_context import InvocationContext

from src.entry_adapter.auth import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS, _valid_scope_value
from src.graph.graph import InterconnectIncidentHandoverGraph
from src.services.azure_llm import REQUIRED_SECRET_KEYS, LazyAzureOpenAILLM

AGENT_ID = "TEL-C2-113"
NAMESPACE = "tel"
MARKETPLACE_MODE = "invoke"


def resolve_scope() -> dict[str, str]:
    """宣言された scope キーを環境変数から解決する（fail closed）。

    値の trim や補正はしない。空白入り・空・改行入りの環境変数は deployment の
    設定ミスであって、黙って直すと誤った区画のデータを返しうるため起動時に落とす。

    検証は entry_adapter の `_valid_scope_value` を再利用する。自前で書くと
    判定がドリフトする（内部改行を通してしまい、起動時 fail-closed のつもりが
    per-request の `scope_missing_or_invalid` に劣化していた）。

    > 環境変数名について: `STG_DEFAULT_SCOPE_ENVS` は元々
    > 「entry-token 認証後の read-only エントリに限る STG 既定 scope」の用途で
    > 宣言されたもので、この経路（Marketplace・恒常・認証ゲートなし）は 2 つ目の
    > 消費者にあたる。名前と用途が乖離しているため、本来は parts:entry-adapter
    > 側で両用途を明文化した名前へ改版すべき。横展開の前に対応する。
    """
    scope: dict[str, str] = {}
    invalid: list[str] = []
    for key in SCOPE_KEYS:
        env_name = STG_DEFAULT_SCOPE_ENVS[key]
        value = os.environ.get(env_name, "")
        if not _valid_scope_value(value):
            invalid.append(env_name)
            continue
        scope[key] = value
    if invalid:
        raise RuntimeError(
            f"{AGENT_ID}: scope 環境変数が未設定または不正: {invalid}. "
            "Agent Settings の Environment variable に設定する。"
        )
    return scope


class MarketplaceGraph(InterconnectIncidentHandoverGraph):
    """Marketplace 経路の entry adapter を被せた Graph。"""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = dict(config or {})
        merged.setdefault("llm", LazyAzureOpenAILLM())
        super().__init__(merged)
        self._scope = resolve_scope()

    def provision_secrets(self, provider: Any) -> None:
        """秘密の束縛時に Azure 設定の欠落を検出して**起動を止める**。

        runner は `compile()` の後にこれを呼ぶ。ここで落とさないと、Pod は起動に
        成功して invoke のたびに `llm_call_failed` を返す状態になる。scope は
        既に起動時 fail-closed なので、deployment 設定ミスの壊れ方を揃える。
        """
        super().provision_secrets(provider)
        missing = [key for key in REQUIRED_SECRET_KEYS if not provider.get(key)]
        if missing:
            raise RuntimeError(
                f"{AGENT_ID}: Azure OpenAI 設定が不足: {missing}. "
                "Agent Settings の Environment variable に設定する。"
            )

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict | None = None,
    ) -> dict:
        # 受け取った input_context はマージせず、**adapter が所有するキーだけで組み直す**
        # （Esports Tournament Dispute Adjudication Support Agent と同じ形）。理由は 2 つ:
        #
        # 1. `input_context` は S-2 の走査対象外
        #    （`_PII_SCAN_FIELDS = ("user_input","validated_input","llm_response")`）。
        #    runner が渡す `conversation_history` は全会話の自由文で、PII マスクも
        #    injection 検査も受けずに State へ入り、checkpointer を有効化すれば
        #    そのまま checkpoint に載る。本テンプレに消費者は無い。
        # 2. マージ形だと将来 runner がキーを増やしたとき素通りする。組み直しなら
        #    adapter が意図したキー以外は構造的に入らない。
        #
        # invoke 経路が input_context から読むのは mode と request_scope だけ
        # （`caller_id` / `decided_at` は feedback 経路専用で、この経路は到達しない。
        #  caller 識別子は `ctx` 経由で `state["caller_id"]` に入るため重複不要）。
        adapted = {
            "mode": MARKETPLACE_MODE,
            "request_scope": dict(self._scope),
        }
        return super().invoke(user_input, session_id=session_id, ctx=ctx, input_context=adapted)


def main() -> None:
    from shared.bootstrap.marketplace_app import run_agent_marketplace

    run_agent_marketplace(MarketplaceGraph, agent_name=AGENT_ID, namespace=NAMESPACE)


if __name__ == "__main__":
    main()
