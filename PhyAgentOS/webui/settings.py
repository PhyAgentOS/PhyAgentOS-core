"""Read/write helpers for the local settings WebUI.

The browser never receives a secret value. It receives a redacted config, the
JSON schema for rendering the form, and a map of secret paths whose boolean
says whether a value is currently stored. On save a blank secret keeps the
stored value unless the client names the path in ``clear_secrets``.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from PhyAgentOS.config.loader import config_write_lock, load_config, save_config
from PhyAgentOS.config.schema import Config

# Containers whose entries commonly carry credentials. Every entry is treated
# as a secret: showing a header value wholesale is never safe.
_HEADER_CONTAINERS = {"headers", "extraheaders", "env", "environment"}
_SECRET_WORDS = ("secret", "password", "passwd", "credential", "privatekey")

_MISSING = object()


class SettingsError(Exception):
    """Base class for settings failures that map to HTTP errors."""


class RevisionMismatchError(SettingsError):
    """The config file changed since the client read it."""


class InvalidSecretPathError(SettingsError):
    """A clear_secrets entry does not name a known secret field."""


class InvalidSettingsError(SettingsError):
    """The submitted or stored configuration cannot be used."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def is_secret_name(name: str) -> bool:
    """Match credential-shaped field names without matching token budgets."""
    normalized = _normalize_name(name)
    if normalized in {"key", "token"}:
        return True
    if normalized.endswith(("key", "token")):
        return True
    return any(word in normalized for word in _SECRET_WORDS)


def is_header_container(name: str) -> bool:
    return _normalize_name(name) in _HEADER_CONTAINERS


def _unwrap_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        args = [item for item in get_args(annotation) if item is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _mapping_value_type(annotation: Any) -> Any:
    if get_origin(annotation) is not dict:
        return None
    args = get_args(annotation)
    return _unwrap_optional(args[1]) if len(args) == 2 else None


def _walk_secrets(value: Any, path: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Yield ``(dotted path, stored value)`` for every secret leaf."""
    if isinstance(value, BaseModel):
        for name, field in type(value).model_fields.items():
            alias = field.alias or name
            child = path + (str(alias),)
            raw = getattr(value, name)
            annotation = _unwrap_optional(field.annotation)
            if is_header_container(name):
                if isinstance(raw, dict):
                    for key, item in raw.items():
                        yield child + (str(key),), item
            elif get_origin(annotation) is dict:
                value_type = _mapping_value_type(annotation)
                if (
                    isinstance(raw, dict)
                    and isinstance(value_type, type)
                    and issubclass(value_type, BaseModel)
                ):
                    for key, item in raw.items():
                        yield from _walk_secrets(item, child + (str(key),))
            elif isinstance(raw, BaseModel):
                yield from _walk_secrets(raw, child)
            elif is_secret_name(name) and isinstance(raw, str):
                yield child, raw


def _redact_value(_: Any) -> str:
    return ""


def secret_fields(config: Config) -> dict[str, bool]:
    """Map each secret path to whether a non-blank value is stored."""
    result: dict[str, bool] = {}
    for path, raw in _walk_secrets(config, ()):
        result[".".join(path)] = isinstance(raw, str) and bool(raw.strip())
    return result


def redact_config(config: Config) -> dict[str, Any]:
    """Dump the config with every secret leaf replaced by an empty string."""
    data = config.model_dump(by_alias=True)
    for path, raw in _walk_secrets(config, ()):
        _set_path(data, path, _redact_value(raw))
    return data


def _set_path(tree: dict[str, Any], path: tuple[str, ...], value: Any) -> bool:
    node: Any = tree
    for segment in path[:-1]:
        nxt = node.get(segment) if isinstance(node, dict) else None
        if nxt is None:
            nxt = {}
            node[segment] = nxt
        if not isinstance(nxt, dict):
            return False
        node = nxt
    if not isinstance(node, dict):
        return False
    node[path[-1]] = value
    return True


def _get_path(tree: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = tree
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return _MISSING
        node = node[segment]
    return node


def _parent_exists(tree: dict[str, Any], path: tuple[str, ...]) -> bool:
    """True when the submitted object that should hold this leaf is still there.

    A ``None`` container (for example optional ``extraHeaders``) counts as
    present-but-blank, so stored secrets are restored instead of dropped.
    """
    node: Any = tree
    for segment in path[:-1]:
        if not isinstance(node, dict) or segment not in node:
            return False
        node = node[segment]
    return isinstance(node, dict) or node is None


def revision_of(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_raw(config_path: Path) -> bytes:
    try:
        return config_path.read_bytes()
    except FileNotFoundError:
        return b""


def validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Return pydantic errors without the rejected input or its context."""
    return [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg", "invalid value"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


def snapshot(config_path: Path) -> tuple[Config, bytes]:
    """Read the config and the exact bytes its revision is computed from."""
    raw = read_raw(config_path)
    try:
        config = load_config(config_path, strict=True)
    except (ValueError, ValidationError):
        raise InvalidSettingsError(
            "The existing configuration file is invalid; no values were returned."
        ) from None
    return config, raw


def _merge_secrets(
    current: Config,
    submitted: dict[str, Any],
    clear_secrets: Iterable[str],
) -> dict[str, Any]:
    cleared = {path.strip() for path in clear_secrets if path.strip()}
    current_secrets = list(_walk_secrets(current, ()))
    known = {".".join(path) for path, _ in current_secrets}
    unknown = sorted(cleared - known)
    if unknown:
        raise InvalidSecretPathError(f"Unknown secret field(s): {', '.join(unknown)}")

    merged = copy.deepcopy(submitted)
    for path, raw in current_secrets:
        dotted = ".".join(path)
        if dotted in cleared:
            _set_path(merged, path, "")
            continue
        # A deleted parent means the user removed that whole object; do not
        # resurrect it just to restore a secret.
        if not _parent_exists(merged, path):
            continue
        submitted_value = _get_path(merged, path)
        if submitted_value is _MISSING or not (
            isinstance(submitted_value, str) and submitted_value.strip()
        ):
            _set_path(merged, path, raw)
    return merged


def save_settings(
    config_path: Path,
    submitted: dict[str, Any],
    revision: str,
    clear_secrets: Iterable[str],
) -> tuple[Config, bytes]:
    """Validate and atomically save while holding the shared config lock."""
    path = config_path.expanduser()
    try:
        with config_write_lock(path) as locked:
            raw = read_raw(locked)
            if revision_of(raw) != revision:
                raise RevisionMismatchError(
                    "The configuration changed on disk; reload and try again."
                )
            try:
                current = load_config(locked, strict=True)
            except (ValueError, ValidationError):
                raise InvalidSettingsError(
                    "The existing configuration file is invalid; nothing was saved."
                ) from None

            merged = _merge_secrets(current, submitted, clear_secrets)
            try:
                config = Config.model_validate(merged)
            except ValidationError as exc:
                raise InvalidSettingsError(
                    "Configuration is invalid; nothing was saved.",
                    validation_errors(exc),
                ) from None
            save_config(config, locked)
            saved_raw = read_raw(locked)
    except BlockingIOError:
        raise RevisionMismatchError(
            "The configuration is being updated by another process; retry shortly."
        ) from None
    return config, saved_raw
