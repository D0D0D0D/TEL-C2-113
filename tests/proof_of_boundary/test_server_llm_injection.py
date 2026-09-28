# server.py must build its secrets provider before Graph() and probe the Azure
# keys with .get() (never .require()), so an unprovisioned environment degrades
# to config={"llm": None} instead of crashing the process at import —
# deploy-stg provisions no Azure config today.
#
# Both entry paths share one LLM seam: this adapter and cli.py's Marketplace
# adapter both inject LazyAzureOpenAILLM, which resolves the connection values
# per call. No credential is held on a module global.

import importlib

_FULL_AZURE_CONFIG = {
    "AZURE_OPENAI_API_KEY": "dummy-test-key",
    "AZURE_OPENAI_ENDPOINT": "https://example-resource.services.ai.azure.com",
    "AZURE_OPENAI_DEPLOYMENT": "dummy-deployment",
}


class TestServerBootsWithoutAzureConfig:
    def test_server_imports_and_degrades_to_no_llm(self, monkeypatch):
        from shared.secrets.inmemory_provider import InMemoryProvider

        monkeypatch.setattr(
            "shared.secrets.factory",
            lambda namespace, agent_name: InMemoryProvider({}),
        )

        import src.api.server as server

        importlib.reload(server)

        assert server.app is not None
        assert server.agent is not None
        # Deterministic fallback, not a crash and not a half-built client.
        assert server._llm is None
        assert server.agent._nodes["main"]._llm is None


class TestServerConstructsLlmWithAzureConfig:
    def test_llm_is_constructed_and_reaches_main_node(self, monkeypatch):
        from shared.secrets.inmemory_provider import InMemoryProvider

        monkeypatch.setattr(
            "shared.secrets.factory",
            lambda namespace, agent_name: InMemoryProvider(_FULL_AZURE_CONFIG),
        )
        # The registry-built wheel that CI installs has no azure_openai_client
        # module; pretend it is present so this test covers the happy path.
        monkeypatch.setattr("src.services.azure_llm.client_available", lambda: True)

        import src.api.server as server

        importlib.reload(server)

        from src.services.azure_llm import LazyAzureOpenAILLM

        assert isinstance(server._llm, LazyAzureOpenAILLM)
        assert server.agent._nodes["main"]._llm is server._llm

    def test_config_present_but_client_absent_degrades(self, monkeypatch):
        """The CI / STG situation: keys supplied, Azure client not in the wheel.

        Must fall back to the documented llm=None degradation rather than
        injecting a client that raises ImportError on the first LLM call.
        """
        from shared.secrets.inmemory_provider import InMemoryProvider

        monkeypatch.setattr(
            "shared.secrets.factory",
            lambda namespace, agent_name: InMemoryProvider(_FULL_AZURE_CONFIG),
        )
        monkeypatch.setattr("src.services.azure_llm.client_available", lambda: False)

        import src.api.server as server

        importlib.reload(server)

        assert server._llm is None
        assert server.agent._nodes["main"]._llm is None

    def test_partial_azure_config_degrades_instead_of_half_building(self, monkeypatch):
        # A missing deployment must not yield a client that fails at call time
        # deep inside the graph — the seam stays None so the documented
        # llm_not_configured degradation fires instead.
        from shared.secrets.inmemory_provider import InMemoryProvider

        monkeypatch.setattr(
            "shared.secrets.factory",
            lambda namespace, agent_name: InMemoryProvider(
                {
                    "AZURE_OPENAI_API_KEY": "dummy-test-key",
                    "AZURE_OPENAI_ENDPOINT": "https://example-resource.services.ai.azure.com",
                }
            ),
        )

        import src.api.server as server

        importlib.reload(server)

        assert server._llm is None


class TestStandaloneTrustPromotion:
    def test_external_bearer_never_promotes_to_internal(self):
        import src.api.server as server
        from framework.schemas.trust_level import TrustLevel

        assert (
            server._resolve_standalone_trust(TrustLevel.ANONYMOUS, "Bearer external", "external", "runner")
            is TrustLevel.VERIFIED_EXTERNAL
        )

    def test_runner_bearer_promotes_to_internal(self):
        import src.api.server as server
        from framework.schemas.trust_level import TrustLevel

        assert (
            server._resolve_standalone_trust(TrustLevel.ANONYMOUS, "Bearer runner", "external", "runner")
            is TrustLevel.INTERNAL
        )

    def test_wrong_or_missing_bearer_is_rejected_when_auth_is_enabled(self):
        import pytest
        import src.api.server as server
        from fastapi import HTTPException
        from framework.schemas.trust_level import TrustLevel

        for authorization in ("", "Bearer wrong"):
            with pytest.raises(HTTPException) as exc:
                server._resolve_standalone_trust(TrustLevel.ANONYMOUS, authorization, "external", "runner")
            assert exc.value.status_code == 401
