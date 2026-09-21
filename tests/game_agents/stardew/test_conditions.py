"""Verify configured completion conditions and strict numeric deadlines."""

import pytest

from PhyAgentOS.cli.general_game_commands import success_check


@pytest.mark.parametrize(
    "time, expected",
    [(1650, True), (1700, False), (1800, False), (None, False), ("1650", False), (False, False)],
)
def test_success_requires_completion_before_deadline(time, expected):
    verify = success_check({"game.score": 150, "game.time": {"$lt": 1700}})
    observation = {"game": {"score": 150, "time": time}}
    assert verify(observation, {}) is expected
    observation["game"]["score"] = 0
    assert not verify(observation, {})


@pytest.mark.parametrize(
    "condition", [{"$lt": "1700"}, {"$lt": True}, {"$lt": float("inf")}, {"$lt": 1700, "$gt": 0}]
)
def test_invalid_deadline_configuration_fails_before_starting_game(condition):
    with pytest.raises(ValueError, match="finite numeric"):
        success_check({"game.time": condition})
