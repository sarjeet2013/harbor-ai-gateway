# Feature: Streaming

**Status:** **Shipped (v1 chat).** `POST /v1/chat/completions` with `"stream": true` returns OpenAI-style SSE (`text/event-stream`) via passthrough from LiteLLM/Ollama, or mock SSE when `upstream_mode: mock`. Anthropic Messages and OpenAI Responses streaming are **not** shipped — those surfaces return **HTTP 501** with a clear error if `stream: true` (prefer that over silently buffering).

`stream: true` **bypasses the response cache** (no hit/store) — see [cache.md](cache.md). After a chat stream completes successfully, the edge writes **one** ledger line and calls `budget.record` once (usage parsed from the final SSE chunk when present; otherwise tokens empty / cost from estimates). Mid-stream failure → ledger `status` reflects the error.

## What clients need

OpenAI-style SSE on `POST /v1/chat/completions` with `"stream": true` (Cursor / SDKs / many tools). Anthropic Messages and OpenAI Responses streaming are follow-ons (harder because Harbor translates those surfaces ↔ chat).

## What is needed (implementation checklist)

### v1 — chat passthrough (shipped)

1. **Stream proxy** — `LiteLLMProxy.forward_stream` uses `httpx` `stream=True`; edge returns FastAPI `StreamingResponse` with `media_type=text/event-stream`; passes through upstream SSE bytes.
2. **Branch in `_handle`** — when `body.stream is True` and surface is chat (`openai.chat.completions`), use the stream path instead of buffering JSON. Keep today’s JSON path for `stream: false` / omitted.
3. **Mock SSE** — `upstream_mode: mock` emits OpenAI-compatible chat-completion SSE ending with `data: [DONE]`.
4. **Ledger + budget on complete** — after the upstream stream finishes, write **one** ledger line and call `budget.record` once. Do not charge per chunk. On mid-stream failure, ledger `status` should reflect the error.
5. **Cache** — leave as today: `stream: true` → `should_bypass_cache` (no get/set). Optional later: buffer-and-store full completion while still streaming to the client (out of scope for v1).
6. **Smoke** — curl with `"stream": true`, assert `data:` lines (+ `[DONE]` when mock).

**Not required for v1:** new dependencies, LiteLLM config changes, semantic cache, judge routing.

### Later — other surfaces

7. **`/v1/messages` streaming** — translate Anthropic stream ↔ chat SSE (or stream chat upstream and re-encode Anthropic events). Today: **501** if `stream: true`.
8. **`/v1/responses` streaming** — same idea for the Responses wire format. Today: **501** if `stream: true`.
9. **`/cursor/*`** — only if Cursor attach still needs edge-level SSE beyond LiteLLM’s own proxy.

## Ship order

1. Chat stream passthrough + mock SSE — **done**
2. Ledger / budget after stream completes — **done**
3. Smoke — **done**
4. Messages / Responses SSE only when a real client is blocked

## Out of scope for this note

Semantic cache. Judge routing. (Hybrid Ollama cheap + OpenAI expensive is shipped — see [hybrid.md](hybrid.md).)

## Troubleshooting (Ollama stream)

If `./scripts/smoke.sh` fails the stream section with LiteLLM errors mentioning `async_generator` or `async_get_ollama_response_stream`, upgrade LiteLLM in the venv:

```bash
pip install -U -r requirements-litellm.txt
# or: pip install -U 'litellm[proxy]>=1.72' async_generator
python -m edge stop && python -m edge start
```

Harbor defaults use the `ollama_chat/...` LiteLLM prefix (chat + SSE), not the legacy `ollama/...` completion prefix.
