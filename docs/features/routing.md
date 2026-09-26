# Feature: Intelligent (LLM judge) routing

**Status:** **Shipped as live default.** Harbor’s edge router uses a local Ollama **judge** (`qwen3:8b`) as the primary strategy (`router.strategy: judge_then_heuristic`). Keyword / length **heuristic** remains the failure / timeout / low-confidence fallback — not removed.

## Decision order

1. **Client-requested Harbor alias** — if `body.model` matches a configured pool `name` (`ollama-cheap`, `ollama-coder`, `openai-expensive`, …), that alias **wins**. No judge call. Ledger reason: `client:model`.
2. **Judge** (when `strategy: judge_then_heuristic` and `router.judge.enabled`) — classifies **last-user text** plus light signals (`chars≈N`, `tools=yes|no`). Closed labels only:
   - `chat` → `router.default_cheap_model` (default `ollama-cheap`) → `judge:chat`
   - `coder` → `router.coder_model` (default `ollama-coder`) → `judge:coder`
   - `reason` → `router.reason_model` (default `ollama-reason`) → `judge:reason`
   - `expensive` → primary expensive alias → `judge:expensive`
3. **Fallback** — judge timeout, HTTP/parse failure, confidence &lt; `min_confidence`, or disabled judge → existing expensive + task-class keyword rails. Reason: `judge_fallback:heuristic:…`.
4. **Budget `force_cheap`** — still forces the cheap pool. Within cheap, judge may pick chat/coder/reason; an `expensive` label is remapped via cheap heuristic (not cloud).

Unknown client model ids (e.g. Claude Code’s stock model name) do **not** pin routing — steps 2–3 apply.

Source: `edge/judge.py`, `edge/router.py`; config in `config/gateway.yaml` → `router:`.

## Judge config (template)

```yaml
router:
  strategy: judge_then_heuristic
  judge:
    enabled: true
    litellm_model: ollama_chat/qwen3:8b   # keep judge on a small/hot model
    api_base: http://127.0.0.1:11434
    timeout_ms: 1500
    min_confidence: 0.55
    on_failure: heuristic
```

`strategy: heuristic_cost_first` remains supported (mock / tests). Template ships `judge_then_heuristic`.

## Judge vs cache

Order stays **budget → router → cache**. The judge runs in the router **before** cache so the chosen model identity stays consistent with the cache key. Prefer a fast judge. Cache hits still skip upstream, but route was already chosen.

Ledger optional extras when the judge ran: `judge_ms`, `judge_label`, `judge_confidence`.

## Heuristic rails (fallback)

| Match | Pool / alias | Reason |
| --- | --- | --- |
| `expensive_keywords` / `expensive_min_chars` | expensive | `heuristic:expensive_*` |
| `coder_keywords` | `coder_model` | `heuristic:coder_keyword` |
| `reason_keywords` | `reason_model` | `heuristic:reason_keyword` |
| else | `default_cheap_model` | `heuristic:default_cheap` |

## Cheap pool aliases (default hybrid)

| Alias | LiteLLM model | Role |
| --- | --- | --- |
| `ollama-cheap` | `ollama_chat/qwen3:8b` + `api_base` | Default chat / judge `chat` |
| `ollama-chat` | same 8b | Optional duplicate chat name |
| `ollama-coder` | `ollama_chat/qwen2.5-coder:14b` | Judge `coder` |
| `ollama-reason` | `ollama_chat/deepseek-r1:14b` | Judge `reason` |

## VRAM / latency notes

- Judge should stay on a **small/hot** model (`qwen3:8b`), not a large classifier.
- Avoid keeping two 14Bs resident on one GPU host; Ollama will swap coder ↔ reason.
- If judge TTFB exceeds `timeout_ms`, expect `judge_fallback:heuristic:…` in the ledger (still correct routing via keywords). Raise `timeout_ms` only if you accept added edge latency.

## Rollback

- Sustained judge timeouts dominating ledger, and heuristic quality acceptable → set `strategy: heuristic_cost_first` or `judge.enabled: false`, restart.
- Wrong-pool spikes → tighten labels via prompt/examples or temporarily raise `min_confidence`.

## Claude Code / Cursor

- **Default path:** leave the client model as a non-Harbor id. Harbor runs the judge (or heuristic fallback).
- **Force coder / chat / expensive:** set the request model / `ANTHROPIC_MODEL` to the Harbor alias (`client:model`).

See also [hybrid.md](hybrid.md) and [architecture.md](../architecture.md).
