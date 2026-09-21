"""SQLite evidence ledger, claim registry, retrieval, and graph snapshots."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from PhyAgentOS.benchmarks.minecraft.techtree.evaluator import inventory_counts
from PhyAgentOS.benchmarks.minecraft.techtree.schema import TechTreeTask

from .model import Claim, Evidence, Node, RuntimeFingerprint, canonical_hash, canonical_json

SCHEMA_VERSION = 1


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _action_sequence(agent_result: Any) -> list[Mapping[str, Any]]:
    if not isinstance(agent_result, Mapping):
        return []
    actions = agent_result.get("actions")
    if isinstance(actions, list):
        return [dict(item) for item in actions if isinstance(item, Mapping)]
    results = agent_result.get("results")
    if isinstance(results, list):
        return [
            dict(item["action"])
            for item in results
            if isinstance(item, Mapping) and isinstance(item.get("action"), Mapping)
        ]
    return []


class GraphStore:
    """One durable transaction per observed episode."""

    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path).resolve()
        self.readonly = readonly
        if readonly:
            self.conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro&immutable=1", uri=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
            self.conn.executescript(
                """
                PRAGMA journal_mode=DELETE;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS metadata (
                  key TEXT PRIMARY KEY, value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nodes (
                  node_id TEXT PRIMARY KEY, node_type TEXT NOT NULL,
                  canonical_key TEXT NOT NULL, attributes_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                  evidence_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL,
                  trial_id TEXT NOT NULL, task_id TEXT NOT NULL, source TEXT NOT NULL,
                  outcome TEXT NOT NULL, payload_json TEXT NOT NULL,
                  runtime_fingerprint TEXT NOT NULL, confounded INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                  claim_id TEXT PRIMARY KEY, edge_type TEXT NOT NULL,
                  subject_node_id TEXT NOT NULL, object_node_id TEXT NOT NULL,
                  action_json TEXT NOT NULL, preconditions_json TEXT NOT NULL,
                  effects_json TEXT NOT NULL, scope_json TEXT NOT NULL,
                  outcome TEXT NOT NULL, evidence_class TEXT NOT NULL,
                  serveable INTEGER NOT NULL, status TEXT NOT NULL,
                  confidence REAL NOT NULL, support_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_evidence (
                  claim_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
                  PRIMARY KEY (claim_id, evidence_id)
                );
                CREATE INDEX IF NOT EXISTS idx_claim_status ON claims(status);
                CREATE INDEX IF NOT EXISTS idx_evidence_episode ON evidence(episode_id);
                """
            )
            self.set_metadata("schema_version", SCHEMA_VERSION)
            self.conn.commit()
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if not self.readonly:
            self.conn.commit()
        self.conn.close()

    def set_metadata(self, key: str, value: Any) -> None:
        if self.readonly:
            raise RuntimeError("frozen graph is read-only")
        self.conn.execute(
            "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, canonical_json(value)),
        )

    def get_metadata(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value_json FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _node(self, node: Node) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO nodes VALUES(?,?,?,?)",
            (node.id, node.node_type, node.key, canonical_json(dict(node.attributes))),
        )

    def add(self, evidence: Evidence, claim: Claim) -> bool:
        if self.readonly:
            raise RuntimeError("frozen graph is read-only")
        before = self.conn.total_changes
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    evidence.id,
                    evidence.episode_id,
                    evidence.trial_id,
                    evidence.task_id,
                    evidence.source,
                    evidence.outcome,
                    canonical_json(dict(evidence.payload)),
                    evidence.runtime_fingerprint,
                    int(evidence.confounded),
                ),
            )
            self._node(claim.subject)
            self._node(claim.object)
            self.conn.execute(
                "INSERT OR IGNORE INTO claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    claim.id,
                    claim.edge_type,
                    claim.subject.id,
                    claim.object.id,
                    canonical_json(dict(claim.action)),
                    canonical_json(dict(claim.preconditions)),
                    canonical_json(dict(claim.effects)),
                    canonical_json(dict(claim.scope)),
                    claim.outcome,
                    claim.evidence_class,
                    int(claim.serveable),
                    "candidate",
                    0.5,
                    "{}",
                ),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO claim_evidence VALUES(?,?)", (claim.id, evidence.id)
            )
            self._refresh(claim.id)
        return self.conn.total_changes > before

    def _refresh(self, claim_id: str) -> None:
        rows = self.conn.execute(
            """SELECT DISTINCT e.trial_id,e.episode_id,e.confounded
               FROM claim_evidence ce JOIN evidence e USING(evidence_id)
               WHERE ce.claim_id=?""",
            (claim_id,),
        ).fetchall()
        usable = [row for row in rows if not bool(row["confounded"])]
        trials = sorted({row["trial_id"] for row in usable})
        episodes = sorted({row["episode_id"] for row in usable})
        confidence = 1.0 if trials else 0.0
        status = "verified" if trials else "quarantined"
        support = {
            "observed_trials": trials,
            "episodes": episodes,
            "observation_count": len(trials),
            "verification_policy": "single_observation",
            "backend_seed_control": False,
        }
        self.conn.execute(
            "UPDATE claims SET status=?,confidence=?,support_json=? WHERE claim_id=?",
            (status, confidence, canonical_json(support), claim_id),
        )

    def record_episode(
        self,
        task: TechTreeTask,
        result: Any,
        *,
        trial_id: str,
        source: str,
        runtime: RuntimeFingerprint,
    ) -> bool:
        """Synchronously persist one warm-up or benchmark experience."""

        sequence = _action_sequence(result.agent_result)
        signature = canonical_hash(sequence or [{"task": task.id}], "action")
        success = bool(result.success)
        failure = result.verdict.reason if not success else None
        initial = inventory_counts(result.initial_observation or {})
        final = inventory_counts(result.final_observation or {})
        payload = {
            "task": task.to_dict(),
            "success": success,
            "reward": result.reward,
            "error": result.error,
            "verdict": result.verdict.to_dict(),
            "initial_inventory": initial,
            "final_inventory": final,
            "actions": sequence,
            "backend_seed_control": False,
        }
        evidence = Evidence(
            episode_id=f"{source}:{task.id}:{trial_id}",
            trial_id=trial_id,
            task_id=task.id,
            source=source,
            outcome="success" if success else f"failure:{failure}",
            payload=payload,
            runtime_fingerprint=runtime.hash,
            confounded=bool(result.error),
        )
        subject = Node("SkillAction", signature, {"actions": sequence, "task_family": task.family})
        if success:
            object_node = Node(
                "Goal", f"inventory:{task.target_item}>={task.success_criterion.count}"
            )
            effects: Mapping[str, Any] = {
                "inventory_contains": {task.target_item: task.success_criterion.count}
            }
            outcome = "state_transition_success"
        else:
            object_node = Node("FailurePattern", str(failure or "unknown_failure"))
            effects = {"failure_pattern": str(failure or "unknown_failure")}
            outcome = "observed_failure"
        claim = Claim(
            edge_type="TRANSITION",
            subject=subject,
            object=object_node,
            action={"signature": signature, "sequence": sequence},
            preconditions={"inventory": initial, "family": task.family},
            effects=effects,
            scope={"runtime_fingerprint": runtime.hash, "backend": "mineflayer_http"},
            outcome=outcome,
            evidence_class=f"{source}_episode",
            serveable=not bool(result.error),
        )
        return self.add(evidence, claim)

    def counts(self) -> dict[str, Any]:
        statuses = dict(self.conn.execute("SELECT status,COUNT(*) FROM claims GROUP BY status"))
        return {
            "evidence": self.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "nodes": self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "claims": self.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
            "claim_statuses": statuses,
        }

    def claims(self, *, status: str | None = None) -> list[dict[str, Any]]:
        where = " WHERE c.status=?" if status else ""
        args = (status,) if status else ()
        query = (
            "SELECT c.*,s.node_type s_type,s.canonical_key s_key,s.attributes_json s_attrs,"
            "o.node_type o_type,o.canonical_key o_key,o.attributes_json o_attrs "
            "FROM claims c JOIN nodes s ON s.node_id=c.subject_node_id "
            "JOIN nodes o ON o.node_id=c.object_node_id" + where + " ORDER BY c.claim_id"
        )
        output = []
        for row in self.conn.execute(query, args):
            output.append(
                {
                    "claim_id": row["claim_id"],
                    "edge_type": row["edge_type"],
                    "subject": {
                        "node_id": row["subject_node_id"],
                        "node_type": row["s_type"],
                        "key": row["s_key"],
                        "attributes": json.loads(row["s_attrs"]),
                    },
                    "object": {
                        "node_id": row["object_node_id"],
                        "node_type": row["o_type"],
                        "key": row["o_key"],
                        "attributes": json.loads(row["o_attrs"]),
                    },
                    "action": json.loads(row["action_json"]),
                    "preconditions": json.loads(row["preconditions_json"]),
                    "effects": json.loads(row["effects_json"]),
                    "scope": json.loads(row["scope_json"]),
                    "outcome": row["outcome"],
                    "evidence_class": row["evidence_class"],
                    "serveable": bool(row["serveable"]),
                    "status": row["status"],
                    "confidence": float(row["confidence"]),
                    "support": json.loads(row["support_json"]),
                }
            )
        return output

    def serving_graph(self, runtime: RuntimeFingerprint) -> dict[str, Any]:
        claims = [
            claim
            for claim in self.claims(status="verified")
            if claim["serveable"] and claim["scope"].get("runtime_fingerprint") == runtime.hash
        ]
        nodes = {}
        for claim in claims:
            nodes[claim["subject"]["node_id"]] = claim["subject"]
            nodes[claim["object"]["node_id"]] = claim["object"]
        return {
            "schema_version": SCHEMA_VERSION,
            "runtime_fingerprint": runtime.hash,
            "nodes": [nodes[key] for key in sorted(nodes)],
            "claims": claims,
        }

    def retrieve(
        self,
        goal: str,
        observation: Mapping[str, Any],
        *,
        limit: int = 8,
        max_chars: int = 3000,
    ) -> tuple[str, list[str]]:
        """Return compact verified context; live observations remain authoritative."""

        normalized_goal = goal.lower().replace(".", " ").replace("_", " ")
        terms = {term for term in normalized_goal.split() if len(term) > 2}
        inventory = set(inventory_counts(observation))
        ranked = []
        for claim in self.claims(status="verified"):
            if not claim["serveable"]:
                continue
            haystack = canonical_json(
                {
                    "action": claim["action"],
                    "object": claim["object"]["key"],
                    "effects": claim["effects"],
                }
            ).lower()
            score = 5 * sum(term in haystack for term in terms)
            score += sum(item in haystack for item in inventory)
            ranked.append((-score, claim["claim_id"], claim))
        lines: list[str] = []
        ids: list[str] = []
        for _, claim_id, claim in sorted(ranked):
            line = (
                f"{claim['action']['signature']} with {claim['preconditions']} -> "
                f"{claim['effects']} (confidence={claim['confidence']:.2f})"
            )
            if sum(map(len, lines)) + len(line) > max_chars:
                continue
            lines.append(f"{len(lines) + 1}. {line}")
            ids.append(claim_id)
            if len(lines) == limit:
                break
        if not lines:
            return "", []
        header = "VERIFIED SKILL GRAPH (observations override learned claims):\n"
        return header + "\n".join(lines), ids

    def evidence_jsonl(self) -> str:
        lines = []
        for row in self.conn.execute("SELECT * FROM evidence ORDER BY evidence_id"):
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            value["confounded"] = bool(value["confounded"])
            lines.append(canonical_json(value))
        return "".join(f"{line}\n" for line in lines)


def _artifact_payload(
    store: GraphStore,
    runtime: RuntimeFingerprint,
    extra: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    serving = store.serving_graph(runtime)
    graph_hash = canonical_hash(serving, "graph")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "graph_hash": graph_hash,
        "runtime": runtime.to_dict(),
        "counts": store.counts(),
        "backend_seed_control": False,
        **dict(extra or {}),
    }
    return {**serving, "graph_hash": graph_hash}, manifest


def sync_mutable_graph(
    store: GraphStore,
    artifact_dir: str | Path,
    runtime: RuntimeFingerprint,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if store.readonly:
        raise RuntimeError("cannot sync a frozen graph")
    artifact = Path(artifact_dir).resolve()
    serving, manifest = _artifact_payload(store, runtime, extra)
    _atomic_text(
        artifact / "serving_graph.json",
        json.dumps(serving, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    _atomic_text(artifact / "evidence.jsonl", store.evidence_jsonl())
    _atomic_text(
        artifact / "graph_manifest.json",
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    _atomic_text(artifact / "graph.sha256", manifest["graph_hash"] + "\n")
    return manifest


def freeze_graph(
    store: GraphStore,
    artifact_dir: str | Path,
    runtime: RuntimeFingerprint,
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    artifact = Path(artifact_dir).resolve()
    if store.path.parent != artifact:
        raise ValueError("graph.sqlite must be inside the artifact directory")
    manifest = sync_mutable_graph(store, artifact, runtime, extra=extra)
    store.close()
    for path in artifact.iterdir():
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    artifact.chmod(
        stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )
    return manifest


def load_frozen_graph(
    artifact_dir: str | Path,
    runtime: RuntimeFingerprint,
    *,
    expected_hash: str | None = None,
) -> tuple[GraphStore, dict[str, Any]]:
    artifact = Path(artifact_dir).resolve()
    manifest = json.loads((artifact / "graph_manifest.json").read_text(encoding="utf-8"))
    serving = json.loads((artifact / "serving_graph.json").read_text(encoding="utf-8"))
    declared = serving.pop("graph_hash", None)
    actual = canonical_hash(serving, "graph")
    if declared != actual or manifest.get("graph_hash") != actual:
        raise RuntimeError("skill graph content hash mismatch")
    if expected_hash and expected_hash != actual:
        raise RuntimeError(f"skill graph hash mismatch: expected {expected_hash}, got {actual}")
    if manifest.get("runtime", {}).get("hash") != runtime.hash:
        raise RuntimeError("skill graph runtime scope mismatch")
    return GraphStore(artifact / "graph.sqlite", readonly=True), manifest


def clone_frozen_graph(source: str | Path, destination: str | Path) -> GraphStore:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if destination_path.exists():
        raise FileExistsError(destination_path)
    shutil.copytree(source_path, destination_path)
    destination_path.chmod(
        stat.S_IRUSR
        | stat.S_IWUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )
    for path in destination_path.iterdir():
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    store = GraphStore(destination_path / "graph.sqlite")
    store.set_metadata("base_frozen_graph_hash", (source_path / "graph.sha256").read_text().strip())
    store.set_metadata("artifact_role", "benchmark_mutable")
    store.conn.commit()
    return store
