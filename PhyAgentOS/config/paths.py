"""Instance data paths derived from the active config context."""

from __future__ import annotations

from pathlib import Path

from PhyAgentOS.config.loader import get_config_path
from PhyAgentOS.utils.helpers import ensure_dir


def get_data_dir() -> Path:
    """Return the instance-level data directory."""
    return ensure_dir(get_config_path().parent)


def get_data_subdir(name: str) -> Path:
    """Return a named subdirectory under the instance data directory."""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """Return the media directory, optionally namespaced per channel."""
    base = get_data_subdir("media")
    return ensure_dir(base / channel) if channel else base


def get_cron_dir() -> Path:
    """Return the cron storage directory."""
    return get_data_subdir("cron")


def get_logs_dir() -> Path:
    """Return the logs directory."""
    return get_data_subdir("logs")


def get_skill_bundle_root() -> Path:
    """Return the directory containing installed Skill bundles."""
    return get_data_subdir("skills")


def get_forge_runtime_root() -> Path:
    """Return the local Forge runtime installation directory."""
    return get_data_subdir("forge_runtime")


def get_artifact_cache_root() -> Path:
    """Return the content-addressed Resource Registry download cache."""
    return get_data_subdir("cache")


def get_skill_runtime_state_dir() -> Path:
    """Return the directory containing Skill runtime state."""
    return ensure_dir(get_data_subdir("run") / "skills")


def get_skill_runtime_logs_dir() -> Path:
    """Return the directory containing Skill runtime lifecycle logs."""
    return ensure_dir(get_logs_dir() / "skills")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure the agent workspace path."""
    path = Path(workspace).expanduser() if workspace else Path.home() / ".PhyAgentOS" / "workspace"
    return ensure_dir(path)


def get_cli_history_path() -> Path:
    """Return the shared CLI history file path."""
    return Path.home() / ".PhyAgentOS" / "history" / "cli_history"


def get_legacy_sessions_dir() -> Path:
    """Return the legacy global session directory used for migration fallback."""
    return Path.home() / ".PhyAgentOS" / "sessions"
