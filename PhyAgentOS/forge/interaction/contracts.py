"""Strict business contracts, separate from Forge's invocation envelopes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator

PROTOCOL = "interaction_contract_v1"
ID = {
    "type": "string",
    "minLength": 1,
    "maxLength": 160,
    "pattern": r"^[A-Za-z0-9_.:-]+$(?![\s\S])",
}
COUNT = {"type": "integer", "minimum": 1, "maximum": 10000}


def object_schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


COMMON = {"protocol": {"const": PROTOCOL}, "task_id": ID, "revision_id": ID}
START_SCHEMA = object_schema(
    {
        **COMMON,
        "start_id": ID,
        "interaction_run_id": ID,
        "world_id": ID,
        "binding_digest": ID,
        "runtime_instance_id": ID,
        "gateway_identity": ID,
        "budget": object_schema(
            {
                "decisions": COUNT,
                "actions": COUNT,
                "wall_time_s": {"type": "number", "minimum": 1, "maximum": 86400},
                "idle_time_s": {"type": "number", "minimum": 1, "maximum": 3600},
            }
        ),
    }
)
QUERY_SCHEMA = object_schema(
    {
        **COMMON,
        "view": {"enum": ["snapshot", "submission", "history", "run"]},
        "interaction_run_id": ID,
        "start_id": ID,
        "session_invocation_id": ID,
        "submission_id": ID,
        "after_executor_cursor": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    [*COMMON, "view"],
)


def submission_schema(proposal_schema: dict) -> dict:
    return object_schema(
        {
            **COMMON,
            "interaction_run_id": ID,
            "session_invocation_id": ID,
            "step_id": ID,
            "submission_id": ID,
            "runner_epoch": {"type": "integer", "minimum": 1},
            "expected_control_version": {"type": "integer", "minimum": 1},
            "based_on_observation": ID,
            "world_epoch": ID,
            "proposal": proposal_schema,
        }
    )


class ContractError(ValueError):
    def __init__(self, code: str, details: Any = None):
        super().__init__(code)
        self.code = code
        self.details = details


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def validate(value: Any, schema: dict) -> None:
    try:
        encoded = canonical_json(value)
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid_json") from exc
    if len(encoded.encode()) > 65536:
        raise ContractError("payload_too_large")
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        # Do not echo input values: they can include control credentials.
        raise ContractError("schema_invalid", [list(e.path) for e in errors[:5]])


def validate_query(value: dict) -> None:
    validate(value, QUERY_SCHEMA)
    view = value["view"]
    if view == "run":
        if ("interaction_run_id" in value) == ("start_id" in value):
            raise ContractError("run_reference_required")
    elif "interaction_run_id" not in value or "start_id" in value:
        raise ContractError("run_reference_required")
    if (view == "submission") != ("submission_id" in value):
        raise ContractError("invalid_submission_view")
    if view != "history" and ({"limit", "after_executor_cursor"} & value.keys()):
        raise ContractError("invalid_history_view")


def validate_receipt(receipt: dict) -> None:
    validate(
        receipt,
        object_schema(
            {
                "accepted": {"type": "boolean"},
                "interaction_run_id": ID,
                "submission_id": ID,
                "step_id": ID,
                "control_version": {"type": "integer", "minimum": 1},
                "execution_status": {"enum": ["pending", None]},
                "reason": {"type": ["string", "null"]},
                "retryable": {"type": "boolean"},
                "payload_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "execution_id": ID,
                "transport_invocation_id": ID,
            },
            [
                "accepted",
                "interaction_run_id",
                "submission_id",
                "step_id",
                "control_version",
                "execution_status",
                "reason",
                "retryable",
                "payload_digest",
            ],
        ),
    )
    if receipt["accepted"] and (
        receipt["execution_status"] != "pending" or not receipt.get("execution_id")
    ):
        raise ContractError("invalid_acceptance_receipt")
    if not receipt["accepted"] and receipt["execution_status"] is not None:
        raise ContractError("invalid_rejection_receipt")
