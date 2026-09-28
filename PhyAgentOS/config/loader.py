"""Configuration loading utilities."""

import errno
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from PhyAgentOS.config.schema import Config

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".PhyAgentOS" / "config.json"


def load_config(config_path: Path | None = None, *, strict: bool = False) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.
        strict: Reject invalid JSON instead of falling back when editing settings.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            if strict:
                raise ValueError("Invalid configuration JSON; existing file was not changed.") from None
            print(f"Warning: Failed to load config from {path}: {e}")
            print("Using default configuration.")
        else:
            data = _migrate_config(data)
            return Config.model_validate(data)

    return Config()


@contextmanager
def config_write_lock(config_path: Path | None = None) -> Iterator[Path]:
    """Serialize provider read-modify-write transactions across processes.

    Lock a stable sidecar, never the atomically replaced config inode. Keep the
    critical section short: no prompts, API requests or runtime changes here.
    Closing the handle releases the lock even after an exception or a crash.
    """
    path = config_path or get_config_path()
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path = path.parent.resolve() / path.name
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.with_name(path.name + ".lock"), flags, 0o600)
    with os.fdopen(fd, "r+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError(
                    errno.EAGAIN, "Provider configuration is being updated; retry shortly."
                ) from None
            raise
        yield path


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True)

    # Readers and running processes must never see a partially written config.
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _migrate_config(data: dict) -> dict:
    """Migrate old config formats to current."""
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools = data.get("tools", {})
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")
    return data
