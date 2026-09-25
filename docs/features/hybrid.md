# Feature: Hybrid pools (Ollama cheap + OpenAI expensive)

**Status:** **Shipped (default).** `config/gateway.yaml` uses local **Ollama** for the cheap pool and **OpenAI** for the expensive pool. Everyday Ollama (cheap chat, Anthropic/Responses translate, streaming) works **without** an OpenAI key. Export `OPENAI_API_KEY` when you want heuristic/expensive requests to succeed against OpenAI.

## Default pools

| Pool | Alias | LiteLLM model | Needs key? |
| --- | --- | --- | --- |
| cheap | `ollama-cheap` | `ollama_chat/llama3.2:1b` + `api_base` Ollama | No |
| expensive | `openai-expensive` | `openai/gpt-4o-mini` | Yes — `OPENAI_API_KEY` |

Default expensive id is `openai/gpt-4o-mini` (strong, lower cost than `gpt-4o`). Bump via `litellm_model: openai/gpt-4o` in `config/gateway.yaml`. See [LiteLLM OpenAI](https://docs.litellm.ai/docs/providers/openai).

Optional alternatives in YAML comments: Grok (`xai/grok-3-mini` + `XAI_API_KEY`) or all-Ollama expensive (no cloud key).

## How the key is wired

- LiteLLM (and Harbor’s generator) use env **`OPENAI_API_KEY`**. Do not put raw keys in YAML.
- `edge/litellm_gen.py` auto-sets `api_key: os.environ/OPENAI_API_KEY` on any `openai/*` model (and `XAI_API_KEY` for `xai/*`) unless you override via `extra_litellm_params`.
- `python -m edge start` **warns** (does not fail) if any `openai/*` model is configured and `OPENAI_API_KEY` is unset.

## Optional all-Ollama expensive

Comment out the OpenAI entry in `config/gateway.yaml` and use the commented `ollama-expensive` / `ollama_chat/llama3.2:3b` block instead (no cloud key). Set `cost_estimates_usd.expensive_per_request` back to `0.0` if you want.

## Smoke

`./scripts/smoke.sh` always requires cheap + stream (+ ledger). For the expensive-keyword prompt:

- **`OPENAI_API_KEY` unset** — soft-**SKIP** upstream completion success; still asserts ledger recorded expensive routing.
- **`OPENAI_API_KEY` set** — asserts successful expensive completion + ledger.

Mock mode (`GATEWAY_CONFIG=config/gateway.mock.yaml`) always asserts expensive success (no cloud).

## Mac quick path

```bash
cd ~/github/harbor-ai-gateway && source .venv/bin/activate
ollama pull llama3.2:1b
# optional for OpenAI expensive:
# export OPENAI_API_KEY=...   # from platform.openai.com — never commit
python -m edge stop
python -m edge start
./scripts/smoke.sh
```

See also [RUN_OLLAMA.md](../../RUN_OLLAMA.md) and [streaming.md](streaming.md).
