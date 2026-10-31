"""Safe provider diagnostics.

Known failure classes get a short, fixed message. Anything else keeps the
exception type, HTTP status and a one-line redacted message so the real
provider error is still diagnosable. Raw response bodies, credential-bearing
URLs, Authorization / Bearer values and key/token/password fields are never
echoed, and remote text is treated as data, never as instructions.
"""

import json
import re

# Values that must never reach a reply, a log line or a verification record.
_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s'\"<>]+", re.IGNORECASE)
_BEARER_RE = re.compile(
    r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE
)
_KEY_VALUE_RE = re.compile(
    r"\b(authorization|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|client[_-]?secret|private[_-]?key|token|password|passwd|secret)"
    r"\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)",
    re.IGNORECASE,
)
_SK_RE = re.compile(
    r"\b(?:sk-|sk_|ak-|AIza|AKIA|ghp_|github_pat_|xox[abp]-)[A-Za-z0-9._\-]{6,}"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
)
_LONG_TOKEN_RE = re.compile(
    r"(?<![\w\-])(?=[A-Za-z0-9_\-]*[0-9])(?=[A-Za-z0-9_\-]*[A-Za-z])"
    r"[A-Za-z0-9_\-]{32,}(?![\w\-])"
)
_LONG_ALPHA_RE = re.compile(r"(?<![\w\-])[A-Za-z]{40,}(?![\w\-])")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_STATUS_RE = re.compile(r"\b([45]\d\d)\b")
_PREFIX_RE = re.compile(
    r"^\s*(?:error\s+code:\s*[45]\d\d\s*-\s*|[45]\d\d\s*[-:]\s*)",
    re.IGNORECASE,
)
_DETAIL_LIMIT = 240

# These substrings make ``LLMProvider._is_transient_error`` retry. Unknown
# 4xx details must not smuggle them in, or a permanent failure would be retried.
_RETRY_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "rate limit",
    "overloaded",
    "timeout",
    "timed out",
    "connection",
    "server error",
    "temporarily unavailable",
)

_AUTH_MARKERS = (
    "authentication",
    "unauthorized",
    "api key",
    "invalid token",
    "token refresh",
    "permission denied",
    "forbidden",
)
_NETWORK_MARKERS = (
    "connection",
    "connecterror",
    "timeout",
    "timed out",
    "network",
    "dns",
    "ssl",
)
_REASONING_MARKERS = (
    "reasoning_effort",
    "reasoning.effort",
    "reasoning effort",
    "budget_tokens",
    "thinkingbudget",
)
_CONTEXT_MARKERS = (
    "context_length_exceeded",
    "context_length",
    "maximum context",
    "context length",
    "context window",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "reduce the length",
)
_POLICY_MARKERS = (
    "content policy",
    "content_policy",
    "content filter",
    "safety system",
    "moderation",
    "flagged",
)
_MODEL_MARKERS = (
    "model_not_found",
    "model not found",
    "no such model",
    "unknown model",
    "invalid model",
    "does not exist",
    "deploymentnotfound",
    "deployment not found",
)


def _status_code(error: object, text: str) -> int | None:
    for attr in ("status_code", "http_status", "status"):
        value = getattr(error, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    match = _STATUS_RE.search(text)
    return int(match.group(1)) if match else None


def _message_from_value(value: object, depth: int = 0) -> str:
    if depth > 3:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        if text[:1] in "{[":
            try:
                return _message_from_value(json.loads(text), depth + 1)
            except (TypeError, ValueError):
                return text
        return text
    if isinstance(value, dict):
        for key in ("message", "detail", "error", "msg", "reason"):
            if key in value:
                found = _message_from_value(value[key], depth + 1)
                if found:
                    return found
        return ""
    if isinstance(value, list):
        parts = (_message_from_value(item, depth + 1) for item in value[:3])
        return " ".join(part for part in parts if part)
    return ""


def _raw_message(error: object) -> str:
    if isinstance(error, str):
        text = error
    else:
        text = _message_from_value(getattr(error, "body", None)) or str(error)
    text = _PREFIX_RE.sub("", text.strip(), count=1)
    return _message_from_value(text) or text


def _sanitize(text: str) -> str:
    text = _CONTROL_RE.sub(" ", text)
    text = " ".join(text.split())
    text = _URL_RE.sub("<url>", text)
    text = _BEARER_RE.sub("<redacted>", text)
    text = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _SK_RE.sub("<redacted>", text)
    text = _JWT_RE.sub("<redacted>", text)
    text = _LONG_TOKEN_RE.sub("<redacted>", text)
    text = _LONG_ALPHA_RE.sub("<redacted>", text)
    if len(text) > _DETAIL_LIMIT:
        text = text[: _DETAIL_LIMIT - 3].rstrip() + "..."
    return text


def _neutralize_retry_markers(text: str) -> str:
    for marker in _RETRY_MARKERS:
        text = re.sub(re.escape(marker), "filtered", text, flags=re.IGNORECASE)
    return text


def _has_provider_detail(error: object, status: int | None) -> bool:
    # Plain strings are pre-formatted details from call sites such as Azure or
    # ProviderService.test; SDK errors expose ``body``/``status_code``.
    return isinstance(error, str) or status is not None or getattr(error, "body", None) is not None


_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
# Structured codes for the same failure class. Providers that report one of
# these get the canonical name the agent core uses for bounded recovery.
_CONTEXT_OVERFLOW_CODES = frozenset(
    {"context_length_exceeded", "context_window_exceeded"}
)


def _clean_code(value: object) -> str | None:
    """Return a short machine-shaped code, or None.

    Only identifier-shaped values survive: a response body, a message or a
    credential can never be mistaken for an error code. ``_sanitize`` is
    reused so a credential-shaped value is dropped rather than echoed.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not _CODE_RE.fullmatch(text):
        return None
    if _sanitize(text) != text:
        return None
    return text


def _code_from_value(value: object, depth: int = 0) -> str | None:
    """Read ``error.code`` (or a bare ``code``) from a decoded body."""
    if depth > 3:
        return None
    if isinstance(value, str):
        if value.strip()[:1] != "{":
            return None
        try:
            return _code_from_value(json.loads(value), depth + 1)
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            code = _clean_code(error.get("code"))
            if code is not None:
                return code
        return _clean_code(value.get("code"))
    return None


def _is_context_overflow_exception(error: object) -> bool:
    """True for SDKs that raise a dedicated overflow class (e.g. LiteLLM).

    The exception type is structured evidence from the SDK, not a match on
    provider prose, so it is safe to treat as a context-overflow signal.
    """
    return any(
        cls.__name__ == "ContextWindowExceededError" for cls in type(error).__mro__
    )


def extract_error_code(error: object) -> str | None:
    """Return the provider's structured error code, or ``None``.

    Sources, in order: a dedicated overflow exception class, the exception's
    ``code`` attribute, ``error.code`` in its decoded ``body``, then
    ``response.json()`` when that still carries the provider payload. Message
    text is never inspected, so prose such as "too many tokens" cannot
    authorize a context-overflow recovery. Known overflow codes are mapped to
    ``context_overflow``; every other code is returned only when it is short
    and identifier-shaped, so no response body can leak through this channel.
    """
    if _is_context_overflow_exception(error):
        return "context_overflow"

    # A decoded body (dict) or a JSON string body can be passed directly, as
    # the Azure provider does with ``response.json()``.
    code = _code_from_value(error) if isinstance(error, (dict, str)) else None
    if code is None:
        code = _clean_code(getattr(error, "code", None))
    if code is None:
        code = _code_from_value(getattr(error, "body", None))
    if code is None:
        response = getattr(error, "response", None)
        json_fn = getattr(response, "json", None)
        if callable(json_fn):
            try:
                code = _code_from_value(json_fn())
            except Exception:
                code = None

    if code is None:
        return None
    if code in _CONTEXT_OVERFLOW_CODES:
        return "context_overflow"
    return code


def describe_provider_error(error: object) -> str:
    raw = _raw_message(error)
    text = f"{type(error).__name__} {raw}".lower()
    status = _status_code(error, str(error) if not isinstance(error, str) else error)

    if any(marker in text for marker in _AUTH_MARKERS) or status in (401, 403):
        return "Authentication failed; configure credentials or run provider login."
    if status == 408 or any(marker in text for marker in _NETWORK_MARKERS):
        return "Network connection failure or timeout; check connectivity, proxy and TLS settings."
    if "rate limit" in text or "ratelimit" in text or status == 429:
        return "Provider rate limit (429); check quota and retry later."
    if (
        any(marker in text for marker in ("overloaded", "server error", "temporarily unavailable", "internalservererror"))
        or (status is not None and status >= 500)
    ):
        return "Provider server error; retry later."
    if any(marker in text for marker in _REASONING_MARKERS):
        return (
            "Provider rejected the reasoning settings; its capabilities may differ from the local catalog. "
            "Use /effort none (CLI: --reasoning-effort none) or choose a supported model."
        )
    if any(marker in text for marker in _CONTEXT_MARKERS):
        return (
            "Provider rejected the request: the prompt exceeds the model context window (HTTP 400). "
            "Shorten the conversation or choose a model with a larger context window."
        )
    if any(marker in text for marker in _POLICY_MARKERS):
        return "Provider request failed: the request was rejected under its content policy."
    if status == 404:
        return "Endpoint/Model not found (HTTP 404); check the model ID and API base."
    if status == 413:
        return "Provider rejected the request: payload too large (HTTP 413); reduce the request size."
    if any(marker in text for marker in _MODEL_MARKERS):
        return "Model unavailable; check the model ID and account access."

    type_name = "" if isinstance(error, str) else type(error).__name__
    detail = _sanitize(raw) if raw and _has_provider_detail(error, status) else ""
    if status is not None and status < 500:
        detail = _neutralize_retry_markers(detail)
    label = f"HTTP {status}" if status is not None else ""
    if type_name:
        label = f"{label}, {type_name}" if label else type_name
    if detail and label:
        return f"Provider request failed ({label}): {detail}"
    if detail:
        return f"Provider request failed: {detail}"
    if label:
        return f"Provider request failed ({label}); check service availability and configuration."
    return "Provider request failed; check service availability, quota and configuration."
