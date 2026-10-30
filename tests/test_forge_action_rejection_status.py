from __future__ import annotations

from typing import Any

import pytest

from PhyAgentOS.config.schema import ForgeConfig
from PhyAgentOS.forge.task import AgentTaskCoordinator, AgentTaskError
from PhyAgentOS.forge.tool_client import ForgeToolAPIError, ForgeToolAPITimeoutError
from PhyAgentOS.verification.contracts import (
    CriterionVerdict,
    TaskVerificationContract,
    VerificationAttempt,
    VerificationVerdict,
)


class FlakyActionClient:
    """Raises the configured error on the first invoke_action, then succeeds."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.raised = False

    async def invoke_action(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        if not self.raised:
            self.raised = True
            raise self.error
        return {
            "ok": True,
            "data": {
                "tool_id": tool_id,
                "invocation_id": "inv_recovered",
                "attempt_id": "attempt_recovered",
                "arguments": arguments,
            },
        }


class AcceptingVerifier:
    """Returns a satisfied verdict; audit mode only looks at execution facts."""

    async def verify_agent_task(self, task: Any, **_: Any):
        criterion = task.verification.success_criteria[0]
        return (
            VerificationVerdict(
                verdict="success",
                criteria=[CriterionVerdict(criterion=criterion, status="satisfied")],
                reason="audit verification accepted",
            ),
            None,
            VerificationAttempt(attempt_id="verify_1"),
        )


def _coordinator(
    tmp_path, client: FlakyActionClient, *, verifier: Any | None = None
) -> AgentTaskCoordinator:
    return AgentTaskCoordinator(
        workspace=tmp_path,
        config=ForgeConfig(),
        client=client,  # type: ignore[arg-type]
        verifier=verifier,
    )


def _new_task(coordinator: AgentTaskCoordinator) -> str:
    task = coordinator.create_task(
        task_description="move the arm",
        verification=TaskVerificationContract(mode="off"),
    )
    return task.task_id


async def test_pre_dispatch_rejection_records_failed_and_allows_identical_resend(
    tmp_path,
) -> None:
    client = FlakyActionClient(
        ForgeToolAPIError(
            "operation concurrency 1 is exhausted",
            status_code=503,
            error_code="FORGE_TOOL_GATEWAY_BUSY",
            retryable=True,
        )
    )
    coordinator = _coordinator(tmp_path, client)
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    records = coordinator.store.get(task_id).execution_records
    assert [record.status for record in records] == ["failed"]

    # The gateway never dispatched anything, so the identical resend must
    # not be blocked by the unknown-remote-state guard.
    response = await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    assert response["data"]["invocation_id"] == "inv_recovered"
    records = coordinator.store.get(task_id).execution_records
    assert [record.status for record in records] == ["failed", "accepted"]


async def test_action_admission_rejection_records_failed_and_allows_resend(
    tmp_path,
) -> None:
    # Gateway 1.1.2+ reports admission refusal separately from busy; both mean
    # the invoke never reached the provider.
    client = FlakyActionClient(
        ForgeToolAPIError(
            "endpoint 'motion.server' has no active provider instance",
            status_code=503,
            error_code="FORGE_TOOL_ACTION_ADMISSION_REJECTED",
            retryable=True,
        )
    )
    coordinator = _coordinator(tmp_path, client)
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    records = coordinator.store.get(task_id).execution_records
    assert [record.status for record in records] == ["failed"]
    assert records[0].rejected_before_dispatch is True

    response = await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    assert response["data"]["invocation_id"] == "inv_recovered"


async def test_http_timeout_stays_unknown_and_arms_identical_guard(tmp_path) -> None:
    client = FlakyActionClient(ForgeToolAPITimeoutError("exchange timed out"))
    coordinator = _coordinator(tmp_path, client)
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPITimeoutError):
        await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    records = coordinator.store.get(task_id).execution_records
    assert [record.status for record in records] == ["unknown"]

    with pytest.raises(AgentTaskError, match="identical Action"):
        await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})


async def test_invoke_timeout_error_code_is_not_treated_as_pre_dispatch(
    tmp_path,
) -> None:
    # FORGE_TOOL_INVOKE_TIMEOUT is also emitted after dispatch when the
    # synchronous caller response times out, so a structured code alone must
    # not downgrade the record to failed.
    client = FlakyActionClient(
        ForgeToolAPIError(
            "Action deadline elapsed",
            status_code=504,
            error_code="FORGE_TOOL_INVOKE_TIMEOUT",
            retryable=False,
        )
    )
    coordinator = _coordinator(tmp_path, client)
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    records = coordinator.store.get(task_id).execution_records
    assert [record.status for record in records] == ["unknown"]


def _rejection() -> ForgeToolAPIError:
    return ForgeToolAPIError(
        "operation concurrency 1 is exhausted",
        status_code=503,
        error_code="FORGE_TOOL_GATEWAY_BUSY",
        retryable=True,
    )


async def test_finalization_succeeds_after_a_rejected_attempt_is_retried(
    tmp_path,
) -> None:
    coordinator = _coordinator(tmp_path, FlakyActionClient(_rejection()))
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    response = await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    coordinator.observe_action(
        task_id,
        response["data"]["invocation_id"],
        {"phase": "completed"},
    )

    finalized = await coordinator.finalize_task(task_id)

    assert finalized.status.value == "succeeded"
    assert [record.status for record in finalized.execution_records] == [
        "failed",
        "succeeded",
    ]
    assert [record.rejected_before_dispatch for record in finalized.execution_records] == [
        True,
        False,
    ]


async def test_finalization_fails_when_every_attempt_was_rejected_before_dispatch(
    tmp_path,
) -> None:
    coordinator = _coordinator(tmp_path, FlakyActionClient(_rejection()))
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})

    finalized = await coordinator.finalize_task(task_id)

    # A rejected call never executed, so it can neither satisfy nor poison the
    # task; with no actual execution the task did not succeed.
    assert finalized.status.value == "failed"
    assert [record.rejected_before_dispatch for record in finalized.execution_records] == [True]


async def test_finalization_fails_when_a_dispatched_attempt_failed(
    tmp_path,
) -> None:
    coordinator = _coordinator(tmp_path, FlakyActionClient(_rejection()))
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    response = await coordinator.start_action(task_id, "motion.move_pose", {"distance_m": 0.05})
    coordinator.observe_action(
        task_id,
        response["data"]["invocation_id"],
        {"phase": "failed"},
    )

    finalized = await coordinator.finalize_task(task_id)

    assert finalized.status.value == "failed"


async def test_audit_finalization_ignores_a_rejected_attempt(tmp_path) -> None:
    coordinator = _coordinator(
        tmp_path, FlakyActionClient(_rejection()), verifier=AcceptingVerifier()
    )
    task = coordinator.create_task(
        task_description="move the arm",
        verification=TaskVerificationContract(
            mode="audit",
            goal="move the arm to the target",
            success_criteria=["the arm reached the target"],
        ),
    )

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(
            task.task_id, "motion.move_pose", {"distance_m": 0.05}
        )
    response = await coordinator.start_action(
        task.task_id, "motion.move_pose", {"distance_m": 0.05}
    )
    coordinator.observe_action(
        task.task_id,
        response["data"]["invocation_id"],
        {"phase": "completed"},
    )

    finalized = await coordinator.finalize_task(task.task_id)

    assert finalized.status.value == "succeeded"


class CountingActionClient:
    """Count every dispatch attempt; never succeed."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def invoke_action(
        self,
        tool_id: str,
        arguments: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        del tool_id, arguments
        self.calls += 1
        raise self.error


def _request_validation_rejection(**kwargs: Any) -> ForgeToolAPIError:
    return ForgeToolAPIError(
        "timeout_ms must not exceed configured tools.invoke_timeout_ms maximum 60000",
        status_code=400,
        error_code="FORGE_TOOL_REQUEST_VALIDATION_REJECTED",
        retryable=False,
        **kwargs,
    )


async def test_typed_request_validation_rejection_fails_without_resend_or_recreation(
    tmp_path,
) -> None:
    client = CountingActionClient(_request_validation_rejection())
    coordinator = _coordinator(tmp_path, client)
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(
            task_id, "motion.move_pose", {"distance_m": 0.05}
        )
    records = coordinator.store.get(task_id).execution_records
    assert [record.status for record in records] == ["failed"]
    assert records[0].rejected_before_dispatch is True
    assert records[0].invocation_id is None
    assert records[0].attempt_id is None
    assert records[0].error is not None
    assert records[0].error["status_code"] == 400
    assert records[0].error["error_code"] == "FORGE_TOOL_REQUEST_VALIDATION_REJECTED"
    assert records[0].error["retryable"] is False

    # The rejection is not retried automatically.
    assert client.calls == 1

    finalized = await coordinator.finalize_task(task_id)
    assert finalized.status.value == "failed"

    # A cleanly rejected task no longer blocks the next task.
    created = coordinator.create_task(
        task_description="next attempt",
        verification=TaskVerificationContract(mode="off"),
    )
    assert created.status.value == "executing"
    assert client.calls == 1


async def test_typed_request_validation_with_remote_identity_stays_unknown(
    tmp_path,
) -> None:
    client = CountingActionClient(
        _request_validation_rejection(
            payload={
                "ok": False,
                "error": {
                    "code": "FORGE_TOOL_REQUEST_VALIDATION_REJECTED",
                    "message": "rejected",
                },
                "data": {"invocation_id": "inv_present", "attempt_id": "attempt_present"},
            }
        )
    )
    coordinator = _coordinator(tmp_path, client)
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(
            task_id, "motion.move_pose", {"distance_m": 0.05}
        )
    record = coordinator.store.get(task_id).execution_records[0]
    assert record.status == "unknown"
    assert record.rejected_before_dispatch is False
    assert record.invocation_id == "inv_present"
    assert record.attempt_id == "attempt_present"
    assert client.calls == 1


async def test_bare_400_without_code_stays_unknown(tmp_path) -> None:
    client = CountingActionClient(
        ForgeToolAPIError(
            "Forge Gateway Tool API /tools/motion.move_pose:invoke rejected request: "
            "timeout_ms must not exceed configured tools.invoke_timeout_ms maximum 60000",
            status_code=400,
        )
    )
    coordinator = _coordinator(tmp_path, client)
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(
            task_id, "motion.move_pose", {"distance_m": 0.05}
        )
    record = coordinator.store.get(task_id).execution_records[0]
    assert record.status == "unknown"
    assert record.rejected_before_dispatch is False
    assert record.error is not None
    assert record.error["status_code"] == 400
    assert "error_code" not in record.error

    with pytest.raises(AgentTaskError, match="identical Action"):
        await coordinator.start_action(
            task_id, "motion.move_pose", {"distance_m": 0.05}
        )
    assert client.calls == 1


async def test_validation_code_on_non_400_stays_unknown(tmp_path) -> None:
    client = CountingActionClient(
        ForgeToolAPIError(
            "typed code on an unexpected status",
            status_code=503,
            error_code="FORGE_TOOL_REQUEST_VALIDATION_REJECTED",
            retryable=False,
        )
    )
    coordinator = _coordinator(tmp_path, client)
    task_id = _new_task(coordinator)

    with pytest.raises(ForgeToolAPIError):
        await coordinator.start_action(
            task_id, "motion.move_pose", {"distance_m": 0.05}
        )
    record = coordinator.store.get(task_id).execution_records[0]
    assert record.status == "unknown"
    assert record.rejected_before_dispatch is False
    assert client.calls == 1
