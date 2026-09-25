from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .budget import DayBudget
from .cache import ResponseCache, should_bypass_cache
from .config import GatewayConfig, load_config
from .mock_upstream import mock_anthropic_message, mock_chat_completion, mock_responses
from .proxy import LiteLLMProxy
from .router import pick_model
from .translate import (
    anthropic_to_chat,
    chat_to_anthropic,
    chat_to_responses,
    responses_to_chat,
)
from .ledger import Ledger


def create_app(cfg: GatewayConfig | None = None) -> FastAPI:
    config = cfg or load_config()
    budget = DayBudget(config)
    ledger = Ledger(config.ledger_path)
    proxy = LiteLLMProxy(config.litellm_base_url)
    cache = ResponseCache(config.cache)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(title="Harbor AI Gateway", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.budget = budget
    app.state.ledger = ledger
    app.state.cache = cache

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
                ledger.write(
                    {
                        "surface": surface,
                        "model": decision.model,
                        "pool": decision.pool,
                        "reason": decision.reason,
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
                    }
                )
                return JSONResponse(hit.body, status_code=200)

        est = (
            config.cost_estimates_usd.cheap_per_request
            if decision.pool == "cheap"
            else config.cost_estimates_usd.expensive_per_request
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
            tokens = {
                "prompt": usage.get("prompt_tokens") or usage.get("input_tokens"),
                "completion": usage.get("completion_tokens") or usage.get("output_tokens"),
                "total": usage.get("total_tokens"),
            }
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
                "model": decision.model,
                "pool": decision.pool,
                "reason": decision.reason,
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

    # LiteLLM Cursor path: clients may call /cursor/...; proxy through when litellm mode
    @app.api_route("/cursor/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def cursor_proxy(full_path: str, request: Request) -> Response:
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
        body = None
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body = await request.json()
            except Exception:
                body = None
        status_code, result = await proxy.forward(
            request.method,
            f"/cursor/{full_path}",
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
