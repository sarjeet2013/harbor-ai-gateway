# Run local-llm-gateway with Ollama (Mac)

**Models:** `llama3.2:1b` (cheap) · `llama3.2:3b` (expensive)  
**Ports:** edge `:4000` → LiteLLM `:4001` → Ollama `:11434`

## One-time setup

```bash
# Pull models (~1–2 GB total on arm64)
ollama pull llama3.2:1b
ollama pull llama3.2:3b
ollama list

# Clone / enter repo
cd ~/local-llm-gateway   # or wherever you cloned sarjeet2013/local-llm-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt 'litellm[proxy]'
mkdir -p logs data
```

Confirm `config/gateway.yaml` has `upstream_mode: litellm` and pools
`ollama-cheap` / `ollama-expensive`, and `config/litellm_config.yaml` points at
`http://127.0.0.1:11434`.

## Start (two terminals)

```bash
# Terminal 1 — LiteLLM
cd ~/local-llm-gateway && source .venv/bin/activate
litellm --config config/litellm_config.yaml --port 4001 --host 127.0.0.1

# Terminal 2 — edge
cd ~/local-llm-gateway && source .venv/bin/activate
python -m uvicorn edge.main:app --host 0.0.0.0 --port 4000
```

Or background both:

```bash
cd ~/local-llm-gateway && source .venv/bin/activate
nohup litellm --config config/litellm_config.yaml --port 4001 --host 127.0.0.1 > logs/litellm.log 2>&1 &
nohup python -m uvicorn edge.main:app --host 0.0.0.0 --port 4000 > logs/edge.log 2>&1 &
```

## Smoke test

```bash
# Health
curl -s http://127.0.0.1:4000/healthz

# Cheap path (default)
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"ollama-cheap","messages":[{"role":"user","content":"Say hi in one short sentence."}],"max_tokens":64}'

# Expensive path (keyword trigger)
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Please refactor this architecture and design a system."}],"max_tokens":64}'

# Why-log
tail -n 5 logs/why.jsonl
```

Expect a real model reply (not a mock stub) and a why-log line with
`model` + `reason` (e.g. `ollama-cheap` / `heuristic:default_cheap`).
