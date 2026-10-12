"""Injected artifact map: no model-supplied filesystem or network access."""

import base64
import hashlib

from PhyAgentOS.forge.interaction.contracts import digest


class ObservationArtifacts:
    def __init__(self):
        self._artifacts = {}

    def register(self, scope, observation, artifact_id, content, media_type="image/png"):
        if (
            media_type not in {"image/png", "image/jpeg", "image/webp"}
            or len(content) > 8 * 1024 * 1024
        ):
            raise ValueError("unsupported observation artifact")
        sha = hashlib.sha256(content).hexdigest()
        key = (digest(scope), digest(observation), artifact_id, sha)
        self._artifacts[key] = (bytes(content), media_type)
        return {"artifact_id": artifact_id, "sha256": sha}

    def resolve(self, scope, snapshot, reference):
        if set(reference) != {"artifact_id", "sha256"}:
            raise ValueError("artifact references cannot contain paths or URLs")
        observation = {k: v for k, v in snapshot["observation"].items() if k != "artifacts"}
        key = (digest(scope), digest(observation), reference["artifact_id"], reference["sha256"])
        if key not in self._artifacts:
            raise ValueError("artifact does not belong to this episode/observation")
        content, media_type = self._artifacts[key]
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64," + base64.b64encode(content).decode()},
        }
