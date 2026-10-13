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
    def __init__(self, *, transport, workers, decide, artifacts, experiences=None,
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
        self._experiences = json.loads(json.dumps(experiences or {}))
        self._experiences_digest = digest(self._experiences)

    def authorize(self, runtime):
        if runtime.client._client._transport is not self.transport:
            raise RuntimeError("private transport identity changed")
        if runtime.gateway_url != "http://benchmark.private":
            raise RuntimeError("private adapter binding changed")
        if digest(self._experiences) != self._experiences_digest:
            raise RuntimeError("frozen episodic experience changed")

    def experience_for(self, case):
        environment = case.get("environment", {})
        arm = environment.get("arm")
        if arm == "none":
            return []
        if arm != "fixed":
            raise ValueError("unsupported frozen-experience arm")
        task = environment.get("task_name")
        experience = self._experiences.get(task)
        if not experience or experience.get("task_name") != task:
            raise ValueError("missing task-local frozen episodic experience")
        return [copy.deepcopy(experience)]
