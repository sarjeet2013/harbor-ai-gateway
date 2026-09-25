# Feature: Hybrid pools (Ollama cheap + Grok expensive)

**Status:** **Shipped (default).** `config/gateway.yaml` uses local **Ollama** for the cheap pool and **xAI Grok** for the expensive pool. Everyday Ollama (cheap chat, Anthropic/Responses translate, streaming) works **without** a Grok key. Export `XAI_API_KEY` when you want heuristic/expensive requests to succeed against Grok.

## Default pools

| Pool | Alias | LiteLLM model | Needs key? |
| --- | --- | --- | --- |
| cheap | `ollama-cheap` | `ollama_chat/llama3.2:1b` + `api_base` Ollama | No |
| expensive | `grok-expensive` | `xai/grok-3-mini` | Yes — `XAI_API_KEY` |

Model id is mid-tier `xai/grok-3-mini` (widely available). To bump later, edit `litellm_model` to a newer `xai/grok-4.x`, or fall back to `xai/grok-2-latest` if needed. See [LiteLLM xAI](https://docs.litellm.ai/docs/providers/xai).

## How the key is wired

- LiteLLM (and Harbor’s generator) use env **`XAI_API_KEY`**. Do not put raw keys in YAML.
- `edge/litellm_gen.py` auto-sets `api_key: os.environ/XAI_API_KEY` on any `xai/*` model unless you override via `extra_litellm_params`.
- `python -m edge start` **warns** (does not fail) if any `xai/*` model is configured and `XAI_API_KEY` is unset.

## Optional all-Ollama expensive

Comment out the Grok entry in `config/gateway.yaml` and use the commented `ollama-expensive` / `ollama_chat/llama3.2:3b` block instead (no cloud key). Set `cost_estimates_usd.expensive_per_request` back to `0.0` if you want.

## Smoke

`./scripts/smoke.sh` always requires cheap + stream (+ ledger). For the expensive-keyword prompt:

- **`XAI_API_KEY` unset** — soft-**SKIP** upstream completion success; still asserts ledger recorded expensive routing.
- **`XAI_API_KEY` set** — asserts successful expensive completion + ledger.

Mock mode (`GATEWAY_CONFIG=config/gateway.mock.yaml`) always asserts expensive success (no cloud).

## Mac quick path

```bash
cd ~/harbor-ai-gateway && source .venv/bin/activate
ollama pull llama3.2:1b
# optional for Grok expensive:
# export XAI_API_KEY=...   # from https://console.x.ai — never commit
python -m edge stop
python -m edge start --detach
./scripts/smoke.sh
```

See also [RUN_OLLAMA.md](../../RUN_OLLAMA.md) and [streaming.md](streaming.md).
