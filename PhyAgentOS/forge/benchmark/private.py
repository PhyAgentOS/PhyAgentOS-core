"""Host-injected private adapter authority; no public listener or URL opt-in."""

import copy
import json

import httpx

from PhyAgentOS.forge.interaction.contracts import digest


class PrivateBenchmarkTransport(httpx.AsyncBaseTransport):
    def __init__(self, adapter):
        self.adapter = adapter

    async def handle_async_request(self, request):
        if request.url.host != "benchmark.private" or request.url.scheme != "http":
            raise RuntimeError("private benchmark transport cannot access network hosts")
        return await self.adapter.handle(request)


class PrivateBenchmarkDeployment:
    def __init__(self, *, transport, workers, decide, artifacts, case_experiences=None,
                 control_timeout_s=300, poll_interval_s=0.2):
        if type(transport) is not PrivateBenchmarkTransport:
            raise ValueError("explicit private adapter transport required")
        if not workers or len(set(workers)) != len(workers):
            raise ValueError("distinct authorized workers required")
        self.transport = transport
        self.workers = tuple(workers)
        self.decide, self.artifacts = decide, artifacts
        self.reflect = self.validate_lesson = None
        self.control_timeout_s, self.poll_interval_s = control_timeout_s, poll_interval_s
        self._experiences = json.loads(json.dumps(case_experiences or {}, allow_nan=False))
        if not isinstance(self._experiences, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, list)
            for key, value in self._experiences.items()
        ):
            raise ValueError("case_experiences must map case keys to frozen lists")
        self._experiences_digest = digest(self._experiences)

    def authorize(self, runtime):
        if runtime.client._client._transport is not self.transport:
            raise RuntimeError("private transport identity changed")
        if runtime.gateway_url != "http://benchmark.private":
            raise RuntimeError("private adapter binding changed")
        if digest(self._experiences) != self._experiences_digest:
            raise RuntimeError("frozen episodic experience changed")

    def experience_for(self, case):
        """Optional host-owned context keyed by frozen case identity, independent of domain."""
        return copy.deepcopy(self._experiences.get(case["case_key"], []))
