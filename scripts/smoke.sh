#!/usr/bin/env bash
# Smoke-test OpenAI chat, Anthropic messages, and OpenAI responses against the edge.
# Works with default Ollama/LiteLLM (config/gateway.yaml) and mock mode.
#   python -m edge start && ./scripts/smoke.sh
#   GATEWAY_CONFIG=config/gateway.mock.yaml python -m edge start && ./scripts/smoke.sh
set -euo pipefail

BASE="${GATEWAY_BASE_URL:-http://127.0.0.1:4000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LEDGER="${LEDGER_PATH:-${WHY_LOG_PATH:-$ROOT/logs/ledger.jsonl}}"
PASS=0
FAIL=0

py_check() {
  local name="$1"
  local code="$2"
  local body_file="$3"
  local py_expr="$4"
  if [[ ! "$code" =~ ^2 ]]; then
    echo "FAIL  $name — HTTP $code"
    echo "      body: $(head -c 500 "$body_file")"
    FAIL=$((FAIL + 1))
    return
  fi
  if python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
assert $py_expr
" "$body_file" 2>/tmp/gw_smoke_err; then
    echo "PASS  $name (HTTP $code)"
    PASS=$((PASS + 1))
  else
    echo "FAIL  $name — assertion failed"
    cat /tmp/gw_smoke_err 2>/dev/null || true
    echo "      body: $(head -c 500 "$body_file")"
    FAIL=$((FAIL + 1))
  fi
}

echo "Gateway: $BASE"
echo "== health =="
HCODE=$(curl -s -o /tmp/gw_health.json -w "%{http_code}" "$BASE/healthz" || echo "000")
py_check "GET /healthz" "$HCODE" /tmp/gw_health.json 'd.get("ok") is True'

MODE=$(python3 -c "import json; print(json.load(open('/tmp/gw_health.json')).get('upstream_mode') or '')" 2>/dev/null || true)
if [[ -n "${SMOKE_CHEAP_MODEL:-}" ]]; then
  CHEAP_MODEL="$SMOKE_CHEAP_MODEL"
elif [[ "$MODE" == "mock" ]]; then
  CHEAP_MODEL="mock-cheap"
else
  # Default gateway.yaml pools
  CHEAP_MODEL="ollama-cheap"
fi
echo "Mode: ${MODE:-unknown}  cheap model: $CHEAP_MODEL"

# Chat shape: real completion with non-empty assistant text.
# Mock mode also prefixes content with "mock:" — assert that when mode is mock.
if [[ "$MODE" == "mock" ]]; then
  CHAT_ASSERT='d.get("object")=="chat.completion" and "mock:" in (d["choices"][0]["message"].get("content") or "")'
  EXP_ASSERT='d.get("object")=="chat.completion" and "mock:" in (d["choices"][0]["message"].get("content") or "")'
else
  CHAT_ASSERT='d.get("object")=="chat.completion" and isinstance(d["choices"][0]["message"].get("content"), str) and len(d["choices"][0]["message"]["content"].strip())>0'
  EXP_ASSERT='d.get("object")=="chat.completion" and isinstance(d["choices"][0]["message"].get("content"), str) and len(d["choices"][0]["message"]["content"].strip())>0'
fi

echo "== OpenAI /v1/chat/completions =="
CCODE=$(curl -s -o /tmp/gw_chat.json -w "%{http_code}" \
  -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local-gateway" \
  -d "{\"model\":\"$CHEAP_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"hello from smoke chat\"}]}" || echo "000")
py_check "POST /v1/chat/completions" "$CCODE" /tmp/gw_chat.json "$CHAT_ASSERT"

echo "== Anthropic /v1/messages =="
ACODE=$(curl -s -o /tmp/gw_msg.json -w "%{http_code}" \
  -X POST "$BASE/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-local-gateway" \
  -H "anthropic-version: 2023-06-01" \
  -d "{\"model\":\"$CHEAP_MODEL\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"hello from smoke messages\"}]}" || echo "000")
py_check "POST /v1/messages" "$ACODE" /tmp/gw_msg.json \
  'd.get("type")=="message" and d["content"][0]["type"]=="text" and len((d["content"][0].get("text") or "").strip())>0'

echo "== OpenAI /v1/responses (Codex) =="
RCODE=$(curl -s -o /tmp/gw_resp.json -w "%{http_code}" \
  -X POST "$BASE/v1/responses" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local-gateway" \
  -d "{\"model\":\"$CHEAP_MODEL\",\"input\":\"hello from smoke responses\"}" || echo "000")
py_check "POST /v1/responses" "$RCODE" /tmp/gw_resp.json \
  'd.get("object")=="response" and d.get("status")=="completed"'

echo "== ledger =="
if [[ -f "$LEDGER" ]] && python3 -c "
import json,sys
lines=open(sys.argv[1]).read().strip().splitlines()
assert lines, 'empty ledger'
ok=False
for line in lines[-20:]:
    r=json.loads(line)
    if r.get('model') and r.get('reason'):
        ok=True
        break
assert ok, 'no recent ledger line with model+reason'
print(lines[-1])
" "$LEDGER"; then
  echo "PASS  ledger has model + reason ($LEDGER)"
  PASS=$((PASS + 1))
else
  echo "FAIL  ledger missing or incomplete at $LEDGER"
  FAIL=$((FAIL + 1))
fi

echo "== expensive heuristic =="
ECODE=$(curl -s -o /tmp/gw_exp.json -w "%{http_code}" \
  -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local-gateway" \
  -d '{"model":"ignored","messages":[{"role":"user","content":"please refactor the architecture"}]}' || echo "000")
py_check "expensive heuristic chat" "$ECODE" /tmp/gw_exp.json "$EXP_ASSERT"

# Confirm ledger recorded expensive pool for that request
if python3 -c "
import json,sys
lines=open(sys.argv[1]).read().strip().splitlines()
r=json.loads(lines[-1])
assert r.get('pool')=='expensive' or 'keyword' in (r.get('reason') or ''), r
" "$LEDGER" 2>/tmp/gw_smoke_err; then
  echo "PASS  ledger pool/reason reflects expensive heuristic"
  PASS=$((PASS + 1))
else
  echo "FAIL  ledger did not record expensive routing"
  cat /tmp/gw_smoke_err 2>/dev/null || true
  FAIL=$((FAIL + 1))
fi

echo "== OpenAI /v1/chat/completions stream =="
SCODE=$(curl -s -o /tmp/gw_chat_stream.txt -w "%{http_code}" \
  -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local-gateway" \
  -d "{\"model\":\"$CHEAP_MODEL\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"hello from smoke stream\"}]}" || echo "000")
if [[ "$SCODE" =~ ^2 ]] && grep -q "data:" /tmp/gw_chat_stream.txt && grep -q "\[DONE\]" /tmp/gw_chat_stream.txt; then
  echo "PASS  POST /v1/chat/completions stream (HTTP $SCODE, data: + [DONE])"
  PASS=$((PASS + 1))
elif [[ "$SCODE" =~ ^2 ]] && grep -q "data:" /tmp/gw_chat_stream.txt; then
  # litellm/Ollama may omit [DONE] depending on provider; require at least data:
  echo "PASS  POST /v1/chat/completions stream (HTTP $SCODE, data:)"
  PASS=$((PASS + 1))
else
  echo "FAIL  POST /v1/chat/completions stream — HTTP $SCODE or missing data:"
  echo "      body: $(head -c 500 /tmp/gw_chat_stream.txt 2>/dev/null || true)"
  FAIL=$((FAIL + 1))
fi

echo
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
