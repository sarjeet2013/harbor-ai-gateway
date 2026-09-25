# Response cache

**Status:** **Exact + semantic cache shipping**. Exact is the default in live `gateway.yaml`; set `mode: semantic` after pulling an embedding model on Ollama (e.g. Mini). Intelligent (judge) routing remains **planned**.

Harbor’s differentiation vs smart-routing-only gateways: prove value with **tokens saved** via response caching at the edge.

## Goals

- Reduce token spend by serving identical **and similar** requests from a local cache.
- Prove gateway value: every hit records `tokens_saved*` in the ledger.
- Stay personal/MVP: local disk only — **no Redis required**.

## Request path

```
budget → router → cache → ledger → upstream (LiteLLM / mock)
```

On a **hit**, the edge returns the cached JSON body and **skips upstream**. Day budget is **not** charged on pure hits (`cost_usd: 0`).

## Modes

### Exact (`mode: exact`) — always available

Hash of a **normalized** request:

- Routed `model` + `pool` (after the heuristic router)
- `surface` (chat / messages / responses — response shapes differ)
- `messages` and/or `input` (+ `system` when present)
- Sampling/tool fields that change the completion (`temperature`, `max_tokens`, `tools`, …)

No embedding calls. Module: `edge/cache.py`. Integrated in `edge/main.py` **after** the router picks a model, **before** upstream.

### Semantic (`mode: semantic`) — shipped

**Exact-first**, then embedding similarity:

1. Same exact-hash path as today → hit with `cache_kind: exact`.
2. On exact miss: embed **cacheable text** (normalized user/system/messages/input content — not full tool JSON). Compare cosine similarity to stored embeddings for entries with the **same** `model`, `pool`, and `surface`. Candidates whose sampling keys (`temperature`, `max_tokens`, `tools`, …) differ are skipped.
3. If best similarity ≥ `similarity_threshold` (default **0.92**) → hit with `cache_kind: semantic`; return cached body; ledger `tokens_saved*` as today; skip upstream; no budget charge.
4. On miss: upstream as today; on success store the exact entry **plus** embedding vector (when embed succeeds).
5. If the embedding provider fails (Ollama down, model missing): **soft-fail** to miss (still store exact without embedding on set; warn once). Never 500 the request because cache embed failed.

**Embedding backend (MVP):** Ollama HTTP via `httpx` — `POST {embedding_api_base}/api/embeddings` with `{"model", "prompt"}` (falls back to `/api/embed` if 404). Default `embedding_api_base` is the cheap pool’s `api_base` (Mini), else `http://127.0.0.1:11434`.

## Out of scope / next

- **Intelligent (judge) routing** — planned **after** cache. Not built yet; Omnigent-style smart routing is a follow-on, not a substitute for tokens-saved cache.
- Streaming responses — **not cached** (`stream: true` bypasses cache). Chat SSE passthrough is shipping — see [streaming.md](streaming.md).
- Multi-node / Redis shared cache.

## Storage

Local disk under `data/cache/` (configurable via `cache.path`):

- One JSON file per entry (`{sha256}.json`) — may include `embedding`, `embed_model`, `text_fingerprint`
- `index.json` for TTL + max_entries (FIFO eviction) + lightweight semantic meta (`has_embedding`, `sampling`, …)
- MVP may scan ≤ `max_entries` (1000) for semantic search

Gitignored via `data/*`.

## Ledger / metrics (required)

Every request (including misses and budget rejects) includes:

| Field | Meaning |
| --- | --- |
| `cache_hit` | `true` / `false` |
| `cache_kind` | `exact` \| `semantic` \| `none` |
| `tokens_saved` | Estimated tokens not sent upstream (prompt + completion from cached entry) |
| `tokens_saved_prompt` | Prompt/input tokens from cached usage |
| `tokens_saved_completion` | Completion/output tokens from cached usage |

On miss: `cache_hit: false`, `cache_kind: "none"`, all `tokens_saved*` = `0`.

**Future value-add:** aggregate dashboard / richer CLI UX around `python -m edge cache-stats` (hit rate + total tokens saved from ledger). The basic CLI command ships with exact + semantic.

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

Optional model/pool keying is inherent: the hash (and semantic filter) includes routed `model` and `pool`.

## Config (`config/gateway.yaml`)

```yaml
cache:
  enabled: true
  mode: exact   # or semantic
  ttl_seconds: 86400
  max_entries: 1000
  path: data/cache
  embedding_model: nomic-embed-text
  # embedding_api_base: http://192.168.4.31:11434  # default: cheap pool api_base
  similarity_threshold: 0.92
```

Live default stays **`mode: exact`** until the embed model is verified on Mini. `config/gateway.yaml.example` documents **`mode: semantic`**.

### Enable semantic (live)

```bash
# On Mini (or via Air → Mini):
curl http://192.168.4.31:11434/api/pull -d '{"name":"nomic-embed-text"}'
# Then in gateway.yaml: mode: semantic  (embedding_model already set)
python -m edge stop && python -m edge start -d
```

## Implementation

1. `edge/cache.py` — `normalize_for_hash`, `cacheable_text`, `cosine_similarity`, Ollama embed, `get` / `set`, disk persistence, TTL, max_entries.
2. `edge/main.py` `_handle` — after `pick_model`, before mock/LiteLLM:
   - On hit → return cached body; ledger with savings; **no** `budget.record`.
   - On miss → upstream; on success store entry (+ embedding in semantic mode); ledger miss fields.
3. `python -m edge cache-stats` — hit rate + `tokens_saved` totals from ledger + disk entry count.

## Testing notes

- Unit: hash stability; cosine; semantic hit when vectors similar; exact preferred; threshold miss; embed failure → miss; `mode: exact` never calls embed (mock).
- Integration (mock): two identical `POST /v1/chat/completions` → second ledger line has `cache_hit: true`, `cache_kind: exact`.
- Bypass: `X-Cache-Bypass: 1` forces miss.
- Streaming: `"stream": true` never hits/stores.

```bash
# Mac / local
GATEWAY_CONFIG=config/gateway.mock.yaml python -m edge start -d
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mock-cheap","messages":[{"role":"user","content":"cache me"}]}'
# repeat same curl → cache hit
python -m edge cache-stats
tail -1 logs/ledger.jsonl
python -m edge stop
```
