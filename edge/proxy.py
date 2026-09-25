from __future__ import annotations

from typing import Any

import httpx


class LiteLLMProxy:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def forward(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | str]:
        url = f"{self.base_url}{path}"
        hop = {"content-type": "application/json"}
        if headers:
            for k, v in headers.items():
                lk = k.lower()
                if lk in ("authorization", "x-api-key", "anthropic-version", "openai-organization"):
                    hop[k] = v
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(method, url, json=json_body, headers=hop)
        try:
            data: dict[str, Any] | str = resp.json()
        except Exception:
            data = resp.text
        return resp.status_code, data
