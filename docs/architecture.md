# Architecture (v0)

## Components

1. **Clients** — curl/SDK first; Codex (local OpenAI-compat); Claude Code (Anthropic Messages); Cursor (public HTTPS + LiteLLM `/cursor` or Azure-style attach).
2. **Edge (ours)** — process on `:4000` accepting the surfaces clients need; runs budget → router → why-log before upstream.
3. **Day budget** — spend-so-far vs daily USD cap; over budget → reject or force-cheap (config).
4. **Heuristic router** — classify request; pick from cheap/expensive pools in config; does not rebuild LiteLLM’s catalog.
5. **Why-log** — one line: classify context, model chosen, reason (heuristic / budget / override), then tokens/cost/status.
6. **LiteLLM** — wrapped proxy; providers + keys.
7. **Config** — YAML/env only in v0 (no DB).

## Diagram

See README mermaid. Same path for all clients once traffic hits the edge.
