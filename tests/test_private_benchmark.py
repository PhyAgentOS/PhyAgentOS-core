import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from PhyAgentOS.forge.benchmark.private import PrivateBenchmarkDeployment, PrivateBenchmarkTransport


def deployment():
    transport = PrivateBenchmarkTransport(None)
    return PrivateBenchmarkDeployment(transport=transport, workers=['worker'], decide=None, artifacts=None,
        case_experiences={'fixed-case': [{'reflection': 'conditional'}]})


def test_private_transport_identity_and_no_network():
    deployed = deployment()
    runtime = SimpleNamespace(client=SimpleNamespace(_client=SimpleNamespace(_transport=deployed.transport)),
        gateway_url='http://benchmark.private')
    deployed.authorize(runtime)
    runtime.client._client._transport = httpx.MockTransport(lambda request: httpx.Response(200))
    with pytest.raises(RuntimeError, match='identity'):
        deployed.authorize(runtime)


def test_task_scope_no_lesson_and_frozen_copy():
    deployed = deployment()
    assert deployed.experience_for({'case_key': 'none-case'}) == []
    case = {'case_key': 'fixed-case'}
    memory = deployed.experience_for(case)
    memory[0]['reflection'] = 'mutated'
    assert deployed.experience_for(case)[0]['reflection'] == 'conditional'
    assert deployed.experience_for({'case_key': 'different-case'}) == []


@pytest.mark.asyncio
async def test_private_transport_rejects_network_host():
    with pytest.raises(RuntimeError, match='network hosts'):
        await deployment().transport.handle_async_request(httpx.Request('GET', 'http://127.0.0.1/tools'))


@pytest.mark.asyncio
async def test_real_core_private_path(tmp_path, monkeypatch):
    from benchmark_support import system
    coordinator, task, adapter, supervisor, registry = await system(
        tmp_path, monkeypatch, rounds=1, reflect=False, private=True)
    try:
        await coordinator.start_session(task.task_id, 'grid.run', {})
        await supervisor.monitors[task.task_id]
        assert coordinator.benchmark.store.get('run', task.task_id)['settled']
        children = coordinator.benchmark.store.list('subtask', task.task_id)
        assert all(child['settled'] and child['outcome'] == 'failure' for child in children)
        assert all(child['history'] for child in children)
    finally:
        await supervisor.shutdown()
        await registry.current().client.close()
