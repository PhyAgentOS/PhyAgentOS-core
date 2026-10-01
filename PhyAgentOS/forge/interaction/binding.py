"""Verified optional sidecars. A declaration never grants execution authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from .contracts import PROTOCOL, digest

RECEIPT = ".paos-interaction-install.json"
FIELDS = {
    "protocol",
    "session_tool",
    "snapshot_tool",
    "submission_tool",
    "proposal_schema_file",
    "snapshot_views",
    "event_channel",
    "execution_policy",
    "access_policy",
}


def _files(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("interaction bundle cannot contain symlinks")
        if path.is_file() and path.name != RECEIPT:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def read_contract(root: Path) -> dict | None:
    path = root / "interaction.yaml"
    if not path.exists():
        if (root / RECEIPT).exists():
            raise ValueError("installed interaction sidecar disappeared")
        return None
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict) or set(value) != FIELDS or value["protocol"] != PROTOCOL:
        raise ValueError("unsupported interaction sidecar")
    for key in ("session_tool", "snapshot_tool", "submission_tool"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError("interaction tool reference is missing")
    if len({value[k] for k in ("session_tool", "snapshot_tool", "submission_tool")}) != 3:
        raise ValueError("interaction tools must be distinct")
    if value["snapshot_views"] != ["snapshot", "submission", "history", "run"]:
        raise ValueError("interaction recovery views are required")
    if (
        value["execution_policy"]
        != {
            "max_active_sessions_per_task": 1,
            "max_inflight_decisions_per_run": 1,
            "runner_mode": "single_owner_no_hot_takeover",
            "completion_policy": "quiesce_then_stop",
        }
        or value["access_policy"] != "private_control_plane"
    ):
        raise ValueError("unsupported interaction execution/access policy")
    if value["event_channel"] != {
        "mode": "polling",
        "event_type": "progress",
        "business_kind": "interaction_changed",
    }:
        raise ValueError("this implementation supports polling only")
    relative = Path(value["proposal_schema_file"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe proposal schema path")
    schema_path = (root / relative).resolve()
    if not schema_path.is_relative_to(root.resolve()):
        raise ValueError("proposal schema escapes bundle")
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)

    def strict(node):
        if isinstance(node, dict):
            if "$ref" in node or "$dynamicRef" in node:
                raise ValueError("proposal schema must be self-contained")
            if node.get("type") == "object" and node.get("additionalProperties") is not False:
                raise ValueError("proposal objects must reject unknown properties")
            for child in node.values():
                strict(child)
        elif isinstance(node, list):
            for child in node:
                strict(child)

    strict(schema)
    if schema.get("type") != "object" or not schema.get("properties"):
        raise ValueError("proposal schema must define a bounded operation object")
    return {"contract": value, "proposal_schema": schema}


def install_receipt(manifest, archive_sha: str, *, verified: bool) -> None:
    root = manifest.bundle_root
    if (root / RECEIPT).exists():
        raise ValueError("archive cannot supply installer receipt")
    extension = read_contract(root)
    if extension is None:
        return
    if not verified:
        raise ValueError("interaction installation requires archive manifest verification")
    contract = extension["contract"]
    if not {contract[k] for k in ("session_tool", "snapshot_tool", "submission_tool")} <= set(
        manifest.required_tools
    ):
        raise ValueError("interaction tools must be declared in required_tools")
    (root / RECEIPT).write_text(
        json.dumps(
            {"version": 1, "archive_sha256": archive_sha, "files": _files(root)}, sort_keys=True
        )
    )


def verified_extension(manifest) -> dict | None:
    extension = read_contract(manifest.bundle_root)
    if extension is None:
        return None
    try:
        receipt = json.loads((manifest.bundle_root / RECEIPT).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("interaction bundle needs a verified reinstall") from exc
    if receipt.get("version") != 1 or receipt.get("files") != _files(manifest.bundle_root):
        raise ValueError("interaction bundle changed after installation")
    return {
        **extension,
        "bundle_digest": digest(receipt),
        "root": str(manifest.bundle_root.resolve()),
    }


class InteractionBinding(BaseModel):
    """Stable opt-in contract association; never replaces the live Forge binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["interaction_binding_v1"] = "interaction_binding_v1"
    task_id: str
    binding_id: str
    extension: dict
    specs: dict[str, str]
    binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
