# Feature: Mini Admin UI

**Status:** **Shipped** (v1 mini). Docker packaging is Phase 1 shipped — see [packaging.md](packaging.md).

When the edge is running, open a browser to configure models, caching, routing, frontier API keys, and Ollama `api_base` (any reachable host) without hand-editing YAML for common changes.

## Open the UI

```text
http://127.0.0.1:4000/admin
```

(Use your configured edge port if not `4000`.)

Also:

```bash
python -m edge start --open-browser
# or in gateway.yaml:
# admin:
#   open_browser: true
```

`start` always prints the admin URL. `--open-browser` / `admin.open_browser` uses Python’s `webbrowser` module (works on common desktop OS installs).

## What you can configure

| Section | Writes to | Fields |
| --- | --- | --- |
| **Status** (read-only) | — | `upstream_mode`, budget snapshot, cache mode/entries, router strategy, model aliases + `api_base`, whether frontier keys are set |
| **Cheap / expensive models** | `config/gateway.yaml` | `name`, `litellm_model`, `api_base`, `enabled`, optional `think: false` via checkbox |
| **Cache** | YAML | `enabled`, `mode`, `similarity_threshold`, `embedding_model`, `embedding_api_base`, `semantic_scope` (preserves `ttl_seconds` / `max_entries` / `path`) |
| **Router** | YAML | `strategy`; judge `enabled` / `litellm_model` / `api_base` / `timeout_ms` / `min_confidence` (preserves keyword lists) |
| **Frontier keys** | repo **`.env` only** | `OPENAI_API_KEY`, optional `XAI_API_KEY` — create/update lines; UI shows **set/unset** only (never the value) |

YAML remains the source of truth. Secrets **never** go into YAML or git.

## API

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/admin` · `/admin/` | HTML shell |
| `GET` | `/admin/static/*` | `index.html` assets (`app.js`, `style.css`) |
| `GET` | `/admin/api/settings` | JSON settings + status (no secret values) |
| `POST` | `/admin/api/settings` | Apply — **localhost-only** |

## Security (localhost guard)

**Mutating** admin routes (`POST /admin/api/settings`) allow Apply when **either**:

1. The TCP peer host is loopback (`127.0.0.1`, `::1`, and IPv4-mapped forms), **or**
2. The HTTP `Host` header is loopback (`127.0.0.1`, `localhost`, with or without a port) — including Docker Desktop / Compose published-port NAT, where the host browser opens `http://127.0.0.1:4000/admin` but the container sees a bridge/gateway peer (e.g. `172.x` / `192.168.65.1`).

There is **no** second public admin port. If `Host` is a LAN or public address (e.g. `192.168.x.x:4000`), Apply is refused even when the peer is a Docker bridge IP.

- Open the UI via `http://127.0.0.1:<port>/admin` (or `http://localhost:<port>/admin`) on the machine running Harbor.
- Binding the edge to `0.0.0.0` still allows LAN clients to *view* `/admin` HTML/GET JSON; they cannot Apply (their `Host` is the LAN IP). Prefer `listen: "127.0.0.1:4000"` if you want the UI unreachable from the LAN.
- No password / remote-admin auth in v1 (out of scope).

When Harbor runs in Docker Compose with `ports: "4000:4000"`, open the UI at `http://127.0.0.1:4000/admin` on the **host**. Peer is not loopback inside the container, but `Host: 127.0.0.1:4000` satisfies the guard. Prefer not to publish `:4000` on a non-loopback interface if you want the UI unreachable from the LAN.

See also [SECURITY.md](../SECURITY.md).

## Restart behavior

After a successful Apply:

1. `gateway.yaml` is rewritten (PyYAML round-trip — **inline comments are not preserved**; same as `python -m edge models set`).
2. LiteLLM config is regenerated (`config/.generated_litellm.yaml`) when generation succeeds.
3. The API schedules an automatic restart of **edge + LiteLLM** via a detached helper:
   `python -m edge restart --delay 0.5 --detach` (logged to `logs/restart.log`).
4. Response includes `restart_scheduled: true` and a message that services are restarting.
   If spawning the helper fails, `restart_scheduled` is `false` and the message tells you to run:

```bash
python -m edge restart
```

There is a short downtime window (typically ~1–3s) while processes stop and come back. The Apply JSON is returned **before** processes are killed so the browser always gets a success response.

Harbor does **not** hot-reload router/cache/LiteLLM in-process (prefer a supervised process restart over a fragile reload).

## Implementation

| Piece | Path |
| --- | --- |
| Routes + guard + helpers | `edge/admin.py` |
| Static UI | `edge/admin_static/` (`index.html`, `app.js`, `style.css`) |
| Mount | `edge/main.py` → `mount_admin(app)` |
| Config knobs | `admin.open_browser` in `edge/config.py` / YAML |
| Save helper | `edge.config.save_raw_config` (+ `models_yaml.dump_raw_yaml`) |

## Out of scope (v1)

- First-run wizard polish
- Auth passwords for remote admin
- pipx / PyInstaller native binaries (still planned; Docker/GHCR is shipped — see [packaging.md](packaging.md))
- Rewriting router/cache logic
