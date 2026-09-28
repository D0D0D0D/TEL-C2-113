"""Azure OpenAI クライアントの遅延解決ラッパー（Marketplace 経路用）。

Marketplace runner は `agent_cls()` を引数なしで生成し、その **後** に
`provision_secrets()` を呼ぶ（`shared/bootstrap/marketplace_app.py`）。
そのため Graph 構築時点では `ctx.secrets` も `SecretProvider` も未束縛で、
`AzureOpenAIClient` をその場で組むことができない。

このラッパーは `config["llm"]` の注入シームをそのまま使いながら、実際の
接続値の解決を `complete()` / `stream()` の呼び出し時点まで遅らせる。
呼び出し時点では runner が `bound_secrets(...)` でプロバイダを束縛済みで、
`current_secrets()` は `InvocationContext.from_state()` が `ctx.secrets` に
入れるものと同一（`framework/schemas/invocation_context.py`）。

- API キーは保持しない（呼び出しごとに解決して即破棄する。State にも載らない）
- `langchain_openai` に依存する `AzureOpenAIClient` の import はメソッド内に
  置く。CI 側の解決結果に依存せずモジュール収集が通るようにするため
"""

from __future__ import annotations

from typing import Any, Iterator

from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

# Marketplace の Agent Settings（User-defined Environment Variables）で与える。
# EnvProvider は namespace/agent_name でスコープせずこの名前のまま読む。
REQUIRED_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)

#: `AzureOpenAIClient(config)` 側のキー名（passthrough で渡させないための拒否リスト）
REQUIRED_SECRET_KEYS_LOWER = ("api_key", "azure_endpoint", "azure_deployment")


def _load_client_class() -> Any:
    """`AzureOpenAIClient` を遅延 import して返す（差し替えシーム）。

    import をここに閉じ込める理由は 2 つ:
    - `langchain_openai` の解決可否にモジュール収集を依存させない
    - **wheel の版によっては `azure_openai_client` モジュール自体が存在しない**。
      module level で import すると、その環境では収集時点で壊れる

    テストはこの関数を patch する。実クラスを要求しないため CI でも実行できる。
    """
    from shared.services.llm.azure_openai_client import AzureOpenAIClient

    return AzureOpenAIClient


def client_available() -> bool:
    """`AzureOpenAIClient` がこの環境に存在するか。

    wheel の版によっては当該モジュールが無い。
    接続値が揃っていても import で落ちるため、**注入する前に**ここで判定し、
    HTTP エントリは既存の `llm=None` 縮退契約へ落とす（呼び出しごとの
    ImportError にしない）。
    """
    try:
        _load_client_class()
    except ImportError:
        return False
    return True


class AzureConfigUnavailable(RuntimeError):
    """接続値が解決できない。値そのものはメッセージに載せない。"""


class LazyAzureOpenAILLM(BaseLLM):
    """呼び出しごとに `AzureOpenAIClient` を組み直す `BaseLLM` 実装。"""

    #: 接続値は `_build_client()` が secrets から解決する。ここに載せると
    #: インスタンスに永続保持され（キー非保持の契約違反）、さらに `**self._passthrough`
    #: が後勝ちで呼び出しスコープの値を上書きしてしまう。
    _FORBIDDEN_PASSTHROUGH = frozenset(REQUIRED_SECRET_KEYS_LOWER)

    def __init__(self, **passthrough: Any) -> None:
        # temperature / max_tokens / timeout など AzureOpenAIClient が
        # 素通しするキーのみを想定した静的設定。可変状態は持たない。
        forbidden = sorted(self._FORBIDDEN_PASSTHROUGH & set(passthrough))
        if forbidden:
            raise ValueError(
                f"LazyAzureOpenAILLM: {forbidden} は passthrough で渡せない。"
                "接続値は呼び出しごとに secrets から解決する。"
            )
        self._passthrough = dict(passthrough)
        self._tools: tuple[Any, ...] = ()

    def _build_client(self) -> Any:
        # 接続値の検査を import より先に置く。逆にすると、秘密が無いだけの環境で
        # `AzureConfigUnavailable` ではなく import エラーが出て原因を取り違える
        # （CI が install する registry 版 wheel には azure_openai_client が無い）。
        secrets = current_secrets()
        resolved = {key: secrets.get(key) for key in REQUIRED_SECRET_KEYS}
        missing = [key for key, value in resolved.items() if not value]
        if missing:
            raise AzureConfigUnavailable(
                f"LazyAzureOpenAILLM: {missing} が解決できない。" "Agent Settings の Environment variable を確認する。"
            )
        client = _load_client_class()(
            {
                "api_key": resolved["AZURE_OPENAI_API_KEY"],
                "azure_endpoint": resolved["AZURE_OPENAI_ENDPOINT"],
                "azure_deployment": resolved["AZURE_OPENAI_DEPLOYMENT"],
                **self._passthrough,
            }
        )
        return client.bind_tools(list(self._tools)) if self._tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return self._build_client().complete(messages)

    def stream(self, messages: list[Any]) -> Iterator[str]:
        return self._build_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "LazyAzureOpenAILLM":
        # インスタンスを共有したまま可変状態を持たないよう、束縛済みの
        # 新インスタンスを返す（BaseLLM の「self または束縛済みインスタンス」契約）。
        bound = LazyAzureOpenAILLM(**self._passthrough)
        bound._tools = tuple(tools)
        return bound


__all__ = ["AzureConfigUnavailable", "LazyAzureOpenAILLM", "REQUIRED_SECRET_KEYS", "client_available"]
