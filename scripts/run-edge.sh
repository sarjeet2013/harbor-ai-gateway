#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs data
export GATEWAY_CONFIG="${GATEWAY_CONFIG:-$ROOT/config/gateway.yaml}"
exec python -m uvicorn edge.main:app --host 0.0.0.0 --port 4000 "$@"
