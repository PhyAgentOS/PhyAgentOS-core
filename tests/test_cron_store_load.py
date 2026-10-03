from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PhyAgentOS.cron.service import CronService


def _job(i: int) -> dict:
    return {
        "id": f"job{i}",
        "name": f"job {i}",
        "enabled": True,
        "schedule": {
            "kind": "every",
            "everyMs": 60000,
            "atMs": None,
            "expr": None,
            "tz": None,
        },
        "payload": {"kind": "agent_turn", "message": f"msg {i}", "deliver": False},
        "state": {},
        "createdAtMs": 1,
        "updatedAtMs": 1,
    }


class TestCronStoreLoad(unittest.IsolatedAsyncioTestCase):
    """A malformed store must not cost the user every scheduled job.

    start() calls _load_store() and then _save_store(), so anything that leaves
    the store empty in memory is written straight back over the file.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cron-store-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "cron.json"

    async def _start(self) -> CronService:
        service = CronService(store_path=self.path)
        self.addCleanup(service.stop)
        await service.start()
        return service

    def _on_disk(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))["jobs"]

    async def test_valid_store_round_trips(self) -> None:
        self.path.write_text(
            json.dumps({"version": 1, "jobs": [_job(1), _job(2)]}, indent=2), encoding="utf-8"
        )

        service = await self._start()

        self.assertEqual(len(service._store.jobs), 2)
        self.assertEqual(len(self._on_disk()), 2)

    async def test_one_malformed_job_does_not_discard_the_others(self) -> None:
        # The third entry has no "name", which used to raise KeyError and take
        # every other job with it.
        self.path.write_text(
            json.dumps(
                {"version": 1, "jobs": [_job(1), _job(2), {"id": "bad"}, _job(3)]}, indent=2
            ),
            encoding="utf-8",
        )

        service = await self._start()

        kept = self._on_disk()
        self.assertEqual([j["id"] for j in kept], ["job1", "job2", "job3"])
        self.assertEqual(len(service._store.jobs), 3)

    async def test_non_object_jobs_do_not_discard_the_others(self) -> None:
        self.path.write_text(
            json.dumps({"jobs": [_job(1), None, 42, "bad", [], _job(2)]}), encoding="utf-8"
        )

        service = await self._start()

        self.assertEqual([j.id for j in service._store.jobs], ["job1", "job2"])
        self.assertEqual([j["id"] for j in self._on_disk()], ["job1", "job2"])

    async def test_invalid_store_structure_is_preserved_for_recovery(self) -> None:
        invalid_stores = [None, [], 42, "bad", {"jobs": None}, {"jobs": {}}, {"jobs": "bad"}, {"jobs": 42}]
        for data in invalid_stores:
            with self.subTest(data=data):
                original = json.dumps(data)
                self.path.write_text(original, encoding="utf-8")

                await self._start()

                self.assertEqual(self._on_disk(), [])
                self.assertIn(original, [
                    p.read_text(encoding="utf-8")
                    for p in self.path.parent.glob("cron.json.corrupt*")
                ])

    async def test_failed_quarantine_does_not_overwrite_the_store(self) -> None:
        original = '{"jobs": [{"id": "recoverable"'
        self.path.write_text(original, encoding="utf-8")

        # Simulate a failed rename while leaving writes to the file possible.
        with patch.object(Path, "replace", side_effect=PermissionError("rename denied")):
            with self.assertRaises(PermissionError):
                await self._start()

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    async def test_exhausted_quarantine_names_do_not_overwrite_the_store(self) -> None:
        original = '{"jobs": [{"id": "recoverable"'
        self.path.write_text(original, encoding="utf-8")
        for n in range(1, 1000):
            self.path.with_name(f"cron.json.corrupt{n}").write_text("old backup", encoding="utf-8")

        with self.assertRaises(OSError):
            await self._start()

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.assertTrue(all(
            p.read_text(encoding="utf-8") == "old backup"
            for p in self.path.parent.glob("cron.json.corrupt*")
        ))

    async def test_unparseable_store_is_preserved_for_recovery(self) -> None:
        truncated = '{"version": 1, "jobs": [{"id": "job1", "name": "job 1"'
        self.path.write_text(truncated, encoding="utf-8")

        await self._start()

        # The service starts empty, but the unreadable content is moved aside
        # rather than overwritten, so it can still be recovered by hand.
        quarantined = sorted(p.name for p in Path(self._tmp.name).glob("cron.json.corrupt*"))
        self.assertEqual(len(quarantined), 1, f"expected one quarantined copy, got {quarantined}")
        kept = (Path(self._tmp.name) / quarantined[0]).read_text(encoding="utf-8")
        self.assertEqual(kept, truncated, "the quarantined copy is not the original content")

        # And the live store is a clean, valid, empty one.
        self.assertEqual(self._on_disk(), [])

    async def test_repeated_failures_do_not_overwrite_the_quarantined_copy(self) -> None:
        self.path.write_text("{ this is not json", encoding="utf-8")

        await self._start()
        first = sorted(p.name for p in Path(self._tmp.name).glob("cron.json.corrupt*"))
        # A second start sees a valid, empty store written by the first, so the
        # quarantined copy must still be the only one and must be untouched.
        await self._start()
        second = sorted(p.name for p in Path(self._tmp.name).glob("cron.json.corrupt*"))

        self.assertEqual(first, second)
        self.assertEqual(len(second), 1)


if __name__ == "__main__":
    unittest.main()
