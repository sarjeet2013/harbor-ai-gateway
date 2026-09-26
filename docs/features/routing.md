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

## Judge config (live)

```yaml
router:
  strategy: judge_then_heuristic
  judge:
    enabled: true
    litellm_model: ollama_chat/qwen3:8b   # never a 14B for classification
    api_base: http://192.168.4.31:11434
    timeout_ms: 400
    min_confidence: 0.55
    on_failure: heuristic
```

`strategy: heuristic_cost_first` remains supported (mock / tests). Live `gateway.yaml` ships `judge_then_heuristic`.

## Judge vs cache

Order stays **budget → router → cache**. The judge runs in the router **before** cache (same as the old heuristic) so the chosen model identity stays consistent with the cache key. Prefer a fast judge (`timeout_ms` &lt; 500). Cache hits still skip upstream, but route was already chosen.

Ledger optional extras when the judge ran: `judge_ms`, `judge_label`, `judge_confidence`.

## Heuristic rails (fallback)

Same keyword lists as phase-1:

| Match | Pool / alias | Reason |
| --- | --- | --- |
| `expensive_keywords` / `expensive_min_chars` | expensive | `heuristic:expensive_*` |
| `coder_keywords` | `coder_model` | `heuristic:coder_keyword` |
| `reason_keywords` | `reason_model` | `heuristic:reason_keyword` |
| else | `default_cheap_model` | `heuristic:default_cheap` |

## Cheap pool aliases (default hybrid)

| Alias | LiteLLM model | Role |
| --- | --- | --- |
| `ollama-cheap` | `ollama_chat/qwen3:8b` + Mini `api_base` | Default chat / judge `chat` |
| `ollama-chat` | same 8b | Optional duplicate chat name |
| `ollama-coder` | `ollama_chat/qwen2.5-coder:14b` | Judge `coder` |
| `ollama-reason` | `ollama_chat/deepseek-r1:14b` | Judge `reason` |

## VRAM / latency notes

- Judge must stay on the **hot 8b** (`qwen3:8b`), never a 14B classifier.
- Mini typically should **not** keep two 14Bs resident; Ollama swaps coder ↔ reason.
- If Mini judge TTFB exceeds `timeout_ms`, expect `judge_fallback:heuristic:…` in the ledger (still correct routing via keywords). Raise `timeout_ms` only if you accept added edge latency.

## Kill / rollback criteria (brief)

- Sustained judge timeouts dominating ledger, *and* heuristic quality acceptable → set `strategy: heuristic_cost_first` or `judge.enabled: false`, restart.
- Wrong-pool spikes after ship → tighten labels via prompt/examples or temporarily raise `min_confidence`.

## Claude Code / Cursor

- **Default path:** leave the client model as a non-Harbor id. Harbor runs the judge (or heuristic fallback).
- **Force coder / chat / expensive:** set the request model / `ANTHROPIC_MODEL` to the Harbor alias (`client:model`).

See also [hybrid.md](hybrid.md) and [architecture.md](../architecture.md).
