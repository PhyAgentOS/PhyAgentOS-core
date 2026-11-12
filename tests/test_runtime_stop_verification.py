import subprocess

import pytest
from test_skill_runtime_manager import _manager

from PhyAgentOS.skill_runtime.manager import RuntimeManagerError
from PhyAgentOS.skill_runtime.state import RuntimeState


def _state(manager, status="running"):
    state = RuntimeState(
        skill_name="move-arm-by-ee",
        profile="mujoco",
        status=status,
        flow_name="paos-move-arm-by-ee-mujoco",
        gateway_url="http://127.0.0.1:19002",
    )
    manager.state_store.save(state)
    return state


@pytest.mark.parametrize("status", ["running", "stopped"])
def test_missing_cli_cannot_report_a_successful_stop(tmp_path, monkeypatch, status):
    manager = _manager(tmp_path)
    _state(manager, status)
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(RuntimeManagerError, match="dora"):
        manager.stop("move-arm-by-ee")
    assert manager.state_store.load("move-arm-by-ee").status == "failed"


@pytest.mark.parametrize("output,code", [("garbage", 0), ("[]", 1)])
def test_failed_observation_is_not_an_absent_flow(tmp_path, monkeypatch, output, code):
    manager = _manager(tmp_path)
    monkeypatch.setattr("shutil.which", lambda _: "/fake/dora")
    monkeypatch.setattr(
        manager, "_run", lambda *args, **kwargs: subprocess.CompletedProcess(args, code, output, "")
    )
    with pytest.raises(RuntimeManagerError, match="Cannot confirm"):
        manager._flow_running("probe")


def test_successful_cli_exit_with_live_flow_is_failed_stop(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    _state(manager)
    monkeypatch.setattr("shutil.which", lambda _: "/fake/dora")
    monkeypatch.setattr(
        manager, "_run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", "")
    )
    monkeypatch.setattr(manager, "_flow_running", lambda _: True)
    with pytest.raises(RuntimeManagerError, match="still running"):
        manager.stop("move-arm-by-ee")
    assert manager.state_store.load("move-arm-by-ee").status == "failed"


def test_force_stop_cleans_owned_processes_even_if_dora_times_out(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    _state(manager)
    calls = []

    class Processes:
        def __init__(self, root, identity):
            calls.append((root, identity))
            self.live = True

        def snapshot(self):
            return {123: "owned-start-time"} if self.live else {}

        def terminate(self):
            calls.append("terminate")
            self.live = False
            return {}

    monkeypatch.setattr("PhyAgentOS.skill_runtime.manager.RuntimeProcessTracker", Processes)
    monkeypatch.setattr(
        manager,
        "_stop_flow",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeManagerError("timeout")),
    )
    monkeypatch.setattr(manager, "_flow_running", lambda _: False)
    assert manager.stop("move-arm-by-ee", force=True).status == "stopped"
    assert calls[-1] == "terminate"
