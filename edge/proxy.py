from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class LiteLLMProxy:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _hop_headers(self, headers: dict[str, str] | None) -> dict[str, str]:
        """Headers sent to LiteLLM.

        Never forward client ``Authorization`` / ``x-api-key`` — those are Harbor
        client credentials (e.g. sk-local-gateway). LiteLLM would otherwise pass
        them through to OpenAI as the provider key and fail with a vague
        "Connection error". Provider auth comes from LiteLLM env/config
        (OPENAI_API_KEY, etc.).
        """
        hop = {"content-type": "application/json"}
        if headers:
            for k, v in headers.items():
                lk = k.lower()
                if lk in ("authorization", "x-api-key"):
                    continue
                if lk in (
                    "anthropic-version",
                    "openai-organization",
                ):
                    hop[k] = v
        return hop

    async def forward(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | str]:
        url = f"{self.base_url}{path}"
        hop = self._hop_headers(headers)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(method, url, json=json_body, headers=hop)
        try:
            data: dict[str, Any] | str = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data

    async def forward_stream(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes] | None, dict[str, Any] | str | None]:
        """Open an upstream streaming POST.

        On 2xx: returns (status, response_headers, byte_iterator, None).
        Caller must fully consume (or aclose) the iterator to release the client.

        On non-2xx: reads the error body, closes the connection, and returns
        (status, response_headers, None, parsed_error_body).
        """
        url = f"{self.base_url}{path}"
        hop = self._hop_headers(headers)
        client = httpx.AsyncClient(timeout=self.timeout)
        try:
            req = client.build_request(method, url, json=json_body, headers=hop)
            resp = await client.send(req, stream=True)
        except Exception:
            await client.aclose()
            raise

        out_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() in ("content-type", "x-request-id")
        }

        if resp.status_code >= 400:
            try:
                raw = await resp.aread()
            finally:
                await resp.aclose()
                await client.aclose()
            text = raw.decode("utf-8", errors="replace")
            try:
                err: dict[str, Any] | str = json.loads(text) if text else text
            except json.JSONDecodeError:
                err = text
            return resp.status_code, out_headers, None, err

        async def _aiter() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return resp.status_code, out_headers, _aiter(), None
