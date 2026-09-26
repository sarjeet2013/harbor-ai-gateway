# Security

## Never commit secrets

- Copy `.env.example` → `.env` and put provider keys there (`OPENAI_API_KEY`, optional `XAI_API_KEY`).
- `.env`, `data/*`, `logs/*`, `*.pid`, and `config/gateway.yaml` (local override) are gitignored.
- Do **not** put raw provider keys in YAML, client snippets, commits, or issues.

## Local bearer ≠ provider key

Clients may send a local placeholder such as `sk-local-gateway` (`Authorization` / `x-api-key`).
Harbor does **not** forward that header to LiteLLM/providers. Cloud auth uses only process env
(e.g. `OPENAI_API_KEY` loaded on `python -m edge start`).

## If a key may have leaked

1. Rotate the key at the provider (OpenAI / xAI / etc.).
2. Remove it from any local files that were shared.
3. Confirm it is not in the current tree: `git grep -E 'sk-proj-|sk-[a-zA-Z0-9]{20,}'` (allow only `sk-local-gateway` / test placeholders).
4. Git history is not rewritten by default — assume old commits may still contain sensitive strings and rotate anyway if unsure.

## Network exposure

- Default listen is `0.0.0.0:4000` (LAN-reachable). Bind to `127.0.0.1:4000` in `config/gateway.yaml` if you only want loopback.
- Cursor attach via a public HTTPS tunnel exposes your edge to the internet — treat the tunnel URL like a credential and stop the tunnel when unused.

## Admin UI

- The mini Admin UI is served from the edge at `/admin` (same listen address as the gateway).
- **Mutating** admin API routes (`POST /admin/api/settings`) refuse non-loopback clients (`127.0.0.1` / `::1`) by default. There is no separate public admin port.
- Frontier keys entered in the UI are written to repo `.env` only — never to `gateway.yaml`. The API returns set/unset booleans, never key values.
- Prefer opening `http://127.0.0.1:<port>/admin` on the host running Harbor. Details: [features/admin-ui.md](features/admin-ui.md).
