"""Core execution settings validate limits without changing provider selection."""

import pytest
from pydantic import ValidationError

from PhyAgentOS.config.schema import Config, CorePerformanceConfig


def test_core_performance_defaults_preserve_model_selection():
    config = Config.model_validate({"agents": {"defaults": {"model": "owner-model"}}})
    assert config.agents.defaults.model == "owner-model"
    assert config.agents.defaults.performance.max_parallel_tools == 4
    assert config.agents.defaults.performance.max_turn_tokens is None


def test_core_performance_camel_case_configuration():
    config = CorePerformanceConfig.model_validate({
        "maxParallelTools": 2, "cacheStablePrompts": False,
        "compactionThreshold": 0.7, "maxTurnTokens": 1000,
    })
    assert config.max_parallel_tools == 2
    assert not config.cache_stable_prompts
    assert config.compaction_threshold == 0.7
    assert config.max_turn_tokens == 1000


@pytest.mark.parametrize("values", [
    {"max_parallel_tools": 0}, {"compaction_threshold": 1},
    {"compaction_threshold": 0}, {"max_turn_tokens": 0},
    {"repeated_call_limit": 1}, {"reserve_steps": 2},
])
def test_core_performance_rejects_invalid_limits(values):
    with pytest.raises(ValidationError):
        CorePerformanceConfig.model_validate(values)
