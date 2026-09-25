# Response cache

**Status:** **Exact cache shipping** (`mode: exact`). Semantic cache and intelligent (judge) routing are **planned / not yet shipped**.

Harbor’s differentiation vs smart-routing-only gateways: prove value with **tokens saved** via response caching at the edge.

## Goals

- Reduce token spend by serving identical (and later: similar) requests from a local cache.
- Prove gateway value: every hit records `tokens_saved*` in the why-log.
- Stay personal/MVP: local disk only — **no Redis required**.

## Request path

```
budget → router → cache → why-log → upstream (LiteLLM / mock)
```

On a **hit**, the edge returns the cached JSON body and **skips upstream**. Day budget is **not** charged on pure hits (`cost_usd: 0`).

## Phases

### v1 Exact cache (shipped)

Hash of a **normalized** request:

- Routed `model` + `pool` (after the heuristic router)
- `surface` (chat / messages / responses — response shapes differ)
- `messages` and/or `input` (+ `system` when present)
- Sampling/tool fields that change the completion (`temperature`, `max_tokens`, `tools`, …)

Module: `edge/cache.py`. Integrated in `edge/main.py` **after** the router picks a model, **before** upstream.

### v2 Semantic cache (planned)

Embeddings + cosine similarity threshold. Config stubs:

```yaml
# planned — not used yet
# embedding_model: ...
# similarity_threshold: 0.92
```

Do not enable semantic expectations until this lands.

## Out of scope for this doc / next

- **Intelligent (judge) routing** — planned **after** cache. Not built yet; Omnigent-style smart routing is a follow-on, not a substitute for tokens-saved cache.
- Streaming responses — **not cached** until streaming support exists (`stream: true` bypasses cache).
- Multi-node / Redis shared cache.

## Storage

Local disk under `data/cache/` (configurable via `cache.path`):

- One JSON file per entry (`{sha256}.json`)
- `index.json` for TTL + max_entries (FIFO eviction)

Gitignored via `data/*`.

## Why-log / metrics (required)

Every request (including misses and budget rejects) includes:

| Field | Meaning |
| --- | --- |
| `cache_hit` | `true` / `false` |
| `cache_kind` | `exact` \| `semantic` \| `none` |
| `tokens_saved` | Estimated tokens not sent upstream (prompt + completion from cached entry; prompt-only if that is all we know) |
| `tokens_saved_prompt` | Prompt/input tokens from cached usage |
| `tokens_saved_completion` | Completion/output tokens from cached usage |

On miss: `cache_hit: false`, `cache_kind: "none"`, all `tokens_saved*` = `0`.

**Future value-add:** aggregate dashboard / richer CLI UX around `python -m edge cache-stats` (hit rate + total tokens saved from why-log). The basic CLI command ships with exact cache.

Example hit line (fields abbreviated):

```json
{"surface":"openai.chat.completions","model":"mock-cheap","pool":"cheap","status":"ok","cache_hit":true,"cache_kind":"exact","tokens_saved":42,"tokens_saved_prompt":30,"tokens_saved_completion":12,"cost_usd":0.0}
```

## Invalidation / bypass

| Mechanism | Behavior |
| --- | --- |
| `ttl_seconds` | Entries older than TTL are dropped |
| `max_entries` | FIFO eviction when full |
| Streaming | `stream: true` → skip cache (do not store) |
| Bypass header | `X-Cache-Bypass: 1` (or `true`) |
| Cache-Control | `Cache-Control: no-cache` / `no-store` |
| Body | `cache_control` string containing `no-cache`, or `cache_control: false` |

Optional model/pool keying is inherent: the hash includes routed `model` and `pool`.

## Config (`config/gateway.yaml`)

```yaml
cache:
  enabled: true
  mode: exact   # exact | semantic (semantic planned)
  ttl_seconds: 86400
  max_entries: 1000
  path: data/cache
  # semantic later: embedding_model, similarity_threshold
```

Default for local Mac testing: **`enabled: true`**, TTL 24h.

## Implementation sketch

1. `edge/cache.py` — `normalize_for_hash`, `get` / `set`, disk persistence, TTL, max_entries.
2. `edge/main.py` `_handle` — after `pick_model`, before mock/LiteLLM:
   - On hit → return cached body; why-log with savings; **no** `budget.record`.
   - On miss → upstream; on success store entry; why-log miss fields.
3. `python -m edge cache-stats` — hit rate + `tokens_saved` totals from why-log + disk entry count.

## Testing notes

- Unit: hash stability for equivalent normalized payloads; hit path returns same body and `tokens_saved > 0`.
- Integration (mock): two identical `POST /v1/chat/completions` → second why-log line has `cache_hit: true`.
- Bypass: `X-Cache-Bypass: 1` forces miss/store skip as configured.
- Streaming: `"stream": true` never hits/stores.

```bash
# Mac / local
GATEWAY_CONFIG=config/gateway.mock.yaml python -m edge start -d
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mock-cheap","messages":[{"role":"user","content":"cache me"}]}'
# repeat same curl → cache hit
python -m edge cache-stats
tail -1 logs/why.jsonl
python -m edge stop
```
