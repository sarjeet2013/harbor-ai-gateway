# Run Harbor AI Gateway with Ollama (Mac)

**Models:** `llama3.2:1b` (cheap) · `llama3.2:3b` (expensive)  
**Ports:** edge `:4000` → LiteLLM `:4001` → Ollama `:11434`  
**Config:** edit **only** `config/gateway.yaml` (models carry `litellm_model` + `api_base`)

## One-time setup

```bash
# Pull models (~1–2 GB total on arm64)
ollama pull llama3.2:1b
ollama pull llama3.2:3b
ollama list

# Clone / enter repo
cd ~/local-llm-gateway   # or wherever you cloned sarjeet2013/local-llm-gateway
git pull
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt 'litellm[proxy]'
mkdir -p logs data
```

Confirm `config/gateway.yaml` has `upstream_mode: litellm` and unified pools:

```yaml
models:
  cheap:
    - name: ollama-cheap
      litellm_model: ollama/llama3.2:1b
      api_base: http://127.0.0.1:11434
  expensive:
    - name: ollama-expensive
      litellm_model: ollama/llama3.2:3b
      api_base: http://127.0.0.1:11434
```

**Do not edit `config/litellm_config.yaml`.** The CLI generates `config/.generated_litellm.yaml` (override path with `GATEWAY_LITELLM_CONFIG`).

### Cloud API keys

Ollama does not need `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. If those are set in your shell while all models are `ollama/*`, the CLI prints a warning — you can ignore or unset them for a cleaner env. The CLI does not strip your environment globally.

## Start / stop (one command)

```bash
cd ~/local-llm-gateway && source .venv/bin/activate

# Sanity: aliases must include ollama-cheap / ollama-expensive
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

# Cheap path (default)
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"ollama-cheap","messages":[{"role":"user","content":"Say hi in one short sentence."}],"max_tokens":64}'

# Expensive path (keyword trigger). "auto" is not a registered alias —
# the router picks the expensive pool head (ollama-expensive) from gateway.yaml.
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

# Why-log
tail -n 5 logs/why.jsonl

```

Expect a real model reply (not a mock stub) and a why-log line with
`model` + `reason` (e.g. `ollama-cheap` / `heuristic:default_cheap`).


`/v1/messages` and `/v1/responses` are implemented on the edge and translated to `/v1/chat/completions` before LiteLLM, so Ollama does not need native Anthropic or Responses support.
