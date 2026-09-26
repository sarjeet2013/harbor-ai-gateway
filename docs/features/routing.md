# Feature: Heuristic routing (phase-1 task-class)

**Status:** **Shipped (phase 1).** Harbor’s edge router is **heuristic only** (keywords + length). An LLM **judge** after the cache is still **planned** — not implemented here.

## Decision order

1. **Client-requested model alias** — if `body.model` matches a configured pool `name` (`ollama-cheap`, `ollama-coder`, `openai-expensive`, …), that alias **wins**. Ledger reason: `client:model`.
2. **Expensive pool** — user/assistant prompt text (system prompts skipped) matches `router.expensive_keywords` or exceeds `router.expensive_min_chars` → primary expensive alias. Reasons: `heuristic:expensive_keyword`, `heuristic:expensive_min_chars`.
3. **Cheap task-class** (phase 1) — within the cheap pool, config-driven keywords pick by **name**:
   - `coder_keywords` → `router.coder_model` (default `ollama-coder`) → `heuristic:coder_keyword`
   - `reason_keywords` → `router.reason_model` (default `ollama-reason`) → `heuristic:reason_keyword`
   - else → `router.default_cheap_model` (default `ollama-cheap`) → `heuristic:default_cheap`

Unknown client model ids (e.g. Claude Code’s stock model name) do **not** pin routing — steps 2–3 apply. Budget `force_cheap` still forces the cheap pool (task-class may apply).

Source: `edge/router.py`, keywords + alias targets in `config/gateway.yaml` → `router:`.

## Cheap pool aliases (default hybrid)

| Alias | LiteLLM model | Role |
| --- | --- | --- |
| `ollama-cheap` | `ollama_chat/qwen3:8b` + Mini `api_base` | **Default chat** (keep for existing Claude Code / Cursor configs). `extra_litellm_params.think: false`. |
| `ollama-chat` | same 8b | Optional duplicate chat name |
| `ollama-coder` | `ollama_chat/qwen2.5-coder:14b` | Code / implement / fix / tests |
| `ollama-reason` | `ollama_chat/deepseek-r1:14b` | Step-by-step / math / prove (alt: `qwen3:14b` in YAML comments) |

All enabled cheap aliases are emitted into generated LiteLLM config (`edge/litellm_gen.py`).

## VRAM note

Mini typically should **not** keep two 14B models resident. Ollama will swap; expect a cold load when switching `ollama-coder` ↔ `ollama-reason`. Prefer one “hot” 14B for your session when possible.

## Claude Code / Cursor

- **Default path:** leave the client model as a non-Harbor id (or omit a Harbor pin). Harbor lands on **`ollama-cheap`** unless task-class or expensive heuristics fire.
- **Auto coder:** prompt text hits `coder_keywords` → routed to **`ollama-coder`** (ledger `heuristic:coder_keyword`).
- **Force coder:** set the request model / `ANTHROPIC_MODEL` to **`ollama-coder`** (wins as `client:model`).
- **Pin 8b chat:** set model to **`ollama-cheap`** (or `ollama-chat`) — disables task-class upgrades for that request.

See also [hybrid.md](hybrid.md) and [architecture.md](../architecture.md).
