# Feature: Hybrid pools (Ollama cheap + OpenAI expensive)

**Status:** **Shipped (default).** `config/gateway.yaml` uses local **Ollama** for the cheap pool and **OpenAI** for the expensive pool. Everyday Ollama (cheap chat, Anthropic/Responses translate, streaming) works **without** an OpenAI key. Export `OPENAI_API_KEY` when you want heuristic/expensive requests to succeed against OpenAI.

## Default pools

| Pool | Alias | LiteLLM model | Needs key? |
| --- | --- | --- | --- |
| cheap | `ollama-cheap` (default chat) | `ollama_chat/qwen3:8b` + Mini `api_base` (`think: false`) | No |
| cheap | `ollama-chat` | same 8b (duplicate chat alias) | No |
| cheap | `ollama-coder` | `ollama_chat/qwen2.5-coder:14b` @ Mini | No |
| cheap | `ollama-reason` | `ollama_chat/deepseek-r1:14b` @ Mini | No |
| expensive | `openai-expensive` | `openai/gpt-4o-mini` | Yes — `OPENAI_API_KEY` |

Task-class routing within cheap (and Claude Code tips): [routing.md](routing.md). **VRAM:** avoid loading two 14Bs at once.

Default expensive id is `openai/gpt-4o-mini` (strong, lower cost than `gpt-4o`). Bump via `litellm_model: openai/gpt-4o` in `config/gateway.yaml`. See [LiteLLM OpenAI](https://docs.litellm.ai/docs/providers/openai).

Optional alternatives in YAML comments: Grok (`xai/grok-3-mini` + `XAI_API_KEY`) or all-Ollama expensive (no cloud key).

## How the key is wired

Harbor authenticates *clients* with a local bearer (e.g. `sk-local-gateway`). That header is **not** forwarded to LiteLLM — otherwise LiteLLM would send it to OpenAI as the provider key and fail. Provider auth is only `OPENAI_API_KEY` in the LiteLLM process env (or `.env` loaded on `edge start`).


`python -m edge start` loads repo-root `.env` (shell exports win) and passes the env to LiteLLM. Copy `.env.example` → `.env` and set `OPENAI_API_KEY`.


- LiteLLM (and Harbor’s generator) use env **`OPENAI_API_KEY`**. Do not put raw keys in YAML.
- `edge/litellm_gen.py` auto-sets `api_key: os.environ/OPENAI_API_KEY` on any `openai/*` model (and `XAI_API_KEY` for `xai/*`) unless you override via `extra_litellm_params`.
- `python -m edge start` **warns** (does not fail) if any `openai/*` model is configured and `OPENAI_API_KEY` is unset.

## Optional all-Ollama expensive

Comment out the OpenAI entry in `config/gateway.yaml` and use the commented `ollama-expensive` block instead (point `api_base` at the same Ollama host; no cloud key). Set `cost_estimates_usd.expensive_per_request` back to `0.0` if you want.


## CLI: list / set model pools

`config/gateway.yaml` **`models.cheap` / `models.expensive`** remain the source of truth (no admin UI). Open fields for the CLI are **model id** (`litellm_model`) and **`api_base`**, plus optional enable/disable on the pool’s primary entry.

```bash
# Show current pools
python -m edge models
# same: python -m edge models list

# Point cheap at Mini Ollama (short id → ollama_chat/… when --api-base is set)
python -m edge models set --pool cheap --model qwen3:8b --api-base http://192.168.4.31:11434

# Bump expensive OpenAI model (no api_base → openai/… prefix)
python -m edge models set --pool expensive --model gpt-4o-mini
# or full LiteLLM id: --model openai/gpt-4o

# Optional: disable a pool’s primary entry (skipped by LiteLLM gen + router names)
python -m edge models set --pool expensive --disable

# Dry-run (no write)
python -m edge models set --pool cheap --model qwen3:8b --api-base http://192.168.4.31:11434 --dry-run
```

After a successful write, restart so edge + LiteLLM reload config:

```bash
python -m edge stop && python -m edge start
```

`models set` rewrites YAML with PyYAML (**inline comments are not preserved**). Prefer `config/gateway.yaml.example` as the annotated template, or hand-edit `gateway.yaml` when you need comments.

`python -m edge doctor` soft-probes each configured Ollama `api_base` with `GET /api/tags` (reachable + model names). If Mini is down it **warns** only — doctor still succeeds when deps are OK.

## Smoke

`./scripts/smoke.sh` always requires cheap + stream (+ ledger). For the expensive-keyword prompt:

- **`OPENAI_API_KEY` unset** — soft-**SKIP** upstream completion success; still asserts ledger recorded expensive routing.
- **`OPENAI_API_KEY` set** — asserts successful expensive completion + ledger.

Mock mode (`GATEWAY_CONFIG=config/gateway.mock.yaml`) always asserts expensive success (no cloud).

## Mac quick path

Default cheap pool points at **Mac Mini Ollama on LAN** (`qwen3:8b` @ `http://192.168.4.31:11434`). On the Mini: `OLLAMA_HOST=0.0.0.0:11434` so it listens on the LAN, and `ollama pull qwen3:8b` once. Air local Ollama can stay on `127.0.0.1` for other use.

```bash
cd ~/github/harbor-ai-gateway && source .venv/bin/activate
# Confirm Mini is reachable from Air:
curl -s http://192.168.4.31:11434/api/tags | head
# optional for OpenAI expensive:
# export OPENAI_API_KEY=...   # from platform.openai.com — never commit
python -m edge stop
python -m edge start
./scripts/smoke.sh
```

See also [RUN_OLLAMA.md](../../RUN_OLLAMA.md) and [streaming.md](streaming.md).

## Troubleshooting: OpenAI "Connection error"

If smoke/expensive fails with LiteLLM `OpenAIException - Connection error` while a direct
`httpx` call to `api.openai.com` works, check LiteLLM logs for
`SSLCertVerificationError` / `CERTIFICATE_VERIFY_FAILED`.

LiteLLM's proxy uses **aiohttp** for OpenAI. On many macOS Python installs the default
CA store is empty, so TLS fails. Harbor's `python -m edge start` sets `SSL_CERT_FILE` and
`REQUESTS_CA_BUNDLE` to **certifi**'s bundle when unset. Restart after pulling that fix:

```bash
python -m edge stop
python -m edge start --detach
./scripts/smoke.sh
```

You can also export those yourself before start. Do not disable TLS verification.

