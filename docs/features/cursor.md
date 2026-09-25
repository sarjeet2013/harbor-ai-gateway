# Feature: Cursor attach

**Status:** **Shipped (local + tunnel).** Cursor’s Override OpenAI Base URL traffic usually comes from **Cursor’s cloud**, so `http://127.0.0.1:4000` is unreachable without a **public HTTPS tunnel** to Harbor’s edge.

Harbor serves Cursor on `/cursor/*` through the same `_handle` path as `/v1/chat/completions` (budget, heuristic router, exact cache, ledger). It also lists pool aliases on `GET /v1/models` and `GET /cursor/models`.

## Cursor settings

1. Start Harbor: `python -m edge start` (edge `:4000`, LiteLLM `:4001`).
2. Expose HTTPS to the edge, e.g. Cloudflare quick tunnel:

```bash
~/bin/cloudflared tunnel --url http://127.0.0.1:4000 --protocol http2 --no-autoupdate
```

Copy the printed `https://….trycloudflare.com` host (it changes every restart).

3. **Cursor → Settings → Models → OpenAI**
   - **Override OpenAI Base URL:** `https://<tunnel-host>/cursor`
   - **OpenAI API Key:** any non-empty string (e.g. `sk-local-gateway`). Harbor does not enforce a gateway key yet; do **not** put your raw OpenAI key in Cursor if you can avoid it — keep `OPENAI_API_KEY` in Harbor’s `.env` for the expensive pool.
   - **Custom models:** `ollama-cheap`, `openai-expensive` (names from `config/gateway.yaml`).

4. Select `ollama-cheap` (or `openai-expensive` when `OPENAI_API_KEY` is set) and chat.

See also `config/clients/cursor.env.example`.

## What Harbor implements

| Path | Behavior |
|------|----------|
| `POST /cursor/chat/completions` | Same as `POST /v1/chat/completions` via `_handle` |
| `GET /cursor/models` | Harbor pool aliases (`ollama-cheap`, `openai-expensive`, …) |
| `GET /v1/models` | Same model list |
| Other `/cursor/*` | Fallback proxy to LiteLLM `/v1/*` (LiteLLM 1.79 has no `/cursor` route) |

Streaming: send `"stream": true` on chat; Harbor passthrough SSE is shipped — see [streaming.md](streaming.md).

## Ops notes

- Prefer `--protocol http2` on macOS if QUIC quick tunnels return Cloudflare **530**.
- Keep Harbor and `cloudflared` running while Cursor is attached.
- Restarting the quick tunnel → new host → update Override Base URL.
- For a stable hostname, use a named Cloudflare tunnel (account required) instead of trycloudflare.

## Verify

```bash
TUNNEL=https://YOUR_TUNNEL_HOST
curl -sS "$TUNNEL/v1/models"
curl -sS "$TUNNEL/cursor/chat/completions" \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer sk-local-gateway' \
  -d '{"model":"ollama-cheap","messages":[{"role":"user","content":"hi"}],"max_tokens":16}'
```

Expect HTTP 200 and a completion body.

## Out of scope for this note

Named tunnels / auth gateway keys. Messages/Responses SSE. Semantic cache.
