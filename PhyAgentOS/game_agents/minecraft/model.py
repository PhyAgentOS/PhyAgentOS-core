"""Small, stable data model for Skill Graph v1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

NODE_TYPES = {"Entity", "StateCondition", "SkillAction", "Goal", "FailurePattern", "Remedy"}
EDGE_TYPES = {
    "TRANSITION",
    "HARNESS_REJECTION",
    "REQUIRES",
    "REMEDY_FOR",
    "COMPOSES",
    "PREFER_OVER",
    "REFINES",
    "CONTRADICTS",
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def canonical_hash(value: Any, prefix: str = "sha256") -> str:
    digest = hashlib.sha256(canonical_json(value).encode()).hexdigest()
    return f"{prefix}:{digest}"


@dataclass(frozen=True)
class RuntimeFingerprint:
    profile: Mapping[str, Any]

    @property
    def hash(self) -> str:
        return canonical_hash(dict(self.profile), "runtime")

    def to_dict(self) -> dict[str, Any]:
        return {"hash": self.hash, "profile": dict(self.profile)}


@dataclass(frozen=True)
class Node:
    node_type: str
    key: str
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        if self.node_type not in NODE_TYPES:
            raise ValueError(f"invalid node type: {self.node_type}")
        return canonical_hash(
            {"node_type": self.node_type, "key": self.key, "attributes": dict(self.attributes)},
            "node",
        )


@dataclass(frozen=True)
class Evidence:
    episode_id: str
    trial_id: str
    task_id: str
    source: str
    outcome: str
    payload: Mapping[str, Any]
    runtime_fingerprint: str
    confounded: bool = False

    @property
    def id(self) -> str:
        return canonical_hash(
            {
                "episode_id": self.episode_id,
                "trial_id": self.trial_id,
                "task_id": self.task_id,
                "source": self.source,
                "outcome": self.outcome,
                "payload": dict(self.payload),
                "runtime_fingerprint": self.runtime_fingerprint,
                "confounded": self.confounded,
            },
            "evidence",
        )


@dataclass(frozen=True)
class Claim:
    edge_type: str
    subject: Node
    object: Node
    action: Mapping[str, Any]
    preconditions: Mapping[str, Any]
    effects: Mapping[str, Any]
    scope: Mapping[str, Any]
    outcome: str
    evidence_class: str = "benchmark_episode"
    serveable: bool = True

    @property
    def id(self) -> str:
        if self.edge_type not in EDGE_TYPES:
            raise ValueError(f"invalid edge type: {self.edge_type}")
        return canonical_hash(
            {
                "edge_type": self.edge_type,
                "subject": self.subject.id,
                "object": self.object.id,
                "action": dict(self.action),
                "preconditions": dict(self.preconditions),
                "effects": dict(self.effects),
                "scope": dict(self.scope),
                "outcome": self.outcome,
                "evidence_class": self.evidence_class,
                "serveable": self.serveable,
            },
            "claim",
        )
