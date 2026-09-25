#!/usr/bin/env bash
# Prefer: python -m edge start
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export GATEWAY_CONFIG="${GATEWAY_CONFIG:-$ROOT/config/gateway.yaml}"
exec python -m edge start "$@"
