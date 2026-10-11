#!/usr/bin/env python3
"""Schema-robust wrapper around PAOS's model-backed experience analyzer.

``PhyAgentOS/agent/experience/analyzer.py`` validates the reflection model's JSON
with ``model_validate`` and never retries. Observed live failures on this bench:

* an off-schema enum value (``failure_observations[0].eligibility.decision ==
  "ineligible"``) raised ``ValidationError`` and the coordinator dropped the
  episode silently;
* an empty/malformed reflection response was coerced by a naive normalizer into
  ``lesson_abstraction_validation_v1{reusable: false}``, which made
  ``SkillEvolutionManager`` block a perfectly good lesson cluster.

This subclass keeps the upstream payloads and contracts intact and adds:

* unwrapping of single-key wrappers around the expected object,
* strict field checks: a response that is missing required fields is treated as
  a failed attempt and retried, never silently replaced by a fabricated default,
* deterministic normalisation of the closed vocabularies (outcome / eligibility
  decision + reason / severity / lists / confidence / booleans),
* up to ``max_attempts`` repair round-trips that feed the failure back to the model,
* cluster-target alignment for the Lesson draft (the manager requires the draft
  to target exactly the cluster's Skill and workflow).

It never edits PhyAgentOS sources; it only wraps the public analyzer protocol.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

try:
    import json_repair
except ModuleNotFoundError:  # pragma: no cover - base runtime installs this dependency
    json_repair = None

from PhyAgentOS.agent.experience.analyzer import (
    EVOLUTION_PROMPT,
    LESSON_SYNTHESIS_PROMPT,
    LESSON_VALIDATION_PROMPT,
    ModelExperienceAnalyzer,
)
from PhyAgentOS.agent.experience.contracts import (
    ExperienceAssessment,
    LessonAbstractionValidation,
    LessonProposal,
)

OUTCOMES = ("success", "failure", "mixed", "ignored")
DECISIONS = ("related", "unrelated", "uncertain")
REASONS = (
    "workflow_related",
    "task_unsatisfiable",
    "verifier_limit",
    "evidence_limit",
    "external_or_infrastructure",
    "user_constraint",
    "unknown",
)
PATTERN_FIELDS = ("workflow_key", "pattern_key", "pattern_summary", "recovery_principle")
OUTCOME_SYNONYMS = {
    "failed": "failure",
    "fail": "failure",
    "succeeded": "success",
    "ok": "success",
    "partial": "mixed",
    "partial_success": "mixed",
    "none": "ignored",
    "skipped": "ignored",
    "not_applicable": "ignored",
}

ENGLISH_STOPWORDS = frozenset({
    "the", "is", "a", "an", "and", "or", "of", "to", "in", "on", "for",
    "with", "at", "by", "from", "that", "this", "it", "its", "are", "was",
    "were", "be", "been", "has", "have", "had", "not", "no", "when",
    "which", "who", "if", "then", "than", "so", "as", "but", "do", "does",
    "did", "will", "would", "can", "could", "should", "may", "might",
    "must", "shall", "their", "they", "them", "these", "those",
})


class RetrievabilityError(ValueError):
    """Raised when a synthesized lesson cannot be retrieved by PAOS scorer."""


class RetrievalScorer:
    """Replicates SkillActivationManager._terms and relevant_lessons scoring."""

    ASCII_TERM = re.compile(r"[A-Za-z0-9_\-]+")
    CJK = re.compile(r"[\u4e00-\u9fff]+")

    @classmethod
    def terms(cls, text: str) -> set[str]:
        found = {
            token.lower()
            for token in cls.ASCII_TERM.findall(text)
            if len(token) > 1
        }
        for phrase in cls.CJK.findall(text):
            if len(phrase) == 1:
                found.add(phrase)
            else:
                found.update(
                    phrase[index : index + 2]
                    for index in range(len(phrase) - 1)
                )
        return found

    @classmethod
    def score(
        cls,
        task_summary: str,
        applies_when: list[str],
        does_not_apply_when: list[str],
    ) -> tuple[set[str], set[str], set[str]]:
        query = cls.terms(task_summary)
        applies = query & cls.terms(" ".join(applies_when))
        excludes = query & cls.terms(" ".join(does_not_apply_when))
        signal = applies - ENGLISH_STOPWORDS
        return applies, excludes, signal


def clamp(value: Any, default: float = 0.5) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def slug(value: Any) -> str | None:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text or None


def short_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "y", "1"}:
            return True
        if text in {"false", "no", "n", "0"}:
            return False
    raise ValueError(f"expected a boolean value, got {value!r}")


def unwrap(data: Any) -> dict[str, Any]:
    """Return the object itself, or the single-key wrapper that holds it."""
    if not isinstance(data, dict):
        raise ValueError("the response must be a JSON object")
    for key in ("version", "outcome", "reusable", "failure_observations", "applies_when"):
        if key in data:
            return data
    nested = [value for value in data.values() if isinstance(value, dict)]
    if len(data) == 1 and nested:
        return unwrap(nested[0])
    return data


def require(data: dict[str, Any], keys: tuple[str, ...], kind: str) -> None:
    missing = [key for key in keys if key not in data or data[key] is None or data[key] == ""]
    if missing:
        raise ValueError(f"{kind} is missing required field(s): {', '.join(missing)}")


def normalize_eligibility(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    decision = str(raw.get("decision") or "").strip().lower()
    reason = str(raw.get("reason") or "").strip().lower()
    if reason not in REASONS:
        reason = ""
    if decision not in DECISIONS:
        if reason == "workflow_related":
            decision = "related"
        elif reason:
            decision = "unrelated"
        elif any(raw.get(field) for field in PATTERN_FIELDS):
            decision = "related"
        else:
            decision = "uncertain"
    if decision == "related":
        reason = "workflow_related"
    elif not reason or reason == "workflow_related":
        reason = "unknown"
    return {
        "decision": decision,
        "reason": reason,
        "confidence": clamp(raw.get("confidence")),
        "rationale": short_text(raw.get("rationale"), 1000) or "model rationale unavailable",
    }


def normalize_observation(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    eligibility = normalize_eligibility(raw.get("eligibility"))
    observation: dict[str, Any] = {
        "eligibility": eligibility,
        "skill_name": short_text(raw.get("skill_name"), 200) or None,
        "workflow_key": short_text(raw.get("workflow_key"), 160) or None,
        "matched_cluster_id": short_text(raw.get("matched_cluster_id"), 200) or None,
        "pattern_key": slug(raw.get("pattern_key")),
        "pattern_summary": short_text(raw.get("pattern_summary"), 600) or None,
        "applies_when": string_list(raw.get("applies_when")),
        "does_not_apply_when": string_list(raw.get("does_not_apply_when")),
        "recovery_principle": short_text(raw.get("recovery_principle"), 1000) or None,
    }
    if eligibility["decision"] == "related":
        missing = [field for field in PATTERN_FIELDS if not observation[field]]
        if missing or not observation["applies_when"] or not observation["does_not_apply_when"]:
            observation["eligibility"] = {
                "decision": "uncertain",
                "reason": "unknown",
                "confidence": eligibility["confidence"],
                "rationale": (
                    "downgraded: a related observation requires a normalized pattern "
                    "(missing: " + ", ".join(missing or ["scoped applicability"]) + ")"
                ),
            }
            for field in PATTERN_FIELDS:
                observation[field] = None
            observation["applies_when"] = []
            observation["does_not_apply_when"] = []
    return observation


def normalize_assessment(data: Any) -> dict[str, Any]:
    data = unwrap(data)
    require(data, ("outcome", "rationale"), "experience_assessment_v1")
    observations = [normalize_observation(item) for item in (data.get("failure_observations") or [])]
    outcome = str(data.get("outcome") or "").strip().lower()
    outcome = OUTCOME_SYNONYMS.get(outcome, outcome)
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {' | '.join(OUTCOMES)}, got {outcome!r}")
    reusable = coerce_bool(data.get("reusable", False))
    candidate = data.get("skill_candidate")
    if not isinstance(candidate, dict) or outcome not in {"success", "mixed"} or not reusable:
        candidate = None
    return {
        "version": "experience_assessment_v1",
        "outcome": outcome,
        "reusable": reusable,
        "confidence": clamp(data.get("confidence")),
        "rationale": short_text(data.get("rationale"), 2000),
        "skill_candidate": candidate,
        "failure_observations": observations,
        "contradicted_lesson_ids": string_list(data.get("contradicted_lesson_ids")),
        "conflicts": string_list(data.get("conflicts")),
    }


def normalize_lesson(data: Any) -> dict[str, Any]:
    data = unwrap(data)
    require(
        data,
        ("workflow_key", "applies_when", "does_not_apply_when", "failure_mode", "recommendation"),
        "lesson_proposal",
    )
    severity = str(data.get("severity") or "").strip().lower()
    if severity not in {"advisory", "important"}:
        severity = "advisory"
    return {
        "skill_name": short_text(data.get("skill_name"), 200) or None,
        "workflow_key": short_text(data.get("workflow_key"), 160),
        "applies_when": string_list(data.get("applies_when")),
        "does_not_apply_when": string_list(data.get("does_not_apply_when")),
        "failure_mode": short_text(data.get("failure_mode"), 600),
        "recommendation": short_text(data.get("recommendation"), 1000),
        "severity": severity,
        "supersedes_lesson_ids": string_list(data.get("supersedes_lesson_ids")),
    }


def normalize_validation(data: Any) -> dict[str, Any]:
    data = unwrap(data)
    require(
        data,
        ("reusable", "contains_specific_answer", "rationale"),
        "lesson_abstraction_validation_v1",
    )
    return {
        "version": "lesson_abstraction_validation_v1",
        "reusable": coerce_bool(data.get("reusable")),
        "contains_specific_answer": coerce_bool(data.get("contains_specific_answer")),
        "unsupported_literals": string_list(data.get("unsupported_literals")),
        "confidence": clamp(data.get("confidence")),
        "rationale": short_text(data.get("rationale"), 1000),
    }


REPAIR_INSTRUCTION = (
    "Your previous JSON failed contract validation:\n{error}\n\n"
    "Return exactly one corrected JSON object with the same content. "
    "Allowed closed vocabularies:\n"
    "- outcome: {outcomes}\n"
    "- failure_observations[].eligibility.decision: {decisions}\n"
    "- failure_observations[].eligibility.reason: {reasons}\n"
    "An observation with decision=\"related\" must set reason=\"workflow_related\" and provide "
    "workflow_key, pattern_key (hyphen-case), pattern_summary, recovery_principle, applies_when "
    "and does_not_apply_when. Any other decision must not use reason=\"workflow_related\". "
    "Booleans must be JSON true/false. Output JSON only."
)

STRICT_LESSON_SYNTHESIS_PROMPT = LESSON_SYNTHESIS_PROMPT + """

Additional activation constraint:
- Copy only process claims directly supported by every normalized observation.
- Reuse the observations' own recovery-principle vocabulary when possible.
- Do not invent fallback actions, capabilities, or extra checks. In particular, do not
  add handoff, abort, unverified, revisit, corroboration, or reproducibility claims unless
  the same claim is explicitly present in the normalized observations.
- When uncertain, omit the claim. Keep each field concise and source-grounded so the
  abstraction validator can return unsupported_literals=[].
"""


class RobustAnalyzer(ModelExperienceAnalyzer):
    """ModelExperienceAnalyzer with strict normalisation and repair retries."""

    max_attempts = 3

    async def assess(
        self,
        episode,
        *,
        candidates,
        lessons,
        clusters,
        skill_catalog,
    ) -> ExperienceAssessment:
        self._retrieval_probe = episode.task_summary
        payload = {
            "episode": episode.model_dump(mode="json"),
            "active_candidates": [item.model_dump(mode="json") for item in candidates[:20]],
            "active_lessons": [item.model_dump(mode="json") for item in lessons[:40]],
            "lesson_clusters": [item.model_dump(mode="json") for item in clusters[:40]],
            "registered_skill_catalog": skill_catalog,
        }
        data = await self._robust_json_call(
            EVOLUTION_PROMPT, payload, ExperienceAssessment, normalize_assessment
        )
        return ExperienceAssessment.model_validate(data)

    async def synthesize_lesson(self, cluster, observations) -> LessonProposal:
        payload = {
            "cluster": cluster.model_dump(
                mode="json", exclude={"draft", "validation", "validation_errors"}
            ),
            "normalized_observations": [item.model_dump(mode="json") for item in observations],
        }
        data = await self._robust_json_call(
            STRICT_LESSON_SYNTHESIS_PROMPT, payload, LessonProposal, normalize_lesson,
            post_check=self._check_retrievable,
        )
        # SkillEvolutionManager.validate_cluster_draft requires the draft to target
        # exactly the cluster Skill and workflow; the model only owns the content.
        data["skill_name"] = cluster.skill_name
        data["workflow_key"] = cluster.workflow_key
        return LessonProposal.model_validate(data)

    async def validate_lesson_abstraction(self, cluster, observations, draft):
        payload = {
            "cluster": {
                "skill_name": cluster.skill_name,
                "workflow_key": cluster.workflow_key,
                "canonical_pattern": cluster.canonical_pattern,
            },
            "normalized_observations": [item.model_dump(mode="json") for item in observations],
            "lesson_draft": draft.model_dump(mode="json"),
        }
        data = await self._robust_json_call(
            LESSON_VALIDATION_PROMPT, payload, LessonAbstractionValidation, normalize_validation
        )
        return LessonAbstractionValidation.model_validate(data)

    def _check_retrievable(self, data: dict) -> None:
        probe = getattr(self, "_retrieval_probe", "")
        if not probe:
            return
        applies, excludes, signal = RetrievalScorer.score(
            probe, data["applies_when"], data["does_not_apply_when"]
        )
        if len(applies) > 0 and len(excludes) < len(applies) and len(signal) > 0:
            return
        raise RetrievabilityError(
            "lesson is not retrievable by the originating prompt: "
            f"applies_overlap={len(applies)} {sorted(applies)[:10]} "
            f"excludes_overlap={len(excludes)} {sorted(excludes)[:10]} "
            f"signal_terms={sorted(signal)[:10]}. "
            "Rewrite applies_when to share distinctive terms with the task prompt "
            "and remove prompt-common words from does_not_apply_when."
        )

    async def _robust_json_call(self, system_prompt, payload, model_cls, normalizer, post_check=None):
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            response = await asyncio.wait_for(
                self.provider.chat_with_retry(
                    messages=messages,
                    tools=None,
                    model=self.model,
                    temperature=0.0,
                ),
                timeout=self.timeout_s,
            )
            if response.finish_reason == "error" or not response.content:
                last_error = RuntimeError(response.content or "reflection model returned no content")
                await asyncio.sleep(0.5)
                continue
            text = str(response.content)
            try:
                data = (
                    json_repair.loads(text)
                    if json_repair is not None
                    else json.loads(text)
                )
                data = normalizer(data)
                model_cls.model_validate(data)
                if post_check is not None:
                    post_check(data)
                if attempt > 1:
                    print(f"[robust] {model_cls.__name__} accepted on attempt {attempt}")
                return data
            except Exception as exc:  # noqa: BLE001 - any contract failure is retried
                last_error = exc
                print(f"[robust] {model_cls.__name__} attempt {attempt} failed: "
                      f"{type(exc).__name__}: {str(exc)[:200]}")
                messages = messages[:2] + [
                    {"role": "assistant", "content": text[:6000]},
                    {
                        "role": "user",
                        "content": REPAIR_INSTRUCTION.format(
                            error=str(exc)[:1500],
                            outcomes=" | ".join(OUTCOMES),
                            decisions=" | ".join(DECISIONS),
                            reasons=" | ".join(REASONS),
                        ),
                    },
                ]
        raise last_error if last_error is not None else RuntimeError("reflection failed")
