# Marketplace entry adapter (cli.py) + LazyAzureOpenAILLM boundary tests.
#
# cli.py is a second entry adapter alongside src/api/server.py. It carries two
# security-relevant claims that no other test proves:
#
#   1. LazyAzureOpenAILLM holds no credential — it resolves the connection
#      values from the invocation-scoped SecretProvider on EVERY call. A future
#      "optimization" that caches the client on self would silently break this
#      and every other test would stay green.
#   2. resolve_scope() fails closed at process start on a misconfigured scope
#      env var, rather than degrading into a per-request rejection.
#
# The runner's own call shape is exercised too, since MarketplaceGraph.invoke()
# overrides a framework method and is otherwise only covered by manual runs.

from __future__ import annotations

import pathlib

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import bound_secrets

from src.entry_adapter.auth import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.services.azure_llm import REQUIRED_SECRET_KEYS, AzureConfigUnavailable, LazyAzureOpenAILLM

SCOPE_ENV = STG_DEFAULT_SCOPE_ENVS[SCOPE_KEYS[0]]


class _Provider(SecretProvider):
    def __init__(self, values: dict[str, str]) -> None:
        super().__init__()
        self._values = values

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)


def _azure_values(marker: str) -> dict[str, str]:
    return {
        "AZURE_OPENAI_API_KEY": f"key-{marker}",
        "AZURE_OPENAI_ENDPOINT": f"https://{marker}.services.ai.azure.com",
        "AZURE_OPENAI_DEPLOYMENT": f"deployment-{marker}",
    }


@pytest.fixture
def scoped_env(monkeypatch):
    monkeypatch.setenv(SCOPE_ENV, "TG-INT-001")
    return "TG-INT-001"


class TestLazyLlmResolvesPerCall:
    """The credential must come from the provider bound at call time."""

    def test_two_calls_under_different_providers_use_different_values(self, monkeypatch):
        seen: list[dict] = []

        class _RecordingClient:
            def __init__(self, config: dict) -> None:
                seen.append(dict(config))

            def complete(self, messages):
                return {"content": "", "tool_calls": [], "model": "x", "usage": {}}

        monkeypatch.setattr("src.services.azure_llm._load_client_class", lambda: _RecordingClient)

        llm = LazyAzureOpenAILLM()
        with bound_secrets(_Provider(_azure_values("first"))):
            llm.complete([{"role": "user", "content": "a"}])
        with bound_secrets(_Provider(_azure_values("second"))):
            llm.complete([{"role": "user", "content": "b"}])

        assert len(seen) == 2, "client must be rebuilt on every call, not cached"
        assert seen[0]["api_key"] == "key-first"
        assert (
            seen[1]["api_key"] == "key-second"
        ), "second call reused the first call's credential — the client is being cached"
        assert seen[0]["azure_deployment"] == "deployment-first"
        assert seen[1]["azure_deployment"] == "deployment-second"

    def test_no_credential_is_retained_on_the_instance(self, monkeypatch):
        monkeypatch.setattr(
            "src.services.azure_llm._load_client_class",
            lambda: (lambda config: type("C", (), {"complete": lambda self, m: {}})()),
        )
        llm = LazyAzureOpenAILLM()
        with bound_secrets(_Provider(_azure_values("first"))):
            llm.complete([{"role": "user", "content": "a"}])

        # 完全一致で見る。「repr に鍵が出ない客体を保持する」変異
        # （例: self._last_client = client）を許可キー検査で捕まえる。
        assert set(vars(llm)) == {
            "_passthrough",
            "_tools",
        }, f"unexpected attribute retained after a call: {sorted(vars(llm))}"
        assert not [v for v in vars(llm).values() if "key-first" in repr(v)]

    def test_unbound_secrets_raises_instead_of_building_a_half_client(self):
        with pytest.raises(AzureConfigUnavailable) as exc:
            LazyAzureOpenAILLM()._build_client()
        for key in REQUIRED_SECRET_KEYS:
            assert key in str(exc.value)

    @pytest.mark.parametrize("key", ["api_key", "azure_endpoint", "azure_deployment"])
    def test_connection_values_cannot_be_passed_through(self, key):
        # passthrough に接続値を通せると (a) インスタンスに永続保持され
        # (b) **self._passthrough が後勝ちで呼び出しスコープの解決値を上書きする。
        with pytest.raises(ValueError) as exc:
            LazyAzureOpenAILLM(**{key: "x"})
        assert key in str(exc.value)

    @pytest.mark.parametrize("missing", REQUIRED_SECRET_KEYS)
    def test_each_connection_value_is_individually_required(self, missing):
        values = _azure_values("first")
        values.pop(missing)
        with bound_secrets(_Provider(values)):
            with pytest.raises(AzureConfigUnavailable) as exc:
                LazyAzureOpenAILLM()._build_client()
        assert missing in str(exc.value)

    def test_stream_also_resolves_through_the_client(self, monkeypatch):
        built = []

        class _C:
            def __init__(self, config):
                built.append(config["api_key"])

            def stream(self, messages):
                yield "chunk"

        monkeypatch.setattr("src.services.azure_llm._load_client_class", lambda: _C)
        with bound_secrets(_Provider(_azure_values("first"))):
            assert list(LazyAzureOpenAILLM().stream([])) == ["chunk"]
        assert built == ["key-first"]

    def test_bind_tools_carries_the_tools_into_the_client(self, monkeypatch):
        bound_with = []

        class _C:
            def __init__(self, config):
                pass

            def bind_tools(self, tools):
                bound_with.append(tools)
                return self

            def complete(self, messages):
                return {}

        monkeypatch.setattr("src.services.azure_llm._load_client_class", lambda: _C)
        llm = LazyAzureOpenAILLM().bind_tools([{"name": "t"}])
        with bound_secrets(_Provider(_azure_values("first"))):
            llm.complete([])
        assert bound_with == [[{"name": "t"}]], "bind_tools の内容が client に届いていない"

    def test_bind_tools_returns_a_new_instance(self):
        llm = LazyAzureOpenAILLM()
        bound = llm.bind_tools([{"name": "t"}])
        assert bound is not llm, "bind_tools must not mutate a shared instance"
        assert llm._tools == ()


class TestResolveScopeFailsClosed:
    @pytest.mark.parametrize(
        "value",
        ["", "   ", " TG-INT-001", "TG-INT-001 ", "TG\nINT-001", "TG\rINT-001"],
        ids=["empty", "blank", "leading-space", "trailing-space", "newline", "carriage-return"],
    )
    def test_malformed_scope_env_is_rejected_at_boot(self, monkeypatch, value):
        import cli

        monkeypatch.setenv(SCOPE_ENV, value)
        with pytest.raises(RuntimeError) as exc:
            cli.resolve_scope()
        assert SCOPE_ENV in str(exc.value)

    def test_absent_scope_env_is_rejected_at_boot(self, monkeypatch):
        import cli

        monkeypatch.delenv(SCOPE_ENV, raising=False)
        with pytest.raises(RuntimeError):
            cli.resolve_scope()

    def test_clean_value_resolves(self, monkeypatch, scoped_env):
        import cli

        assert cli.resolve_scope() == {SCOPE_KEYS[0]: scoped_env}


class TestMarketplaceGraphInjectsAdapterContext:
    """The runner supplies neither mode nor scope; the adapter must."""

    def _captured_invoke(self, monkeypatch):
        import cli

        captured: dict = {}

        def _fake_invoke(self, user_input, session_id="", ctx=None, input_context=None):
            captured["user_input"] = user_input
            captured["session_id"] = session_id
            captured["ctx"] = ctx
            captured["input_context"] = input_context
            return {"status": "success", "output": {}}

        monkeypatch.setattr(cli.InterconnectIncidentHandoverGraph, "invoke", _fake_invoke)
        return cli, captured

    def test_mode_and_scope_are_injected_and_history_is_dropped(self, monkeypatch, scoped_env):
        cli, captured = self._captured_invoke(monkeypatch)
        agent = cli.MarketplaceGraph()
        ctx = InvocationContext(caller_id="u-1", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)

        # Exactly the shape shared/bootstrap/marketplace_app.py uses.
        agent.invoke(
            "{}", ctx=ctx, input_context={"conversation_history": [{"role": "user", "content": "私は山田太郎です"}]}
        )

        ic = captured["input_context"]
        assert ic["mode"] == cli.MARKETPLACE_MODE == "invoke"
        assert ic["request_scope"] == {SCOPE_KEYS[0]: scoped_env}
        # S-2 does not scan input_context, and this template consumes nothing else
        # from it — the adapter rebuilds the dict rather than merging, so neither
        # the raw history nor any future runner-supplied key can ride into State.
        assert set(ic) == {"mode", "request_scope"}, f"unexpected keys reached State: {sorted(ic)}"
        # ctx を落とす変異は S-1 で全拒否になる（ANONYMOUS 扱い）。素通しを固定する。
        assert captured["ctx"] is ctx
        assert captured["user_input"] == "{}"

    def test_caller_supplied_mode_and_scope_cannot_win(self, monkeypatch, scoped_env):
        cli, captured = self._captured_invoke(monkeypatch)
        agent = cli.MarketplaceGraph()

        agent.invoke(
            "{}",
            ctx=InvocationContext(caller_id="u-1"),
            input_context={
                "mode": "ingest",
                "request_scope": {SCOPE_KEYS[0]: "OTHER-CARRIER"},
                "some_future_runner_key": "x",
            },
        )

        ic = captured["input_context"]
        assert ic["mode"] == "invoke", "a write mode must not be reachable from this entry"
        assert ic["request_scope"] == {SCOPE_KEYS[0]: scoped_env}, "scope must not be caller-controlled"
        assert "some_future_runner_key" not in ic, "unexpected keys must not survive"

    def test_default_llm_seam_is_the_lazy_client(self, monkeypatch, scoped_env):
        import cli

        agent = cli.MarketplaceGraph()
        assert isinstance(agent.config.get("llm"), LazyAzureOpenAILLM)

    def test_scope_failure_prevents_construction(self, monkeypatch):
        import cli

        monkeypatch.delenv(SCOPE_ENV, raising=False)
        with pytest.raises(RuntimeError):
            cli.MarketplaceGraph()


class TestMainWiresTheAdapter:
    def test_main_passes_the_adapter_subclass_to_the_runner(self, monkeypatch):
        import sys
        import types

        import cli

        captured = {}

        def _fake_runner(agent_cls, *, agent_name, namespace="default", **kw):
            captured["agent_cls"] = agent_cls
            captured["agent_name"] = agent_name
            captured["namespace"] = namespace

        module = types.ModuleType("shared.bootstrap.marketplace_app")
        module.run_agent_marketplace = _fake_runner
        pkg = types.ModuleType("shared.bootstrap")
        pkg.marketplace_app = module
        monkeypatch.setitem(sys.modules, "shared.bootstrap", pkg)
        monkeypatch.setitem(sys.modules, "shared.bootstrap.marketplace_app", module)

        cli.main()

        # 元 Graph を渡す変異は adapter 全体（mode / scope / LLM 注入）を迂回する。
        assert captured["agent_cls"] is cli.MarketplaceGraph
        assert captured["agent_name"] == cli.AGENT_ID
        assert captured["namespace"] == cli.NAMESPACE


class TestModuleImportIsCiSafe:
    def test_cli_imports_without_the_marketplace_extra(self):
        # agenticstar_platform is only imported inside main(); CI installs no
        # marketplace extra, so a module-level import would break collection.
        import cli

        assert cli.AGENT_ID == "TEL-C2-113"
        source = pathlib.Path(cli.__file__).read_text(encoding="utf-8")
        assert "agenticstar_platform" not in source.split("def main(")[0]
