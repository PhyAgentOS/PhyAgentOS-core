"""Safe provider diagnostics: never echo remote responses or credential-bearing URLs."""

import re


def describe_provider_error(error: object) -> str:
    text = f"{type(error).__name__} {error}".lower()
    if any(
        marker in text
        for marker in (
            "authentication",
            "unauthorized",
            "api key",
            "invalid token",
            "token refresh",
        )
    ) or re.search(r"\b(?:401|403)\b", text):
        return "Authentication failed; configure credentials or run provider login."
    if any(
        marker in text
        for marker in (
            "connection",
            "connecterror",
            "timeout",
            "timed out",
            "network",
            "dns",
            "ssl",
        )
    ):
        return "Network connection failure or timeout; check connectivity, proxy and TLS settings."
    if "rate limit" in text or re.search(r"\b429\b", text):
        return "Provider rate limit (429); check quota and retry later."
    if any(marker in text for marker in ("overloaded", "server error", "temporarily unavailable")) or re.search(r"\b(?:500|502|503|504)\b", text):
        return "Provider server error (503); retry later."
    if any(marker in text for marker in ("reasoning_effort", "reasoning.effort", "budget_tokens", "thinkingbudget")):
        return (
            "Provider rejected the reasoning settings; its capabilities may differ from the local catalog. "
            "Use /effort none (CLI: --reasoning-effort none) or choose a supported model."
        )
    if "model" in text or "deployment" in text:
        return "Model unavailable; check the model ID and account access."
    if any(marker in text for marker in ("404", "endpoint", "url", "400")):
        return "Endpoint configuration error; check API Base and API compatibility."
    return "Provider request failed; check service availability, quota and configuration."
