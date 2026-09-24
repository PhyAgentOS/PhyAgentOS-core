"""Shared AsyncOpenAI client construction for the direct (non-LiteLLM) providers.

CustomProvider and OpenAIResponsesProvider talk to an OpenAI-compatible
endpoint over the same SDK client; only the request/response shapes they
send differ. Building that client here keeps the two providers from
drifting apart on proxy handling, timeouts or retry semantics.
"""

from __future__ import annotations

import uuid

import httpx
from openai import AsyncOpenAI

DEFAULT_TIMEOUT_S = 180.0
DEFAULT_MAX_RETRIES = 2


def resolve_timeout_s(timeout_s: float | None) -> float:
    """One HTTP attempt's read timeout; ``None`` keeps the 180s default.

    The default assumes a model that answers in seconds; a reasoning model
    on a long turn can legitimately need minutes, and a call that exceeds
    the timeout is retried by the SDK before it ever returns, so a ceiling
    below the model's real latency converts every slow turn into several
    failed attempts (measured 2026-09-24: qwen3.8-max-0902, 3 x 180s = 540s,
    matching the run's recurring 541-543s gaps). Configure it per provider
    as ``providers.<name>.timeoutS``.
    """
    return DEFAULT_TIMEOUT_S if timeout_s is None else float(timeout_s)


def resolve_max_retries(max_retries: int | None) -> int:
    """SDK-level retries per call; ``None`` keeps the SDK default (2).

    0 leaves retrying to the framework's own ``chat_with_retry``, so the
    worst case for one logical call is a known multiple of the timeout
    instead of the SDK's own multiplication on top of it.
    """
    return DEFAULT_MAX_RETRIES if max_retries is None else int(max_retries)


def build_async_openai_client(
    api_key: str,
    api_base: str,
    timeout_s: float | None,
    max_retries: int | None,
) -> AsyncOpenAI:
    """Build the shared SDK client for direct OpenAI-compatible providers."""
    # trust_env=False avoids picking up a system SOCKS proxy that uses the
    # unsupported 'socks://' scheme (httpx only supports socks5://).
    http_client = httpx.AsyncClient(
        trust_env=False,
        timeout=httpx.Timeout(resolve_timeout_s(timeout_s), connect=15.0),
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
    )
    return AsyncOpenAI(
        api_key=api_key,
        base_url=api_base,
        default_headers={"x-session-affinity": uuid.uuid4().hex},
        max_retries=resolve_max_retries(max_retries),
        http_client=http_client,
    )
