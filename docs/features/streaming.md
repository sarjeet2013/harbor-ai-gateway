# Feature: Streaming

**Status:** **Not shipped.** Edge buffers full JSON today (`edge/proxy.py` → `httpx` non-stream). LiteLLM / Ollama already support SSE; the gap is Harbor’s edge.

`stream: true` is accepted on the wire and **bypasses the response cache** (no hit/store) — see [cache.md](cache.md). Non-stream JSON remains the only path that records usage into budget + ledger from a parsed body.

## What clients need

OpenAI-style SSE on `POST /v1/chat/completions` with `"stream": true` (Cursor / SDKs / many tools). Anthropic Messages and OpenAI Responses streaming are follow-ons (harder because Harbor translates those surfaces ↔ chat).

## What is needed (implementation checklist)

### v1 — chat passthrough (do this first)

1. **Stream proxy** — add `LiteLLMProxy.forward_stream` (or equivalent) using `httpx` `stream=True`; return FastAPI `StreamingResponse` with `media_type=text/event-stream`; pass through upstream SSE bytes.
2. **Branch in `_handle`** — when `body.stream is True` and surface is chat, use the stream path instead of buffering JSON. Keep today’s JSON path for `stream: false` / omitted.
3. **Mock SSE** — `upstream_mode: mock` should emit a small fake chat-completion SSE so smoke works without LiteLLM.
4. **Ledger + budget on complete** — after the upstream stream finishes (tee chunks or read the final usage chunk), write **one** ledger line and call `budget.record` once. Do not charge per chunk. On mid-stream failure, ledger `status` should reflect the error.
5. **Cache** — leave as today: `stream: true` → `should_bypass_cache` (no get/set). Optional later: buffer-and-store full completion while still streaming to the client (out of scope for v1).
6. **Smoke** — curl with `"stream": true`, assert `data:` lines + a ledger row with `status: ok`.

**Not required for v1:** new dependencies, LiteLLM config changes, semantic cache, judge routing.

### Later — other surfaces

7. **`/v1/messages` streaming** — translate Anthropic stream ↔ chat SSE (or stream chat upstream and re-encode Anthropic events).
8. **`/v1/responses` streaming** — same idea for the Responses wire format.
9. **`/cursor/*`** — only if Cursor attach still needs edge-level SSE beyond LiteLLM’s own proxy.

## Ship order

1. Chat stream passthrough + mock SSE  
2. Ledger / budget after stream completes  
3. Smoke  
4. Messages / Responses SSE only when a real client is blocked  

## Out of scope for this note

Implementing the code — this doc is the standing plan until streaming ships. When it ships, update **Status** here and the Planned lists in [architecture.md](../architecture.md) and the README.
