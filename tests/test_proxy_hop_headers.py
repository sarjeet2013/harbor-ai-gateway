"""Client auth must not be forwarded to LiteLLM (provider key comes from env)."""

from edge.proxy import LiteLLMProxy


def test_hop_headers_strips_client_authorization():
    p = LiteLLMProxy("http://127.0.0.1:4001")
    hop = p._hop_headers(
        {
            "Authorization": "Bearer sk-local-gateway",
            "x-api-key": "sk-local-gateway",
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "openai-organization": "org-test",
        }
    )
    lower = {k.lower(): v for k, v in hop.items()}
    assert "authorization" not in lower
    assert "x-api-key" not in lower
    assert lower.get("content-type") == "application/json"
    assert lower.get("anthropic-version") == "2023-06-01"
    assert lower.get("openai-organization") == "org-test"
