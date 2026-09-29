"""Real-process regression checks for provider configuration boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from PhyAgentOS.cli.commands import app
from PhyAgentOS.config.loader import config_write_lock, load_config, save_config
from PhyAgentOS.config.schema import Config, ProviderConfig
from PhyAgentOS.providers.service import ProviderService

_WORKER = """
import sys
import time
from pathlib import Path
from PhyAgentOS.config.schema import ProviderConfig
from PhyAgentOS.providers import service

path, ready, release = map(Path, sys.argv[1:4])
action = sys.argv[4]
original_load = service.load_config

def wait():
    ready.touch()
    deadline = time.monotonic() + 15
    while not release.exists():
        if time.monotonic() > deadline:
            raise RuntimeError('test synchronization timed out')
        time.sleep(0.01)

if action == 'write':
    def paused_load(*args, **kwargs):
        config = original_load(*args, **kwargs)
        wait()
        return config
    service.load_config = paused_load
    service.ProviderService.update(path, 'custom', ProviderConfig(
        api_base='http://localhost:9/v1', default_model='new-model',
    ))
else:
    snapshot = service.ProviderService.load(path)
    wait()
    assert snapshot.config.providers.custom.api_key == 'old-test-key'
    assert snapshot.selection('custom').model == 'old-model'
    assert snapshot.config.providers.openai.enabled
"""


def _start_worker(tmp_path: Path, path: Path, action: str):
    ready, release = tmp_path / "ready", tmp_path / "release"
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(path), str(ready), str(release), action],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return worker, ready, release


def _wait_ready(worker, ready):
    deadline = time.monotonic() + 15
    while not ready.exists():
        if worker.poll() is not None:
            stdout, stderr = worker.communicate()
            pytest.fail(f"worker exited before synchronization: {stdout} {stderr}")
        if time.monotonic() > deadline:
            pytest.fail("worker synchronization timed out")
        time.sleep(0.01)


def _finish_worker(worker, release):
    release.touch()
    try:
        stdout, stderr = worker.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.communicate()
        raise
    assert worker.returncode == 0, stdout + stderr


@pytest.fixture
def config_path(tmp_path):
    config = Config()
    config.agents.defaults.provider = "openai"
    config.agents.defaults.model = "gpt-5"
    config.providers.openai.api_key = "openai-test-key"
    config.providers.custom.api_key = "old-test-key"
    config.providers.custom.api_base = "http://localhost:9/v1"
    config.providers.custom.default_model = "old-model"
    path = tmp_path / "config.json"
    save_config(config, path)
    return path


@pytest.mark.parametrize("operation", ["update", "remove", "use"])
def test_provider_mutations_reject_concurrent_process_writes(tmp_path, config_path, operation):
    worker, ready, release = _start_worker(tmp_path, config_path, "write")
    try:
        _wait_ready(worker, ready)
        before = config_path.read_bytes()
        with pytest.raises(BlockingIOError):
            if operation == "update":
                ProviderService.update(config_path, "openai", ProviderConfig(api_key="new-test-key"))
            elif operation == "remove":
                ProviderService.remove(config_path, "openai")
            else:
                ProviderService.use(config_path, "custom", "old-model")
        assert config_path.read_bytes() == before
    finally:
        _finish_worker(worker, release)

    # A fresh retry reads the committed state; it cannot resurrect removed keys
    # or overwrite the other process's unrelated provider update.
    ProviderService.remove(config_path, "openai")
    config = load_config(config_path, strict=True)
    assert not config.providers.openai.enabled
    assert config.providers.openai.api_key == ""
    assert config.providers.custom.default_model == "new-model"
    if os.name != "nt":
        assert config_path.stat().st_mode & 0o777 == 0o600


def test_running_process_keeps_snapshot_after_external_update_and_removal(tmp_path, config_path):
    worker, ready, release = _start_worker(tmp_path, config_path, "snapshot")
    try:
        _wait_ready(worker, ready)
        ProviderService.update(config_path, "custom", ProviderConfig(
            api_base="http://localhost:8/v1", api_key="new-test-key", default_model="new-model",
        ))
        ProviderService.remove(config_path, "openai")
    finally:
        _finish_worker(worker, release)
    fresh = ProviderService.load(config_path)
    assert fresh.config.providers.custom.api_key == "new-test-key"
    assert fresh.selection("custom").model == "new-model"
    assert not fresh.config.providers.openai.enabled


def test_busy_cli_is_safe_and_different_configs_are_independent(config_path, tmp_path):
    other_path = tmp_path / "other.json"
    save_config(Config(), other_path)
    with config_write_lock(config_path):
        result = CliRunner().invoke(app, ["provider", "remove", "openai", "-c", str(config_path)])
        assert result.exit_code == 1
        assert "another process" in result.output
        assert "test-key" not in result.output
        ProviderService.update(other_path, "custom", ProviderConfig(api_base="http://localhost:9/v1"))
    ProviderService.remove(config_path, "openai")
    assert not json.loads(config_path.read_text())["providers"]["openai"]["enabled"]


def test_config_lock_is_released_after_writer_process_dies(tmp_path, config_path):
    worker, ready, _release = _start_worker(tmp_path, config_path, "write")
    try:
        _wait_ready(worker, ready)
    finally:
        worker.kill()
        worker.communicate(timeout=15)
    # The lock sidecar remains; its existence is not treated as lock ownership.
    assert config_path.with_name("config.json.lock").exists()
    ProviderService.remove(config_path, "openai")
    config = load_config(config_path, strict=True)
    assert not config.providers.openai.enabled
    assert config.providers.custom.default_model == "old-model"


def test_failed_write_releases_lock_and_preserves_file(config_path, monkeypatch):
    before = config_path.read_bytes()
    with monkeypatch.context() as patch:
        def fail_save(*_args):
            raise OSError("simulated disk failure")

        patch.setattr("PhyAgentOS.providers.service.save_config", fail_save)
        with pytest.raises(OSError, match="simulated disk failure"):
            ProviderService.remove(config_path, "openai")
    assert config_path.read_bytes() == before
    ProviderService.remove(config_path, "openai")
    assert not load_config(config_path, strict=True).providers.openai.enabled
