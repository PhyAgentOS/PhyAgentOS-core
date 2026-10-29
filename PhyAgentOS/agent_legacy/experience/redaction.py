"""Small, deterministic redaction helpers for persisted experience records."""

from __future__ import annotations

import hashlib
import re

_URL = re.compile(r"\b(?:https?|wss?)://[^\s<>()]+", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_TOKEN = re.compile(
    r"\b(?:sk|pk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b(?:api[_ -]?key|password|passwd|secret|access[_ -]?token|refresh[_ -]?token)"
    r"\s*[:=]\s*[^\s,;}]+",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(
    r"(?<![\w.])/(?:etc|home|root|data|var|opt|srv|tmp|Users)(?:/[^\s`]+)+"
)
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\s`]+\\)*[^\s`]+")
_EXECUTABLE_ID = re.compile(
    r"\b(?:command|session)_[A-Za-z0-9_-]{6,}\b", re.IGNORECASE
)
_ACTION_ASSIGNMENT = re.compile(
    r"\baction[_ -]?type\s*[:=]\s*[^\s,;}]+", re.IGNORECASE
)


def redact_text(value: str) -> str:
    """Remove endpoint and credential-shaped values while retaining semantic context."""
    text = _URL.sub("[REDACTED_ENDPOINT]", str(value))
    text = _BEARER.sub("Bearer [REDACTED_CREDENTIAL]", text)
    text = _TOKEN.sub("[REDACTED_CREDENTIAL]", text)
    text = _SECRET_ASSIGNMENT.sub("[REDACTED_CREDENTIAL]", text)
    text = _ABSOLUTE_PATH.sub("[REDACTED_PATH]", text)
    text = _WINDOWS_PATH.sub("[REDACTED_PATH]", text)
    text = _EXECUTABLE_ID.sub("[REDACTED_RECORD_ID]", text)
    return _ACTION_ASSIGNMENT.sub("[REDACTED_ACTION]", text)


def opaque_ref(value: str) -> str:
    """Persist an evidence fingerprint rather than a potentially sensitive locator."""
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]
    return f"evidence:{digest}"
