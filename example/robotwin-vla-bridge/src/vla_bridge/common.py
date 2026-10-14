"""Shared config plumbing (RegistrationConfig + YAML loader).

Trimmed copy of vision-grasp nodes' common.py — only the pieces the bridge
needs; the registration announcer is byte-identical to the image_vision one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a node config file is missing or malformed."""


@dataclass(frozen=True)
class RegistrationConfig:
    enabled: bool
    response_timeout_ms: int
    initial_backoff_ms: int
    max_backoff_ms: int


def load_yaml(path: Any) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path} must be a YAML mapping")
    return raw


def parse_registration(raw: dict[str, Any]) -> RegistrationConfig:
    section = raw.get("registration") or {}
    return RegistrationConfig(
        enabled=bool(section.get("enabled", True)),
        response_timeout_ms=int(section.get("response_timeout_ms", 5000)),
        initial_backoff_ms=int(section.get("initial_backoff_ms", 250)),
        max_backoff_ms=int(section.get("max_backoff_ms", 10000)),
    )


__all__ = ["ConfigError", "RegistrationConfig", "load_yaml", "parse_registration"]
