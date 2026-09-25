"""Router: client alias wins; system prompt does not force expensive."""
from edge.config import GatewayConfig, ModelsConfig, ModelSpec, RouterConfig
from edge.router import pick_model


def _cfg() -> GatewayConfig:
    return GatewayConfig(
        listen="127.0.0.1:0",
        upstream_mode="mock",
        models=ModelsConfig(
            cheap=[ModelSpec(name="ollama-cheap")],
            expensive=[ModelSpec(name="openai-expensive")],
        ),
        router=RouterConfig(
            strategy="heuristic_cost_first",
            expensive_keywords=["refactor", "complex", "architecture"],
            expensive_min_chars=2000,
        ),
    )


def test_client_alias_wins_over_keyword_heuristic():
    body = {
        "model": "ollama-cheap",
        "system": "You help refactor complex architecture.",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 32000,
    }
    d = pick_model(body, _cfg(), client_model="ollama-cheap")
    assert d.model == "ollama-cheap"
    assert d.pool == "cheap"
    assert "client_model" in d.reason


def test_system_keywords_do_not_force_expensive():
    body = {
        "model": "ignored",
        "system": "Please refactor the complex architecture carefully.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    d = pick_model(body, _cfg(), client_model=None)
    assert d.pool == "cheap"
    assert d.model == "ollama-cheap"
