# Feature: Streaming

**Status:** **Shipped (v1 chat + Anthropic messages + OpenAI Responses).** `POST /v1/chat/completions` with `"stream": true` returns OpenAI-style SSE; `POST /v1/messages` with `"stream": true` returns Anthropic Messages SSE (via chat upstream + remap); `POST /v1/responses` with `"stream": true` returns OpenAI Responses SSE (via chat upstream + remap) (`text/event-stream`) via passthrough/remap from LiteLLM/Ollama, or mock SSE when `upstream_mode: mock`.

`stream: true` uses the **same response cache** as non-stream (exact + semantic) — see [cache.md](cache.md). On a **hit**, the edge synthesizes OpenAI chat, Anthropic, or Responses SSE from the cached complete JSON body (no upstream). On a **miss**, upstream SSE is passed through (chat) or remapped (messages / responses) to the client while the edge accumulates bytes; a successful complete stream is stored for later hits. After a stream completes successfully, the edge writes **one** ledger line and calls `budget.record` once on misses (usage parsed from the final SSE chunk / `response.completed` when present; otherwise tokens empty / cost from estimates). Pure cache hits do not charge budget. Mid-stream failure → ledger `status` reflects the error and **nothing is stored**.

## What clients need

OpenAI-style SSE on `POST /v1/chat/completions`, Anthropic Messages SSE on `POST /v1/messages` (Claude Code), and OpenAI Responses SSE on `POST /v1/responses` (Codex).

## What is needed (implementation checklist)

### v1 — chat passthrough (shipped)

1. **Stream proxy** — `LiteLLMProxy.forward_stream` uses `httpx` `stream=True`; edge returns FastAPI `StreamingResponse` with `media_type=text/event-stream`; passes through upstream SSE bytes.
2. **Branch in `_handle`** — when `body.stream is True` and surface is chat (`openai.chat.completions`), use the stream path instead of buffering JSON. Keep today’s JSON path for `stream: false` / omitted.
3. **Mock SSE** — `upstream_mode: mock` emits OpenAI-compatible chat-completion SSE ending with `data: [DONE]`.
4. **Ledger + budget on complete** — after the upstream stream finishes, write **one** ledger line and call `budget.record` once. Do not charge per chunk. On mid-stream failure, ledger `status` should reflect the error.
5. **Cache** — **shipped:** `stream: true` lookups/stores share the non-stream key (`stream` omitted from hash). Hit → synthesize SSE; miss → tee-store on complete. Explicit bypass headers still skip cache.
6. **Smoke** — curl with `"stream": true`, assert `data:` lines (+ `[DONE]` when mock); second identical stream should ledger `cache_hit: true`.

**Not required for v1:** new dependencies, LiteLLM config changes, judge routing.

### Later — other surfaces

7. **`/v1/messages` streaming** — **done** (chat upstream + Anthropic SSE remap; collect-then-remap v1).
8. **`/v1/responses` streaming** — **done** (chat upstream + Responses SSE remap; collect-then-remap v1).
9. **`/cursor/*`** — only if Cursor attach still needs edge-level SSE beyond LiteLLM’s own proxy.

## Ship order

1. Chat stream passthrough + mock SSE — **done**
2. Ledger / budget after stream completes — **done**
3. Smoke — **done**
4. Messages / Responses SSE — **done** (collect-then-remap)

## Limits

- **Anthropic messages miss path** still **collect-then-remap** OpenAI chat SSE before emitting Anthropic events (`openai_chat_sse_to_anthropic_sse`). Cache **store** and **hit replay** work; miss TTFB for `/v1/messages` remains buffered until the upstream chat stream finishes. Token-at-a-time Anthropic remap is a follow-on.
- **OpenAI Responses miss path** likewise **collect-then-remap** (`openai_chat_sse_to_responses_sse`). Emits `response.created` → `response.output_text.delta`* → `response.completed` (plus item/part lifecycle events). Cache store/hit replay work; miss TTFB for `/v1/responses` is buffered until the upstream chat stream finishes. Token-at-a-time Responses remap is a follow-on.

## Out of scope for this note

Judge routing. (Hybrid Ollama cheap + OpenAI expensive is shipped — see [hybrid.md](hybrid.md). Semantic response cache is shipped — see [cache.md](cache.md).)

## Troubleshooting (Ollama stream)

If `./scripts/smoke.sh` fails the stream section with LiteLLM errors mentioning `async_generator` or `async_get_ollama_response_stream`, upgrade LiteLLM in the venv:

```bash
pip install -U -r requirements-litellm.txt
# or: pip install -U 'litellm[proxy]>=1.72' async_generator
python -m edge stop && python -m edge start
```

Harbor defaults use the `ollama_chat/...` LiteLLM prefix (chat + SSE), not the legacy `ollama/...` completion prefix.
