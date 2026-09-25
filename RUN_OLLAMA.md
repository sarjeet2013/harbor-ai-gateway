# Run Harbor AI Gateway with Ollama (Mac)

**Hybrid default:** cheap = Mini LAN Ollama (`qwen3:8b` @ `192.168.4.31:11434`) · expensive = OpenAI (`openai/gpt-4o-mini`)  
**Ports:** edge `:4000` → LiteLLM `:4001` → Mini Ollama `:11434` (+ OpenAI cloud for expensive)  
**Config:** edit **only** `config/gateway.yaml` (models carry `litellm_model` + optional `api_base`)  
**Keys:** everyday Ollama needs **no** cloud key. For expensive/heuristic OpenAI: `export OPENAI_API_KEY=...`  
Details: **[docs/features/hybrid.md](docs/features/hybrid.md)**.

## One-time setup

```bash
# Cheap model lives on Mac Mini (already: qwen3:8b). From Air, confirm LAN:
curl -s http://192.168.4.31:11434/api/tags
# On Mini (once): OLLAMA_HOST=0.0.0.0:11434 and `ollama pull qwen3:8b`
# Air local Ollama (llama3.2:*) can stay; cheap pool does not use it.

# Clone / enter repo
cd ~/harbor-ai-gateway   # or wherever you cloned sarjeet2013/harbor-ai-gateway
git pull
python3 -m venv .venv
source .venv/bin/activate
# Core + LiteLLM (required — default gateway.yaml is upstream_mode: litellm)
pip install -r requirements.txt -r requirements-litellm.txt
# equivalent: pip install -r requirements.txt 'litellm[proxy]'
python -m edge doctor   # must report LiteLLM OK before start
mkdir -p logs data
```

Confirm `config/gateway.yaml` has `upstream_mode: litellm` and hybrid pools:

```yaml
models:
  cheap:
    - name: ollama-cheap
      litellm_model: ollama_chat/qwen3:8b
      api_base: http://192.168.4.31:11434
  expensive:
    - name: openai-expensive
      litellm_model: openai/gpt-4o-mini
      # litellm_gen sets api_key: os.environ/OPENAI_API_KEY
```

**Do not edit `config/litellm_config.yaml`.** The CLI generates `config/.generated_litellm.yaml` (override path with `GATEWAY_LITELLM_CONFIG`).

### Cloud API keys

- **Ollama** does not need `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. If those are set while all models are `ollama/*` or `ollama_chat/*`, the CLI prints a warning — ignore or unset.
- **OpenAI** (default expensive): `export OPENAI_API_KEY=...` (see [platform.openai.com](https://platform.openai.com)). Without it, `python -m edge start` warns but still starts; cheap/Ollama works; expensive requests fail until the key is set. Never commit the key — use shell env or `.env` (gitignored). Optional Grok: set `xai/…` in YAML + `XAI_API_KEY` (API access required; SuperGrok alone is not enough).

## Start / stop (one command)

```bash
cd ~/harbor-ai-gateway && source .venv/bin/activate

# Optional for hybrid expensive:
# export OPENAI_API_KEY=...

# Sanity: aliases must include ollama-cheap / openai-expensive
python -m edge config-check

# Stop any old manual uvicorn/litellm you had on :4000/:4001
python -m edge stop
# or: kill old terminals / lsof -ti tcp:4000,4001 | xargs kill

# Foreground (Ctrl+C stops both)
python -m edge start

# Or background (-d is the short form of --detach)
python -m edge start --detach
python -m edge status
python -m edge stop
```

## Smoke test

```bash
# Health
curl -s http://127.0.0.1:4000/healthz

# Cheap path (default) — works without OPENAI_API_KEY
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"ollama-cheap","messages":[{"role":"user","content":"Say hi in one short sentence."}],"max_tokens":64}'

# Expensive path (keyword trigger). Needs OPENAI_API_KEY for a real OpenAI completion.
# "auto" is not a registered alias — the router picks openai-expensive from gateway.yaml.
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Please refactor this architecture and design a system."}],"max_tokens":64}'

# Anthropic messages (Claude Code) — edge translates to chat/completions for Ollama
curl -s http://127.0.0.1:4000/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: sk-local-gateway' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"ollama-cheap","max_tokens":64,"messages":[{"role":"user","content":"Say hi in one short sentence."}]}'

# OpenAI responses (Codex) — also translated to chat/completions
curl -s http://127.0.0.1:4000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"ollama-cheap","input":"Say hi in one short sentence."}'

# Full smoke (cheap+stream always; expensive soft-SKIPs without OPENAI_API_KEY)
./scripts/smoke.sh

# Ledger
tail -n 5 logs/ledger.jsonl

```

Expect a real model reply on cheap (not a mock stub) and a ledger line with
`model` + `reason` (e.g. `ollama-cheap` / `heuristic:default_cheap`).

`/v1/messages` and `/v1/responses` are implemented on the edge and translated to `/v1/chat/completions` before LiteLLM, so Ollama does not need native Anthropic or Responses support.
