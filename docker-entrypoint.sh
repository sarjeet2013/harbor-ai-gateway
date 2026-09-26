#!/bin/sh
# Harbor container entrypoint: seed config templates into a mounted config/
# volume, then exec the CMD (default: python -m edge start).
set -e

mkdir -p /app/data /app/logs /app/config

# Templates baked at /opt/harbor survive a bind-mount over /app/config.
if [ -d /opt/harbor ]; then
  for f in gateway.yaml.example gateway.docker.yaml.example gateway.mock.yaml; do
    if [ -f "/opt/harbor/$f" ] && [ ! -f "/app/config/$f" ]; then
      cp "/opt/harbor/$f" "/app/config/$f"
    fi
  done
fi

# First-run: seed gateway.yaml (CLI also does this; do it here so GATEWAY_CONFIG
# overrides still see a file when users mount an empty config dir).
if [ ! -f /app/config/gateway.yaml ]; then
  if [ -f /opt/harbor/gateway.docker.yaml.example ]; then
    cp /opt/harbor/gateway.docker.yaml.example /app/config/gateway.yaml
    echo "seeded /app/config/gateway.yaml from gateway.docker.yaml.example"
  elif [ -f /app/config/gateway.docker.yaml.example ]; then
    cp /app/config/gateway.docker.yaml.example /app/config/gateway.yaml
    echo "seeded /app/config/gateway.yaml from gateway.docker.yaml.example"
  elif [ -f /opt/harbor/gateway.yaml.example ]; then
    cp /opt/harbor/gateway.yaml.example /app/config/gateway.yaml
    echo "seeded /app/config/gateway.yaml from gateway.yaml.example"
  elif [ -f /app/config/gateway.yaml.example ]; then
    cp /app/config/gateway.yaml.example /app/config/gateway.yaml
    echo "seeded /app/config/gateway.yaml from gateway.yaml.example"
  fi
fi

export HARBOR_DOCKER="${HARBOR_DOCKER:-1}"
exec "$@"
