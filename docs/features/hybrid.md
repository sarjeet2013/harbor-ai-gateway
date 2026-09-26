# Feature: Hybrid pools (Ollama cheap + OpenAI expensive)

**Status:** **Shipped (default).** Cheap pool = **Ollama**; expensive pool = **OpenAI**. Everyday Ollama (cheap chat, Anthropic/Responses translate, streaming) works **without** an OpenAI key. Set `OPENAI_API_KEY` when you want heuristic/expensive requests to succeed against OpenAI.

## Default pools

| Pool | Alias | LiteLLM model | Needs key? |
| --- | --- | --- | --- |
| cheap | `ollama-cheap` (default chat) | `ollama_chat/qwen3:8b` + `api_base` (`think: false`) | No |
| cheap | `ollama-chat` | same 8b (duplicate chat alias) | No |
| cheap | `ollama-coder` | `ollama_chat/qwen2.5-coder:14b` | No |
| cheap | `ollama-reason` | `ollama_chat/deepseek-r1:14b` | No |
| expensive | `openai-expensive` | `openai/gpt-4o-mini` | Yes — `OPENAI_API_KEY` |

Task-class routing within cheap (and Claude Code tips): [routing.md](routing.md). **VRAM:** avoid loading two 14Bs at once on one GPU host.

Default expensive id is `openai/gpt-4o-mini`. Bump via `litellm_model: openai/gpt-4o` in `config/gateway.yaml`. See [LiteLLM OpenAI](https://docs.litellm.ai/docs/providers/openai).

Optional alternatives in YAML comments: Grok (`xai/grok-3-mini` + `XAI_API_KEY`) or all-Ollama expensive (no cloud key).

## How the key is wired

Harbor authenticates *clients* with a local bearer (e.g. `sk-local-gateway`). That header is **not** forwarded to LiteLLM — otherwise LiteLLM would send it to OpenAI as the provider key and fail. Provider auth is only `OPENAI_API_KEY` in the LiteLLM process env (or `.env` loaded on `edge start`). See [../SECURITY.md](../SECURITY.md).

`python -m edge start` loads repo-root `.env` (shell exports win) and passes the env to LiteLLM. Copy `.env.example` → `.env` and set `OPENAI_API_KEY`.

- LiteLLM (and Harbor’s generator) use env **`OPENAI_API_KEY`**. Do not put raw keys in YAML.
- `edge/litellm_gen.py` auto-sets `api_key: os.environ/OPENAI_API_KEY` on any `openai/*` model (and `XAI_API_KEY` for `xai/*`) unless you override via `extra_litellm_params`.
- `python -m edge start` **warns** (does not fail) if any `openai/*` model is configured and `OPENAI_API_KEY` is unset.

## Optional all-Ollama expensive

Comment out the OpenAI entry in `config/gateway.yaml` and use the commented `ollama-expensive` block instead (same `api_base`; no cloud key). Set `cost_estimates_usd.expensive_per_request` to `0.0` if you want.

## CLI: list / set model pools

`config/gateway.yaml` **`models.cheap` / `models.expensive`** remain the source of truth (no admin UI).

```bash
python -m edge models
# Point cheap at local (or remote) Ollama
python -m edge models set --pool cheap --model qwen3:8b --api-base http://127.0.0.1:11434
# Remote host example:
# python -m edge models set --pool cheap --model qwen3:8b --api-base http://<ollama-host>:11434

python -m edge models set --pool expensive --model gpt-4o-mini
python -m edge models set --pool expensive --disable   # optional
python -m edge models set --pool cheap --model qwen3:8b --api-base http://127.0.0.1:11434 --dry-run
```

After a successful write, restart:

```bash
python -m edge stop && python -m edge start
```

`models set` rewrites YAML with PyYAML (**inline comments are not preserved**). Prefer `config/gateway.yaml.example` as the annotated template.

`python -m edge doctor` soft-probes each configured Ollama `api_base` with `GET /api/tags`. If Ollama is down it **warns** only — doctor still succeeds when deps are OK.

## Smoke

`./scripts/smoke.sh` always requires cheap + stream (+ ledger). For the expensive-keyword prompt:

- **`OPENAI_API_KEY` unset** — soft-**SKIP** upstream completion success; still asserts ledger recorded expensive routing.
- **`OPENAI_API_KEY` set** — asserts successful expensive completion + ledger.

Mock mode (`GATEWAY_CONFIG=config/gateway.mock.yaml`) always asserts expensive success (no cloud).

## Quick path

Template cheap pool points at **local Ollama** (`qwen3:8b` @ `http://127.0.0.1:11434`). Pull once: `ollama pull qwen3:8b`. Use any reachable URL for remote Ollama.

```bash
source .venv/bin/activate          # Windows: .venv\Scripts\activate
curl -s http://127.0.0.1:11434/api/tags | head
# optional for OpenAI expensive — set in .env, never commit
python -m edge stop
python -m edge start
./scripts/smoke.sh
```

See also [../../RUN_OLLAMA.md](../../RUN_OLLAMA.md) and [streaming.md](streaming.md).

## Troubleshooting: OpenAI "Connection error"

If smoke/expensive fails with LiteLLM `OpenAIException - Connection error` while a direct
`httpx` call to `api.openai.com` works, check LiteLLM logs for
`SSLCertVerificationError` / `CERTIFICATE_VERIFY_FAILED`.

LiteLLM's proxy uses **aiohttp** for OpenAI. On some Python installs the default
CA store is empty, so TLS fails. Harbor's `python -m edge start` sets `SSL_CERT_FILE` and
`REQUESTS_CA_BUNDLE` to **certifi**'s bundle when unset. Restart after pulling that fix:

```bash
python -m edge stop
python -m edge start --detach
./scripts/smoke.sh
```

You can also export those yourself before start. Do not disable TLS verification.
