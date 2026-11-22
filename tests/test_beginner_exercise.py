"""A receipt, unknown result or invalid physical evidence must not pass a demo."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "example/beginner-simulation/exercise.py"
spec = importlib.util.spec_from_file_location("beginner_exercise", PATH)
exercise = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exercise)
NAMES = [f"joint{i}" for i in range(1, 7)]


class Client:
    def __init__(self, *, unknown=False, nonfinite=False, robot="piper"):
        self.calls = []
        self.unknown, self.nonfinite, self.robot = unknown, nonfinite, robot

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def invoke_query_tool(self, *_):
        return {
            "data": {
                "name": self.robot,
                "source": "mjcf",
                "joints": [{"name": name, "kind": "revolute", "limits": [-1, 1]} for name in NAMES],
                "initial_qpos": dict.fromkeys(NAMES, 0),
            }
        }

    async def get_tool_context(self, *_):
        return {"data": {"ready": True}}

    async def invoke_action(self, tool, arguments, **_):
        self.calls.append((tool, arguments))
        return {"data": {"invocation_id": str(len(self.calls))}}

    async def invocation_status(self, *_):
        return {"data": {"phase": "unknown" if self.unknown else "completed"}}

    async def invocation_result(self, *_):
        if self.unknown:
            return {"data": {"result": {"status": "unknown"}}}
        outputs = {"episode_reset": True, "rehomed": True}
        if self.calls[-1][0] == "sim.set_joint_positions":
            outputs = {
                "motion_result": {
                    "error_code": "SUCCESS",
                    "joint_names": NAMES,
                    "final_position_errors": [float("nan") if self.nonfinite else 0.001] * 6,
                }
            }
        return {"data": {"result": {"status": "succeeded", "outputs": outputs}}}


class ExerciseTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_action_is_saved_as_failure_and_never_replayed(self):
        client = Client(unknown=True)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(exercise, "ForgeToolClient", return_value=client),
        ):
            report = Path(directory) / "report.json"
            with self.assertRaisesRegex(RuntimeError, "did not succeed"):
                await exercise.exercise("http://127.0.0.1:19002", report)
            data = json.loads(report.read_text())
            self.assertFalse(data["passed"])
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(data["actions"][0]["result"]["data"]["result"]["status"], "unknown")

    async def test_nonfinite_joint_evidence_cannot_be_accepted(self):
        client = Client(nonfinite=True)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(exercise, "ForgeToolClient", return_value=client),
        ):
            report = Path(directory) / "report.json"
            with self.assertRaisesRegex(RuntimeError, "Joint error"):
                await exercise.exercise("http://127.0.0.1:19002", report)
            self.assertFalse(json.loads(report.read_text())["passed"])
            self.assertEqual(len(client.calls), 2)

    async def test_wrong_robot_never_receives_a_motion(self):
        client = Client(robot="another_robot")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(exercise, "ForgeToolClient", return_value=client),
        ):
            with self.assertRaisesRegex(ValueError, "Piper"):
                await exercise.exercise("http://127.0.0.1:19002", Path(directory) / "report.json")
            self.assertEqual(client.calls, [])

    async def test_success_requires_all_three_terminal_outcomes(self):
        client = Client()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(exercise, "ForgeToolClient", return_value=client),
        ):
            report = Path(directory) / "report.json"
            await exercise.exercise("http://127.0.0.1:19002", report)
            data = json.loads(report.read_text())
            self.assertTrue(data["passed"])
            self.assertEqual(
                [c[0] for c in client.calls], ["sim.reset", "sim.set_joint_positions", "sim.reset"]
            )
            self.assertEqual(client.calls[1][1]["positions"], [0.2, 0, 0, 0, 0, 0])
            self.assertTrue(all(row["passed"] for row in data["actions"]))
