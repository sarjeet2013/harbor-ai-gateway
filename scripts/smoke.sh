#!/usr/bin/env bash
# Smoke-test OpenAI chat, Anthropic messages, and OpenAI responses against the edge.
set -euo pipefail

BASE="${GATEWAY_BASE_URL:-http://127.0.0.1:4000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHY="${WHY_LOG_PATH:-$ROOT/logs/why.jsonl}"
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

echo "== OpenAI /v1/chat/completions =="
CCODE=$(curl -s -o /tmp/gw_chat.json -w "%{http_code}" \
  -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local-gateway" \
  -d '{"model":"mock-cheap","messages":[{"role":"user","content":"hello from smoke chat"}]}' || echo "000")
py_check "POST /v1/chat/completions" "$CCODE" /tmp/gw_chat.json \
  'd.get("object")=="chat.completion" and "mock:" in d["choices"][0]["message"]["content"]'

echo "== Anthropic /v1/messages =="
ACODE=$(curl -s -o /tmp/gw_msg.json -w "%{http_code}" \
  -X POST "$BASE/v1/messages" \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-local-gateway" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"mock-cheap","max_tokens":64,"messages":[{"role":"user","content":"hello from smoke messages"}]}' || echo "000")
py_check "POST /v1/messages" "$ACODE" /tmp/gw_msg.json \
  'd.get("type")=="message" and d["content"][0]["type"]=="text"'

echo "== OpenAI /v1/responses (Codex) =="
RCODE=$(curl -s -o /tmp/gw_resp.json -w "%{http_code}" \
  -X POST "$BASE/v1/responses" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local-gateway" \
  -d '{"model":"mock-cheap","input":"hello from smoke responses"}' || echo "000")
py_check "POST /v1/responses" "$RCODE" /tmp/gw_resp.json \
  'd.get("object")=="response" and d.get("status")=="completed"'

echo "== why-log =="
if [[ -f "$WHY" ]] && python3 -c "
import json,sys
lines=open(sys.argv[1]).read().strip().splitlines()
assert lines, 'empty why-log'
ok=False
for line in lines[-20:]:
    r=json.loads(line)
    if r.get('model') and r.get('reason'):
        ok=True
        break
assert ok, 'no recent line with model+reason'
print(lines[-1])
" "$WHY"; then
  echo "PASS  why-log has model + reason ($WHY)"
  PASS=$((PASS + 1))
else
  echo "FAIL  why-log missing or incomplete at $WHY"
  FAIL=$((FAIL + 1))
fi

echo "== expensive heuristic =="
ECODE=$(curl -s -o /tmp/gw_exp.json -w "%{http_code}" \
  -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"ignored","messages":[{"role":"user","content":"please refactor the architecture"}]}' || echo "000")
py_check "expensive heuristic chat" "$ECODE" /tmp/gw_exp.json \
  '"mock:" in d["choices"][0]["message"]["content"]'

# Confirm why-log recorded expensive pool for that request
if python3 -c "
import json,sys
lines=open(sys.argv[1]).read().strip().splitlines()
r=json.loads(lines[-1])
assert r.get('pool')=='expensive' or 'keyword' in (r.get('reason') or ''), r
" "$WHY" 2>/tmp/gw_smoke_err; then
  echo "PASS  why-log pool/reason reflects expensive heuristic"
  PASS=$((PASS + 1))
else
  echo "FAIL  why-log did not record expensive routing"
  cat /tmp/gw_smoke_err 2>/dev/null || true
  FAIL=$((FAIL + 1))
fi

echo
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
