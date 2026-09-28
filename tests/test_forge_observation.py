from __future__ import annotations

import asyncio
import base64
import json
import unittest

from PhyAgentOS.forge.observation import ForgeObservationCollector, utc_now

PNG = b"\x89PNG\r\n\x1a\nsmall"


def image_message(sequence: int, data: bytes = PNG, source: str = "front") -> str:
    return json.dumps(
        {
            "type": "image",
            "id": source,
            "seq": sequence,
            "timestamp": float(sequence),
            "content_type": "image/png",
            "data": base64.b64encode(data).decode(),
        }
    )


class ForgeObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.collector = ForgeObservationCollector(
            "http://gateway",
            required_image_sources=["front"],
            max_artifact_bytes=1024,
        )

    async def test_deduplicates_sequences_and_enforces_after_boundary(self) -> None:
        await self.collector._handle_image_message(image_message(1))
        before = await self.collector.wait_for_before(0.1)
        await self.collector._handle_image_message(image_message(1))
        self.assertEqual((await self.collector.latest_snapshot()).images["front"].sequence, 1)
        terminal = utc_now()
        waiter = asyncio.create_task(
            self.collector.wait_for_after(
                before, terminal_observed_at=terminal, timeout_s=0.5
            )
        )
        await asyncio.sleep(0)
        await self.collector._handle_image_message(image_message(2))
        after = await waiter
        self.assertEqual(after.images["front"].sequence, 2)

    async def test_rejects_invalid_base64_and_media(self) -> None:
        payload = json.loads(image_message(1))
        payload["data"] = "%%%"
        await self.collector._handle_image_message(json.dumps(payload))
        self.assertTrue(self.collector.errors)
        self.assertFalse((await self.collector.latest_snapshot()).images)

    async def test_all_sources_must_cross_the_after_boundary(self) -> None:
        collector = ForgeObservationCollector(
            "http://gateway",
            required_image_sources=["front", "wrist"],
            max_artifact_bytes=1024,
        )
        await collector._handle_image_message(image_message(1, source="front"))
        await collector._handle_image_message(image_message(5, source="wrist"))
        before = await collector.wait_for_before(0.1)
        terminal = utc_now()
        waiter = asyncio.create_task(
            collector.wait_for_after(
                before, terminal_observed_at=terminal, timeout_s=0.5
            )
        )
        await collector._handle_image_message(image_message(2, source="front"))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        await collector._handle_image_message(image_message(6, source="wrist"))
        after = await waiter
        self.assertEqual(set(after.images), {"front", "wrist"})

    async def test_oversized_state_is_rejected(self) -> None:
        collector = ForgeObservationCollector(
            "http://gateway",
            required_image_sources=[],
            require_state=True,
            max_artifact_bytes=16,
        )
        await collector._handle_state_message(json.dumps({"state": "x" * 100}))
        self.assertTrue(collector.errors)
        self.assertIsNone((await collector.latest_snapshot()).state)

    async def test_stale_optional_state_is_not_labeled_as_after_evidence(self) -> None:
        await self.collector._handle_image_message(image_message(1))
        await self.collector._handle_state_message(json.dumps({"joint": [0]}))
        before = await self.collector.wait_for_before(0.1)
        terminal = utc_now()
        await self.collector._handle_image_message(image_message(2))
        after = await self.collector.wait_for_after(
            before, terminal_observed_at=terminal, timeout_s=0.1
        )
        self.assertIsNone(after.state)


if __name__ == "__main__":
    unittest.main()
