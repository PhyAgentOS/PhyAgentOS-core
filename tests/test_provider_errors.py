"""Behaviour of the shared provider error classifier.

These tests pin two things at once: real provider failures stay diagnosable,
and credentials, bearer tokens, key/token/password fields and URLs never reach
the message that is shown to the user or written to a log.
"""

import httpx
import openai
import pytest

from PhyAgentOS.providers.base import LLMProvider
from PhyAgentOS.providers.errors import describe_provider_error


def _status_error(error_cls, status, message, *, body=None):
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(status, request=request, text=str(message))
    return error_cls(message, response=response, body=message if body is None else body)


def test_authentication_error_is_short_and_redacted():
    error = _status_error(
        openai.AuthenticationError,
        401,
        "Incorrect API key provided: sk-LEAKCANARY1234567890 "
        "for https://user:pass@api.example.test/v1?token=LEAKCANARY",
    )

    message = describe_provider_error(error)

    assert "Authentication failed" in message
    assert "LEAKCANARY" not in message
    assert "sk-" not in message
    assert "api.example.test" not in message


def test_permission_error_is_treated_as_authentication():
    error = _status_error(
        openai.PermissionDeniedError,
        403,
        "forbidden: Bearer LEAKCANARY-BEARER-VALUE",
    )

    message = describe_provider_error(error)

    assert "Authentication failed" in message
    assert "LEAKCANARY" not in message


def test_connection_failure_is_network():
    error = httpx.ReadTimeout(
        "timed out talking to https://user:pass@api.example.test?token=LEAKCANARY"
    )

    message = describe_provider_error(error)

    assert "Network connection failure" in message
    assert "LEAKCANARY" not in message
    assert "api.example.test" not in message


def test_openai_connection_error_is_network():
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    error = openai.APIConnectionError(request=request)

    assert "Network connection failure" in describe_provider_error(error)


def test_rate_limit_is_rate_limited_without_body():
    error = _status_error(
        openai.RateLimitError,
        429,
        "Rate limit reached for default; api_key=sk-LEAKCANARY1234567890",
    )

    message = describe_provider_error(error)

    assert "rate limit" in message
    assert "LEAKCANARY" not in message


def test_server_error_keeps_retryable_marker_without_body():
    error = _status_error(
        openai.InternalServerError,
        503,
        "upstream failed at https://api.example.test/v1 with Bearer LEAKCANARY",
    )

    message = describe_provider_error(error)

    assert "server error" in message
    assert "LEAKCANARY" not in message
    assert LLMProvider._is_transient_error(message)


def test_404_is_not_reported_as_endpoint_configuration_error():
    error = _status_error(
        openai.NotFoundError,
        404,
        "The model `gpt-secret-model` does not exist; "
        "see https://user:pass@api.example.test/v1?api_key=LEAKCANARY",
    )

    message = describe_provider_error(error)

    assert "404" in message
    assert "not found" in message
    assert "Endpoint configuration error" not in message
    assert "LEAKCANARY" not in message
    assert "api.example.test" not in message


def test_context_length_rejection_is_actionable_not_endpoint_error():
    error = _status_error(
        openai.BadRequestError,
        400,
        "This model's maximum context length is 8192 tokens. "
        "However, your messages resulted in 9000 tokens.",
    )

    message = describe_provider_error(error)

    assert "context window" in message
    assert "Endpoint configuration error" not in message


@pytest.mark.parametrize(
    "payload",
    [
        "Invalid value for 'messages[0]': expected a string",
        "messages: text content blocks must be non-empty",
        "Invalid value for 'temperature': expected a number",
    ],
)
def test_invalid_request_400_keeps_sanitized_provider_message(payload):
    error = _status_error(
        openai.BadRequestError,
        400,
        payload,
        body={"error": {"message": payload}},
    )

    message = describe_provider_error(error)

    assert "HTTP 400" in message
    assert payload in message
    assert "Endpoint configuration error" not in message


@pytest.mark.parametrize(
    "payload",
    [
        "Authorization: Bearer sk-LEAKCANARY1234567890",
        "api_key=sk-LEAKCANARY1234567890",
        'token="LEAKCANARY-TOKEN-VALUE"',
        "password=LEAKCANARY-PASSWORD",
        "https://user:pass@api.example.test/v1?api_key=LEAKCANARY",
        "present sk-LEAKCANARY1234567890",
    ],
)
def test_unrecognized_400_redacts_credentials_and_urls(payload):
    error = _status_error(
        openai.BadRequestError,
        400,
        f"Invalid request: {payload}",
        body={"error": {"message": f"Invalid request: {payload}"}},
    )

    message = describe_provider_error(error)

    assert "HTTP 400" in message
    assert "LEAKCANARY" not in message
    assert "user:pass" not in message
    assert "api.example.test" not in message


def test_jwt_and_long_tokens_are_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlMTIzNDU2"
    long_token = "LEAKCANARY0123456789ABCDEFGHIJKLMNOPQRSTUV"
    error = _status_error(
        openai.BadRequestError,
        400,
        f"Invalid request: {jwt} {long_token}",
        body={"error": {"message": f"Invalid request: {jwt} {long_token}"}},
    )

    message = describe_provider_error(error)

    assert "eyJhbGci" not in message
    assert "LEAKCANARY0123456789" not in message


def test_unstructured_exception_reports_type_without_inventing_a_cause():
    message = describe_provider_error(RuntimeError("boom"))

    assert "Provider request failed" in message
    assert "RuntimeError" in message
    assert "boom" not in message
    assert "Endpoint configuration error" not in message


def test_string_detail_with_status_and_json_body_is_summarized():
    message = describe_provider_error(
        '400: {"error": {"message": "Invalid URL (POST /v1/chat/completions)"}}'
    )

    assert "HTTP 400" in message
    assert "Invalid URL (POST /v1/chat/completions)" in message
    assert "Endpoint configuration error" not in message


def test_unknown_400_numbers_do_not_become_retryable_statuses():
    message = describe_provider_error("400: max_tokens 5000 exceeds allowed value")

    assert "HTTP 400" in message
    assert not LLMProvider._is_transient_error(message)
