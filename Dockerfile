# Harbor AI Gateway — Phase 1 image (edge + managed LiteLLM via `python -m edge start`).
# Ollama is external (host or remote). No secrets baked in.
FROM python:3.12-slim

WORKDIR /app

# System deps for some wheels; keep slim.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-litellm.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-litellm.txt

COPY edge ./edge
COPY config/gateway.yaml.example config/gateway.docker.yaml.example config/gateway.mock.yaml /opt/harbor/
# Also ship examples under /app/config for image inspection / non-mounted runs
COPY config/gateway.yaml.example config/gateway.docker.yaml.example config/gateway.mock.yaml ./config/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/data /app/logs \
    && chmod -R a+rX /opt/harbor /app/config

ENV PYTHONUNBUFFERED=1 \
    HARBOR_DOCKER=1 \
    GATEWAY_CONFIG=/app/config/gateway.yaml

EXPOSE 4000

ENTRYPOINT ["docker-entrypoint.sh"]
# Foreground start: edge + LiteLLM children; SIGTERM stops both (see edge/cli.py).
CMD ["python", "-m", "edge", "start"]
