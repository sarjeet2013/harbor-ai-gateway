# Running Harbor with Ollama

**Hybrid default:** cheap = Ollama (`qwen3:8b` @ `http://127.0.0.1:11434`) · expensive = OpenAI (`openai/gpt-4o-mini`)  
**Ports:** edge `:4000` → LiteLLM `:4001` → Ollama `:11434` (+ OpenAI cloud for expensive)  
**Keys:** everyday Ollama needs **no** cloud key. For expensive/heuristic OpenAI: set `OPENAI_API_KEY` in `.env` (see `.env.example`).

Ollama can run on the **same machine** as Harbor or on **any reachable host**. Set `api_base` accordingly (template default is loopback).

## One-time setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-litellm.txt

cp config/gateway.yaml.example config/gateway.yaml
cp .env.example .env               # optional: OPENAI_API_KEY=...

# Ensure Ollama is running, then pull models
ollama serve                       # if not already a background service
ollama pull qwen3:8b
# optional coder / reason / embeddings:
# ollama pull qwen2.5-coder:14b deepseek-r1:14b nomic-embed-text

# Point cheap pool (defaults already use 127.0.0.1:11434)
python -m edge models set --pool cheap --model qwen3:8b --api-base http://127.0.0.1:11434
# Remote Ollama example:
# python -m edge models set --pool cheap --model qwen3:8b --api-base http://<ollama-host>:11434

python -m edge doctor
```

Confirm Ollama:

```bash
curl -s http://127.0.0.1:11434/api/tags | head
```

If Ollama listens on another host, expose it there (e.g. `OLLAMA_HOST=0.0.0.0:11434`) and use that URL as `api_base`.

## Config shape

`config/gateway.yaml` (copied from the example) should look like:

```yaml
models:
  cheap:
    - name: ollama-cheap
      litellm_model: ollama_chat/qwen3:8b
      api_base: http://127.0.0.1:11434
  expensive:
    - name: openai-expensive
      litellm_model: openai/gpt-4o-mini
      # litellm_gen sets api_key: os.environ/OPENAI_API_KEY
```

**Do not edit `config/litellm_config.yaml`.** The CLI generates `config/.generated_litellm.yaml`.

### Cloud API keys

- **Ollama** does not need `OPENAI_API_KEY`.
- **OpenAI** (default expensive): set `OPENAI_API_KEY` in `.env` or the environment ([platform.openai.com](https://platform.openai.com)). Without it, `python -m edge start` warns but still starts; cheap/Ollama works; expensive requests fail until the key is set. Never commit the key.
- Optional Grok: `xai/…` in YAML + `XAI_API_KEY`.

## Start / stop

```bash
source .venv/bin/activate          # Windows: .venv\Scripts\activate

python -m edge config-check
python -m edge stop
python -m edge start               # foreground
# or: python -m edge start --detach && python -m edge status
```

## Smoke test

```bash
curl -s http://127.0.0.1:4000/healthz

curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"ollama-cheap","messages":[{"role":"user","content":"Say hi in one short sentence."}],"max_tokens":64}'

# Expensive path (needs OPENAI_API_KEY for a real completion)
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Please refactor this architecture and design a system."}],"max_tokens":64}'

curl -s http://127.0.0.1:4000/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'x-api-key: sk-local-gateway' \
  -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"ollama-cheap","max_tokens":64,"messages":[{"role":"user","content":"Say hi in one short sentence."}]}'

./scripts/smoke.sh
tail -n 5 logs/ledger.jsonl
```

See also [docs/features/hybrid.md](docs/features/hybrid.md) and [docs/SECURITY.md](docs/SECURITY.md).
