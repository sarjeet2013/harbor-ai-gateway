# Architecture (v0)

## Components

1. **Clients** — curl/SDK first; Codex (`/v1/responses` or chat); Claude Code (Anthropic `/v1/messages`); Cursor (public HTTPS + LiteLLM `/cursor`).
2. **Edge (ours)** — FastAPI on `:4000`; budget → router → why-log before upstream. Managed by `python -m edge start`.
3. **Day budget** — spend-so-far vs daily USD cap; over budget → `force_cheap` or `reject` (config). Persisted to `data/budget.json`.
4. **Heuristic router** — classify request; pick from cheap/expensive pools in `config/gateway.yaml` (alias `name`).
5. **Why-log** — JSONL (`logs/why.jsonl`): model, reason, pool, tokens, cost, status.
6. **Upstream** — `upstream_mode: mock` (echo completions, no keys) or `litellm` (CLI-managed proxy on `:4001`).
7. **Config** — single canonical `config/gateway.yaml`; LiteLLM YAML is generated (`config/.generated_litellm.yaml`).

## Surfaces

| Path | Purpose |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/messages` | Anthropic Messages (Claude Code) |
| `POST /v1/responses` | OpenAI Responses (Codex `wire_api=responses`) |
| `/cursor/*` | Proxied to LiteLLM when not in mock mode |

## Diagram

See README mermaid. Same path for all clients once traffic hits the edge.
