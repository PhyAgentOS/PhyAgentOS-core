"""Child usage belongs to one exact parent turn, including after settlement."""

from PhyAgentOS.agent.services.turn_budget import TurnBudget, TurnBudgetRegistry


def test_child_usage_is_attributed_to_originating_parent_not_latest_turn():
    registry = TurnBudgetRegistry()
    config = TurnBudget(max_tokens=20, registry=registry)
    first, second = config.begin_turn(), config.begin_turn()
    registry.register("parent", "first", first)
    registry.register("parent", "second", second)
    registry.observe("parent", "first", {"total_tokens": 20})
    assert not registry.permits("parent", "first")
    assert registry.permits("parent", "second")
    assert second.spent_tokens == 0
    registry.observe("parent", "second", {})
    assert second.unknown_usage_calls == 1


def test_evicted_parent_fails_closed_and_retention_is_bounded():
    registry = TurnBudgetRegistry(max_turns=1)
    registry.register("a", "a1", TurnBudget().begin_turn())
    registry.register("b", "b1", TurnBudget().begin_turn())
    assert not registry.permits("a", "a1")
    assert registry.permits("b", "b1")
    assert not registry.permits("another-session", "b1")
