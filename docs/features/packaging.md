# Feature: Packaging & distribution

**Status:** **Planned (design only).** Not implemented yet. Today Harbor runs from a git checkout (or local copy) with a Python venv: `pip install -r requirements.txt` and `python -m edge start`. A `Dockerfile` / `docker-compose.yml` exist at the repo root but are not yet the polished first-class release path (no published GHCR image workflow documented here as shipping).

This document is the packaging plan for making Harbor usable **without** requiring every user to clone and wire dependencies by hand — while keeping **Ollama external** (any reachable host via `api_base`).

## What “run locally” means

Harbor is not a single static inference binary. A distribution must include or invoke:

| Piece | Role |
| --- | --- |
| **Edge** | Harbor FastAPI app (`edge/`) — budget, router, cache, ledger, translate |
| **LiteLLM** | Upstream proxy (typically managed child on `:4001`) |
| **Config** | `gateway.yaml` (from `gateway.yaml.example` on first run) |
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

- Install matrix in README: **source (venv)** · **Docker** · **pip/pipx** · **native binary (later)**
- Default examples use `http://127.0.0.1:11434`; document that `api_base` / `embedding_api_base` / judge `api_base` may point at any reachable Ollama
- This phase is largely covered by the OSS-facing README and feature docs; packaging.md is the distribution roadmap

### Phase 1 — Docker as first-class “download and run” *(fastest public win)*

Harden existing Docker assets and publish an image:

- `docker compose up` publishes edge `:4000`, persists `data/` + `logs/`, mounts config
- Publish image to **GHCR** (e.g. `ghcr.io/<org>/harbor-ai-gateway`) on version tags via GitHub Actions
- Compose examples for Ollama on the host:
  - Docker Desktop: `host.docker.internal:11434`
  - Linux: `host-gateway` / `extra_hosts` documented explicitly
- No secrets in the image; optional `.env` bind-mount

**Success criterion:** A new user with Docker + Ollama gets a chat completion through Harbor in under ~10 minutes with **no git clone**.

### Phase 2 — `pip install` / `pipx` *(best cross-OS Python path)*

- Add a real `pyproject.toml` with a console script, e.g. `harbor` → same CLI surface as `python -m edge` (`start`, `stop`, `status`, `doctor`, `models`, `cache-stats`, …)
- Publish to **PyPI** on version tags
- `pipx install harbor-ai-gateway` then `harbor start`
- Declare Python version floor (e.g. 3.11+); pin LiteLLM and runtime deps in package metadata
- First run copies `gateway.yaml.example` → user config path if missing (same behavior as edge CLI today)

**Success criterion:** No Docker required; still requires a Python toolchain. Primary path for developers who already use pipx.

### Phase 3 — Native executables (GitHub Releases)

Per-OS artifacts attached to releases:

| Artifact | Target |
| --- | --- |
| `harbor-macos-arm64` | Apple Silicon |
| `harbor-macos-x64` | Intel macOS |
| `harbor-linux-x64` | Linux x86_64 |
| `harbor-windows-x64.exe` | Windows x64 |

**Tooling (planned):** PyInstaller or Nuitka in CI (matrix build). Bundle edge + uvicorn stack + LiteLLM.

**UX:**

```text
harbor start
harbor start -d
harbor stop
harbor doctor
```

On first run, create config + `data/` + `logs/` beside the binary or under a user config dir (e.g. `~/.harbor/` — exact path TBD at implementation).

**Known hard parts (call out before building):**

- Artifact size: LiteLLM + deps often produce **hundreds of MB** (sometimes approaching ~1GB+)
- Native dependency wheels (pydantic, tiktoken, etc.) per platform
- Windows path length / firewall prompts
- macOS Gatekeeper: unsigned downloads warn; **notarization** is phase 3b, not a v1 blocker for “works if user allows”
- Config and template files must resolve correctly inside the frozen app

**Success criterion:** Download release archive → `./harbor doctor` → `./harbor start` → `GET :4000/healthz` OK.

**Kill / demote criterion:** If frozen artifacts regularly exceed ~1.5GB or cold start exceeds ~30s, keep **Docker + pipx as primary** and treat native binaries as best-effort.

### Phase 4 — OS package managers (optional polish)

Only after Phase 3 artifacts are stable:

- Homebrew tap (formula wrapping release binary or pipx)
- winget / Scoop on Windows

## Suggested repo layout (when implementation starts)

```text
packaging/
  docker/            # optional consolidation of root Dockerfile/compose
  pyinstaller/       # or nuitka/ specs
docs/features/packaging.md   # this file
.github/workflows/release.yml
pyproject.toml               # Phase 2
```

Root `Dockerfile` / `docker-compose.yml` may remain until Phase 1 consolidates them.

## Launch recommendation

For a public launch, **advertise Docker + pip/pipx first**; list native Releases as follow-on. That matches “any OS with Docker or Python, plus Ollama somewhere” without blocking on notarization or giant binary QA.

## Implementation status

| Phase | Status |
| --- | --- |
| 0 Docs / generic defaults | In progress via README + feature docs |
| 1 Docker / GHCR | **Not started** (compose/Dockerfile exist as drafts) |
| 2 PyPI / pipx | **Not started** |
| 3 Native binaries | **Not started** |
| 4 Homebrew / winget | **Not started** |

When a phase ships, update this table and the architecture “planned” section in the same PR.
