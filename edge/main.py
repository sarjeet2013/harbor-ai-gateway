from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .budget import DayBudget
from .cache import ResponseCache, cheap_pool_embedding_api_base, should_bypass_cache
from .config import GatewayConfig, load_config
from .mock_upstream import (
    mock_anthropic_message,
    mock_chat_completion,
    mock_chat_completion_stream,
    mock_responses,
)
from .proxy import LiteLLMProxy
from .router import pick_model
from .translate import (
    anthropic_sse_to_message,
    anthropic_to_chat,
    chat_to_anthropic,
    chat_to_responses,
    iter_anthropic_sse_from_cached_chat,
    iter_anthropic_sse_from_cached_message,
    iter_openai_sse_from_cached_chat,
    openai_chat_sse_to_anthropic_sse,
    openai_chat_sse_to_anthropic_sse_sync,
    openai_chat_sse_to_completion,
    responses_to_chat,
)
from .ledger import Ledger
from .admin import mount_admin

# Surfaces that support streaming in v1
_CHAT_SURFACE = "openai.chat.completions"
_MESSAGES_SURFACE = "anthropic.messages"
_STREAM_SURFACES = {_CHAT_SURFACE, _MESSAGES_SURFACE}


def _parse_usage_from_sse(raw: bytes) -> dict[str, Any]:
    """Best-effort: last SSE data JSON object that carries a usage field."""
    usage: dict[str, Any] = {}
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return usage
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
    return usage


def _tokens_from_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt": usage.get("prompt_tokens") or usage.get("input_tokens"),
        "completion": usage.get("completion_tokens") or usage.get("output_tokens"),
        "total": usage.get("total_tokens"),
    }


def create_app(cfg: GatewayConfig | None = None) -> FastAPI:
    config = cfg or load_config()
    budget = DayBudget(config)
    ledger = Ledger(config.ledger_path)
    proxy = LiteLLMProxy(config.litellm_base_url)

    def _route_ledger_fields(decision) -> dict:
        """Core route fields + optional judge extras for JSONL ledger rows."""
        row = {
            "model": decision.model,
            "pool": decision.pool,
            "reason": decision.reason,
        }
        row.update(decision.ledger_extras())
        return row
    cache = ResponseCache(
        config.cache,
        default_embedding_api_base=cheap_pool_embedding_api_base(config.models.cheap),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(title="Harbor AI Gateway", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.budget = budget
    app.state.ledger = ledger
    app.state.cache = cache

    mount_admin(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "upstream_mode": config.upstream_mode,
            "budget": budget.snapshot(),
            "cache": cache.stats(),
        }

    @app.get("/v1/budget")
    async def get_budget() -> dict[str, Any]:
        return budget.snapshot()

    async def _handle_stream(
        *,
        body: dict[str, Any],
        surface: str,
        path: str,
        headers: dict[str, str],
        decision: Any,
        client_model: Any,
        est: float,
        bypass: bool,
        translate: str | None = None,
    ) -> Response:
        """SSE for chat (passthrough) and Anthropic messages (chat upstream + remap).

        On successful complete streams, tee-store the reconstructed JSON body into
        the response cache (same key space as non-stream). Mid-stream errors and
        incomplete streams do not poison the cache.
        """

        async def _tee_and_record(
            chunks: AsyncIterator[bytes] | Iterator[bytes],
        ) -> AsyncIterator[bytes]:
            collected = bytearray()
            status = "ok"
            status_code = 200
            try:
                if hasattr(chunks, "__anext__"):
                    async for chunk in chunks:  # type: ignore[union-attr]
                        collected.extend(chunk)
                        yield chunk
                else:
                    for chunk in chunks:  # type: ignore[union-attr]
                        collected.extend(chunk)
                        yield chunk
            except Exception:
                status = "upstream_error"
                status_code = 502
                raise
            finally:
                usage = _parse_usage_from_sse(bytes(collected))
                tokens = _tokens_from_usage(usage) if usage else {}
                cost = est
                if status == "ok":
                    budget.record(cost)
                    # Store completed stream into the same cache as non-stream.
                    if cache.enabled and not bypass:
                        raw = bytes(collected)
                        stored: dict[str, Any] | None = None
                        if surface == _CHAT_SURFACE:
                            stored = openai_chat_sse_to_completion(raw)
                        elif surface == _MESSAGES_SURFACE:
                            stored = anthropic_sse_to_message(raw)
                        if stored is not None:
                            cache.set(
                                body,
                                model=decision.model,
                                pool=decision.pool,
                                surface=surface,
                                response=stored,
                            )
                ledger.write(
                    {
                        "surface": surface,
                        **_route_ledger_fields(decision),
                        "client_model": client_model,
                        "status": status,
                        "status_code": status_code,
                        "tokens": tokens,
                        "cost_usd": cost,
                        "spent_usd": budget.snapshot()["spent_usd"],
                        "cache_hit": False,
                        "cache_kind": "none",
                        "tokens_saved": 0,
                        "tokens_saved_prompt": 0,
                        "tokens_saved_completion": 0,
                        "stream": True,
                    }
                )

        # Anthropic messages stream: translate to chat, then remap SSE to Anthropic events.
        upstream_path = path
        upstream_body = body
        if translate == "anthropic":
            upstream_path = "/v1/chat/completions"
            upstream_body = anthropic_to_chat(body, decision.model)
            upstream_body["stream"] = True

        if config.upstream_mode == "mock":
            mock_iter = mock_chat_completion_stream(upstream_body, decision.model)
            if translate == "anthropic":
                mock_iter = openai_chat_sse_to_anthropic_sse_sync(
                    mock_iter, model=decision.model
                )
            return StreamingResponse(
                _tee_and_record(mock_iter),
                media_type="text/event-stream",
            )

        status_code, up_headers, aiter, err = await proxy.forward_stream(
            "POST", upstream_path, json_body=upstream_body, headers=headers
        )
        if aiter is None:
            # Non-2xx before body — surface as JSON error (not SSE)
            ledger.write(
                {
                    "surface": surface,
                    **_route_ledger_fields(decision),
                    "client_model": client_model,
                    "status": "upstream_error",
                    "status_code": status_code,
                    "tokens": {},
                    "cost_usd": est,
                    "spent_usd": budget.snapshot()["spent_usd"],
                    "cache_hit": False,
                    "cache_kind": "none",
                    "tokens_saved": 0,
                    "tokens_saved_prompt": 0,
                    "tokens_saved_completion": 0,
                    "stream": True,
                }
            )
            if isinstance(err, str):
                return Response(
                    content=err, status_code=status_code, media_type="text/plain"
                )
            return JSONResponse(err if err is not None else {"error": "upstream_error"}, status_code=status_code)

        media = up_headers.get("content-type") or up_headers.get("Content-Type") or "text/event-stream"
        # Prefer event-stream even if upstream omitted it
        if "event-stream" not in media and "json" not in media:
            media = "text/event-stream"
        out_iter: AsyncIterator[bytes] | Iterator[bytes] = aiter
        if translate == "anthropic":
            out_iter = openai_chat_sse_to_anthropic_sse(aiter, model=decision.model)
        return StreamingResponse(_tee_and_record(out_iter), media_type="text/event-stream")

    async def _handle(
        request: Request,
        *,
        surface: str,
        path: str,
        mock_fn,
        # litellm mode: translate Anthropic/Responses <-> chat/completions (Ollama-safe)
        translate: str | None = None,
    ) -> Response:
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        client_model = body.get("model")
        action, spent, remaining = budget.check()
        if action == "reject":
            ledger.write(
                {
                    "surface": surface,
                    "model": client_model,
                    "reason": "budget_reject",
                    "status": "rejected",
                    "spent_usd": spent,
                    "remaining_usd": remaining,
                    "cache_hit": False,
                    "cache_kind": "none",
                    "tokens_saved": 0,
                    "tokens_saved_prompt": 0,
                    "tokens_saved_completion": 0,
                }
            )
            return JSONResponse(
                {
                    "error": {
                        "type": "budget_exceeded",
                        "message": (
                            f"Daily budget ${config.daily_budget_usd} exceeded "
                            f"(spent ${spent:.4f})."
                        ),
                        "spent_usd": spent,
                        "daily_budget_usd": config.daily_budget_usd,
                    }
                },
                status_code=429,
            )

        # v1 streaming: chat + Anthropic messages. Responses still 501.
        if body.get("stream") is True and surface not in _STREAM_SURFACES:
            return JSONResponse(
                {
                    "error": {
                        "type": "streaming_not_supported",
                        "message": (
                            "stream:true is supported on POST /v1/chat/completions and "
                            "POST /v1/messages in Harbor v1. OpenAI Responses streaming "
                            "is planned. Omit stream or use chat/messages."
                        ),
                        "surface": surface,
                    }
                },
                status_code=501,
            )

        decision = pick_model(
            body,
            config,
            force_cheap=(action == "force_cheap"),
            client_model=client_model if isinstance(client_model, str) else None,
        )
        body = {**body, "model": decision.model}

        headers = {k: v for k, v in request.headers.items()}
        bypass = should_bypass_cache(body, headers)

        # Cache sits after router, before upstream — hits skip LiteLLM/mock.
        if cache.enabled and not bypass:
            hit = cache.get(
                body, model=decision.model, pool=decision.pool, surface=surface
            )
            if hit is not None:
                # Pure cache hit: do not increment day spend.
                want_stream = body.get("stream") is True and surface in _STREAM_SURFACES
                ledger.write(
                    {
                        "surface": surface,
                        **_route_ledger_fields(decision),
                        "client_model": client_model,
                        "status": "ok",
                        "status_code": 200,
                        "tokens": {
                            "prompt": hit.tokens_saved_prompt,
                            "completion": hit.tokens_saved_completion,
                            "total": hit.tokens_saved,
                        },
                        "cost_usd": 0.0,
                        "spent_usd": budget.snapshot()["spent_usd"],
                        "cache_hit": True,
                        "cache_kind": hit.kind,
                        "tokens_saved": hit.tokens_saved,
                        "tokens_saved_prompt": hit.tokens_saved_prompt,
                        "tokens_saved_completion": hit.tokens_saved_completion,
                        "stream": want_stream,
                    }
                )
                if want_stream:
                    # Replay cached complete JSON as surface-appropriate SSE.
                    if surface == _CHAT_SURFACE:
                        return StreamingResponse(
                            iter_openai_sse_from_cached_chat(hit.body),
                            media_type="text/event-stream",
                        )
                    if surface == _MESSAGES_SURFACE:
                        # Cached body is Anthropic message shape for this surface.
                        cached = hit.body
                        if cached.get("object") == "chat.completion" or (
                            isinstance(cached.get("choices"), list)
                        ):
                            # Defensive: convert OpenAI-shaped entry if present.
                            return StreamingResponse(
                                iter_anthropic_sse_from_cached_chat(
                                    cached, model=decision.model
                                ),
                                media_type="text/event-stream",
                            )
                        return StreamingResponse(
                            iter_anthropic_sse_from_cached_message(cached),
                            media_type="text/event-stream",
                        )
                return JSONResponse(hit.body, status_code=200)

        est = (
            config.cost_estimates_usd.cheap_per_request
            if decision.pool == "cheap"
            else config.cost_estimates_usd.expensive_per_request
        )

        # Streaming path (cache miss — tee upstream and store on complete)
        if body.get("stream") is True and surface in _STREAM_SURFACES:
            return await _handle_stream(
                body=body,
                surface=surface,
                path=path,
                headers=headers,
                decision=decision,
                client_model=client_model,
                est=est,
                bypass=bypass,
                translate=translate,
            )

        status_code = 200
        result: dict[str, Any] | str
        if config.upstream_mode == "mock":
            result = mock_fn(body, decision.model)
            status = "ok"
        else:
            upstream_path = path
            upstream_body = body
            if translate == "anthropic":
                upstream_path = "/v1/chat/completions"
                upstream_body = anthropic_to_chat(body, decision.model)
            elif translate == "responses":
                upstream_path = "/v1/chat/completions"
                upstream_body = responses_to_chat(body, decision.model)

            status_code, result = await proxy.forward(
                "POST", upstream_path, json_body=upstream_body, headers=headers
            )
            if (
                status_code < 400
                and isinstance(result, dict)
                and translate == "anthropic"
            ):
                result = chat_to_anthropic(result, decision.model)
            elif (
                status_code < 400
                and isinstance(result, dict)
                and translate == "responses"
            ):
                result = chat_to_responses(result, decision.model)
            status = "ok" if status_code < 400 else "upstream_error"

        cost = est
        if isinstance(result, dict):
            usage = result.get("usage") or {}
            # Prefer explicit cost if LiteLLM added it
            if "cost" in result:
                try:
                    cost = float(result["cost"])
                except (TypeError, ValueError):
                    pass
            tokens = _tokens_from_usage(usage)
        else:
            tokens = {}

        if status == "ok":
            budget.record(cost)
            # Store non-streaming successful JSON responses
            if (
                cache.enabled
                and not bypass
                and isinstance(result, dict)
                and status_code < 400
            ):
                cache.set(
                    body,
                    model=decision.model,
                    pool=decision.pool,
                    surface=surface,
                    response=result,
                )

        ledger.write(
            {
                "surface": surface,
                **_route_ledger_fields(decision),
                "client_model": client_model,
                "status": status,
                "status_code": status_code,
                "tokens": tokens,
                "cost_usd": cost,
                "spent_usd": budget.snapshot()["spent_usd"],
                "cache_hit": False,
                "cache_kind": "none",
                "tokens_saved": 0,
                "tokens_saved_prompt": 0,
                "tokens_saved_completion": 0,
            }
        )

        if isinstance(result, str):
            return Response(content=result, status_code=status_code, media_type="text/plain")
        return JSONResponse(result, status_code=status_code)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _handle(
            request,
            surface="openai.chat.completions",
            path="/v1/chat/completions",
            mock_fn=mock_chat_completion,
        )

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request) -> Response:
        # Claude Code / Anthropic SDK hit /v1/messages (base URL without /v1).
        # litellm mode: translate <-> chat/completions so Ollama always works.
        return await _handle(
            request,
            surface="anthropic.messages",
            path="/v1/messages",
            mock_fn=mock_anthropic_message,
            translate="anthropic",
        )

    @app.post("/v1/responses")
    async def openai_responses(request: Request) -> Response:
        # Codex wire_api=responses. litellm mode: translate <-> chat/completions.
        return await _handle(
            request,
            surface="openai.responses",
            path="/v1/responses",
            mock_fn=mock_responses,
            translate="responses",
        )

    def _model_list_payload() -> dict:
        """OpenAI-style /v1/models listing Harbor pool aliases (for Cursor etc.)."""
        data = []
        for pool_name, specs in (
            ("cheap", config.models.cheap),
            ("expensive", config.models.expensive),
        ):
            for spec in specs:
                data.append(
                    {
                        "id": spec.name,
                        "object": "model",
                        "owned_by": f"harbor-{pool_name}",
                        "litellm_model": spec.litellm_model,
                    }
                )
        return {"object": "list", "data": data}

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        return JSONResponse(_model_list_payload())

    # Cursor Override Base URL → …/cursor (Cursor cloud calls /cursor/chat/completions).
    # LiteLLM <~1.97 often has no /cursor route, so we serve Harbor's own chat/models
    # path (budget + router + cache + ledger) instead of blind-proxying LiteLLM /cursor.
    @app.api_route("/cursor/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def cursor_proxy(full_path: str, request: Request) -> Response:
        path = full_path.lstrip("/")
        if path.startswith("v1/"):
            path = path[3:]

        if path in ("models", "models/"):
            return JSONResponse(_model_list_payload())

        if path in ("chat/completions", "chat/completions/"):
            return await _handle(
                request,
                surface="openai.chat.completions",
                path="/v1/chat/completions",
                mock_fn=mock_chat_completion,
            )

        if config.upstream_mode == "mock":
            return JSONResponse(
                {
                    "error": (
                        "Cursor attach needs upstream_mode=litellm and a public HTTPS tunnel; "
                        "mock mode has no /cursor protocol. See config/clients/cursor.env.example"
                    )
                },
                status_code=501,
            )

        # Fallback: map remaining /cursor/* onto LiteLLM's OpenAI /v1/* surface
        body = None
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body = await request.json()
            except Exception:
                body = None
        status_code, result = await proxy.forward(
            request.method,
            f"/v1/{path}",
            json_body=body,
            headers={k: v for k, v in request.headers.items()},
        )
        if isinstance(result, str):
            return Response(content=result, status_code=status_code)
        return JSONResponse(result, status_code=status_code)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    cfg = load_config()
    # Allow override via env for docker
    host = os.environ.get("GATEWAY_HOST", cfg.host)
    port = int(os.environ.get("GATEWAY_PORT", str(cfg.port)))
    uvicorn.run("edge.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
