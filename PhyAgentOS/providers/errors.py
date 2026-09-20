"""Safe provider diagnostics: never echo remote responses or credential-bearing URLs."""


def describe_provider_error(error: object) -> str:
    text = f"{type(error).__name__} {error}".lower()
    if any(
        marker in text
        for marker in (
            "401",
            "403",
            "authentication",
            "unauthorized",
            "api key",
            "invalid token",
            "token refresh",
        )
    ):
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
    if any(marker in text for marker in ("429", "rate limit")):
        return "Provider rate limit (429); check quota and retry later."
    if any(marker in text for marker in ("500", "502", "503", "504", "overloaded")):
        return "Provider server error (503); retry later."
    if "model" in text or "deployment" in text:
        return "Model unavailable; check the model ID and account access."
    if any(marker in text for marker in ("404", "endpoint", "url", "400")):
        return "Endpoint configuration error; check API Base and API compatibility."
    return "Provider request failed; check service availability, quota and configuration."
