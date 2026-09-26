# Feature: Packaging & distribution

**Status:** **Phase 1 shipped (Docker / GHCR).** pipx and native binaries remain planned. Admin UI is shipped (see [admin-ui.md](admin-ui.md)). Today Harbor runs from:

| Path | Status |
| --- | --- |
| **Source (venv)** | Shipped — `pip install -r requirements*.txt` + `python -m edge start` |
| **Docker** | **Shipped** — `docker compose up` / `ghcr.io/sarjeet2013/harbor-ai-gateway` |
| **pip / pipx** | Planned (Phase 2) |
| **Native binary** | Planned (Phase 3) |

Ollama stays **external** (any reachable host via `api_base`). No secrets in the image.

## What “run locally” means

Harbor is not a single static inference binary. A distribution must include or invoke:

| Piece | Role |
| --- | --- |
| **Edge** | Harbor FastAPI app (`edge/`) — budget, router, cache, ledger, translate |
| **LiteLLM** | Upstream proxy (typically managed child on `:4001`) |
| **Config** | `gateway.yaml` (from `gateway.yaml.example` / `gateway.docker.yaml.example` on first run) |
| **Runtime dirs** | `data/` (budget, cache, pid), `logs/` (ledger, process logs) |
| **Ollama** | **Not bundled** — user installs Ollama (or points at any OpenAI-compatible Ollama URL) |
| **Provider keys** | Optional `OPENAI_API_KEY` (etc.) via env / `.env` — never baked into artifacts |

Cross-platform goal: **any OS with Docker and/or Python**, plus Ollama somewhere reachable — not Mac-specific, not tied to a particular LAN host.

## Non-goals (near term)

- Bundling Ollama or model weights inside Harbor (size, GPU, version skew)
- Electron / admin GUI as a prerequisite to run the gateway
- Rewriting the stack in Go/Rust solely for a tiny binary (LiteLLM remains Python)
- Shipping secrets or machine-specific `api_base` values inside release artifacts

## Phase ladder

Do not skip rungs: each phase is a usable distribution channel on its own.

### Phase 0 — Docs truth (prerequisite)

- Install matrix in README: **source (venv)** · **Docker** · **pip/pipx (later)** · **native binary (later)**
- Default examples use `http://127.0.0.1:11434`; document that `api_base` / `embedding_api_base` / judge `api_base` may point at any reachable Ollama
- **Done** via README + feature docs

### Phase 1 — Docker as first-class “download and run” — **SHIPPED**

Harden Docker assets and publish an image:

- One service runs `python -m edge start` (foreground): edge `:4000` + managed LiteLLM `:4001` inside the container
- `docker compose up` publishes `:4000`, persists `data/` + `logs/`, mounts `config/`
- Image: **`ghcr.io/sarjeet2013/harbor-ai-gateway`** (GitHub Actions on `v*` tags, `main`, and `workflow_dispatch`)
- Host Ollama:
  - Docker Desktop: `http://host.docker.internal:11434` (`config/gateway.docker.yaml.example`)
  - Linux: compose `extra_hosts: host.docker.internal:host-gateway`
- No secrets in the image; optional `.env` via compose `env_file`
- First run seeds `config/gateway.yaml` from the Docker template when missing
- Admin UI: `http://127.0.0.1:4000/admin` when port published; localhost mutation guard still applies from the host browser to the published port

**Success criterion:** A new user with Docker + Ollama gets a chat completion through Harbor in under ~10 minutes with **no git clone** (pull image + compose + seed config).

#### How to run (Phase 1)

**From a clone (local build):**

```bash
cp .env.example .env   # optional OPENAI_API_KEY
docker compose up --build
curl -s http://127.0.0.1:4000/healthz
```

**Pull published image (no clone required once you have compose + config dir):**

```bash
mkdir -p config data logs
# optional: curl the compose file and gateway.docker.yaml.example from the repo
docker pull ghcr.io/sarjeet2013/harbor-ai-gateway:latest
docker compose up
```

If GHCR package visibility is private on first publish, log in:

```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin
```

Tag a release to publish a semver image: `git tag v0.1.0 && git push origin v0.1.0`.

### Phase 2 — `pip install` / `pipx` *(best cross-OS Python path)* — **planned**

- Add a real `pyproject.toml` with a console script, e.g. `harbor` → same CLI surface as `python -m edge`
- Publish to **PyPI** on version tags
- `pipx install harbor-ai-gateway` then `harbor start`
- Declare Python version floor (e.g. 3.11+); pin LiteLLM and runtime deps in package metadata
- First run copies `gateway.yaml.example` → user config path if missing (same behavior as edge CLI / Docker today)

**Success criterion:** No Docker required; still requires a Python toolchain.

### Phase 3 — Native executables (GitHub Releases) — **planned**

Per-OS artifacts attached to releases (`harbor-macos-arm64`, `harbor-linux-x64`, …) via PyInstaller or Nuitka. See earlier design notes: artifact size (LiteLLM), Gatekeeper, config path resolution.

**Kill / demote criterion:** If frozen artifacts regularly exceed ~1.5GB or cold start exceeds ~30s, keep **Docker + pipx as primary**.

### Phase 4 — OS package managers (optional polish)

Only after Phase 3 artifacts are stable: Homebrew tap, winget / Scoop.

## Layout (Phase 1)

```text
Dockerfile
docker-compose.yml
docker-entrypoint.sh
config/gateway.docker.yaml.example
.github/workflows/docker-publish.yml
docs/features/packaging.md   # this file
```

Phase 2+ may add `pyproject.toml`, `packaging/pyinstaller/`, etc.

## Launch recommendation

Advertise **Docker + source (venv)** first; list pipx and native Releases as follow-on.

## Implementation status

| Phase | Status |
| --- | --- |
| 0 Docs / generic defaults | **Done** |
| 1 Docker / GHCR | **Shipped** |
| 2 PyPI / pipx | **Not started** |
| 3 Native binaries | **Not started** |
| 4 Homebrew / winget | **Not started** |

When a later phase ships, update this table and the architecture “planned” section in the same PR.
