"""Router: client alias wins; task-class within cheap; system prompt does not force expensive."""
from edge.config import GatewayConfig, ModelsConfig, ModelSpec, RouterConfig
from edge.router import pick_model


def _cfg() -> GatewayConfig:
    return GatewayConfig(
        listen="127.0.0.1:0",
        upstream_mode="mock",
        models=ModelsConfig(
            cheap=[
                ModelSpec(name="ollama-cheap"),
                ModelSpec(name="ollama-chat"),
                ModelSpec(name="ollama-coder"),
                ModelSpec(name="ollama-reason"),
            ],
            expensive=[ModelSpec(name="openai-expensive")],
        ),
        router=RouterConfig(
            strategy="heuristic_cost_first",
            expensive_keywords=["refactor", "complex", "architecture"],
            expensive_min_chars=2000,
            coder_keywords=["write a function", "implement", "unit test", "fix the bug"],
            reason_keywords=["step by step", "reason about", "prove that", "math problem"],
            default_cheap_model="ollama-cheap",
            coder_model="ollama-coder",
            reason_model="ollama-reason",
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
    assert d.reason == "client:model"


def test_client_coder_alias_wins():
    body = {
        "messages": [{"role": "user", "content": "please refactor the architecture"}],
    }
    d = pick_model(body, _cfg(), client_model="ollama-coder")
    assert d.model == "ollama-coder"
    assert d.pool == "cheap"
    assert d.reason == "client:model"


def test_client_expensive_alias_wins():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    d = pick_model(body, _cfg(), client_model="openai-expensive")
    assert d.model == "openai-expensive"
    assert d.pool == "expensive"
    assert d.reason == "client:model"


def test_system_keywords_do_not_force_expensive():
    body = {
        "model": "ignored",
        "system": "Please refactor the complex architecture carefully.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    d = pick_model(body, _cfg(), client_model=None)
    assert d.pool == "cheap"
    assert d.model == "ollama-cheap"
    assert d.reason == "heuristic:default_cheap"


def test_coder_keyword_selects_ollama_coder():
    body = {
        "messages": [{"role": "user", "content": "Please write a function to parse JSON."}],
    }
    d = pick_model(body, _cfg(), client_model=None)
    assert d.pool == "cheap"
    assert d.model == "ollama-coder"
    assert d.reason == "heuristic:coder_keyword"


def test_reason_keyword_selects_ollama_reason():
    body = {
        "messages": [{"role": "user", "content": "Solve this math problem step by step."}],
    }
    d = pick_model(body, _cfg(), client_model=None)
    assert d.pool == "cheap"
    assert d.model == "ollama-reason"
    assert d.reason == "heuristic:reason_keyword"


def test_default_cheap_when_no_task_match():
    body = {
        "messages": [{"role": "user", "content": "Say hello in one word."}],
    }
    d = pick_model(body, _cfg(), client_model=None)
    assert d.pool == "cheap"
    assert d.model == "ollama-cheap"
    assert d.reason == "heuristic:default_cheap"


def test_expensive_keyword_still_expensive():
    body = {
        "messages": [{"role": "user", "content": "Please refactor this module."}],
    }
    d = pick_model(body, _cfg(), client_model=None)
    assert d.pool == "expensive"
    assert d.model == "openai-expensive"
    assert d.reason == "heuristic:expensive_keyword"


def test_unknown_client_model_does_not_pin():
    """Claude Code default model ids are not Harbor aliases — heuristics apply."""
    body = {
        "messages": [{"role": "user", "content": "Please implement a binary search."}],
    }
    d = pick_model(body, _cfg(), client_model="claude-sonnet-4")
    assert d.pool == "cheap"
    assert d.model == "ollama-coder"
    assert d.reason == "heuristic:coder_keyword"


def test_default_cheap_by_name_not_first_14b():
    """If ollama-cheap is not models[0], default still picks by name."""
    cfg = _cfg()
    cfg.models.cheap = [
        ModelSpec(name="ollama-coder"),
        ModelSpec(name="ollama-reason"),
        ModelSpec(name="ollama-cheap"),
    ]
    body = {"messages": [{"role": "user", "content": "hi"}]}
    d = pick_model(body, cfg, client_model=None)
    assert d.model == "ollama-cheap"
    assert d.reason == "heuristic:default_cheap"


def test_short_last_user_ignores_long_prior_transcript():
    """Heuristic scores last user turn only — prior implement/length must not route."""
    prior_code = (
        "Please implement a binary search and write a function with unit test.\n"
        + ("x" * 2500)
    )
    body = {
        "messages": [
            {"role": "system", "content": "You help refactor complex architecture."},
            {"role": "user", "content": prior_code},
            {"role": "assistant", "content": "Here is an implementation with tests."},
            {"role": "user", "content": "good morning"},
        ],
    }
    d = pick_model(body, _cfg(), client_model=None)
    assert d.pool == "cheap"
    assert d.model == "ollama-cheap"
    assert d.reason == "heuristic:default_cheap"


def test_last_user_coder_keyword_still_triggers():
    """Prior chat history does not block last-user coder keyword."""
    body = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "Please implement a binary search."},
        ],
    }
    d = pick_model(body, _cfg(), client_model=None)
    assert d.pool == "cheap"
    assert d.model == "ollama-coder"
    assert d.reason == "heuristic:coder_keyword"


def test_last_user_min_chars_still_triggers_expensive():
    long_last = "Please help with this: " + ("y" * 2000)
    body = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": long_last},
        ],
    }
    d = pick_model(body, _cfg(), client_model=None)
    assert d.pool == "expensive"
    assert d.model == "openai-expensive"
    assert d.reason == "heuristic:expensive_min_chars"

