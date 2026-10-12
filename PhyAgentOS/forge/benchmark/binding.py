"""Installer-owned integrity receipts for benchmark bundles."""

import hashlib
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from PhyAgentOS.forge.interaction.contracts import digest

from .contracts import PROTOCOL

RECEIPT = ".paos-benchmark-install.json"
KEYS = ("session_tool", "snapshot_tool", "submission_tool")


def files(root):
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("benchmark bundle cannot contain symlinks")
        if path.is_file() and path.name != RECEIPT:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def read_contract(root):
    path = root / "benchmark.yaml"
    if not path.exists():
        if (root / RECEIPT).exists():
            raise ValueError("benchmark sidecar disappeared")
        return None
    if (root / "interaction.yaml").exists():
        raise ValueError("a Skill cannot declare both interaction and benchmark")
    value = yaml.safe_load(path.read_text())
    if (
        not isinstance(value, dict)
        or set(value)
        != {"protocol", *KEYS, "proposal_schema_file", "snapshot_views", "access_policy"}
        or value["protocol"] != PROTOCOL
    ):
        raise ValueError("unsupported benchmark sidecar")
    if any(not isinstance(value[k], str) or not value[k] for k in KEYS):
        raise ValueError("missing benchmark tools")
    if len({value[k] for k in KEYS}) != 3:
        raise ValueError("benchmark requires three distinct operations")
    if value["snapshot_views"] != ["root", "episode", "receipt", "history"]:
        raise ValueError("benchmark recovery views are required")
    if value["access_policy"] != "private_control_plane":
        raise ValueError("benchmark requires a private control plane")
    relative = Path(value["proposal_schema_file"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("unsafe proposal schema path")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("schema escapes bundle")
    schema = json.loads(target.read_text())
    Draft202012Validator.check_schema(schema)

    def strict(node):
        if isinstance(node, dict):
            if "$ref" in node or "$dynamicRef" in node:
                raise ValueError("proposal schema must be self-contained")
            if node.get("type") == "object" and node.get("additionalProperties") is not False:
                raise ValueError("proposal schema must reject unknown properties")
            for child in node.values():
                strict(child)
        elif isinstance(node, list):
            for child in node:
                strict(child)

    strict(schema)
    if schema.get("type") != "object" or not schema.get("properties"):
        raise ValueError("proposal schema must define an operation object")
    return {"contract": value, "proposal_schema": schema}


def install_receipt(manifest, archive_sha, *, verified):
    root = manifest.bundle_root
    if (root / RECEIPT).exists():
        raise ValueError("archive cannot supply installer receipt")
    extension = read_contract(root)
    if extension is None:
        return
    if not verified:
        raise ValueError("benchmark requires archive verification")
    required = {extension["contract"][key] for key in KEYS}
    if not any(required <= set(manifest.tools_for_profile(p)) for p in manifest.profiles):
        raise ValueError("no profile declares all benchmark tools")
    (root / RECEIPT).write_text(
        json.dumps(
            {"version": 1, "archive_sha256": archive_sha, "files": files(root)}, sort_keys=True
        )
    )


def verified_extension(manifest):
    extension = read_contract(manifest.bundle_root)
    if extension is None:
        return None
    receipt = json.loads((manifest.bundle_root / RECEIPT).read_text())
    if receipt.get("version") != 1 or receipt.get("files") != files(manifest.bundle_root):
        raise ValueError("benchmark bundle changed after installation")
    return {
        **extension,
        "bundle_digest": digest(receipt),
        "root": str(manifest.bundle_root.resolve()),
    }
