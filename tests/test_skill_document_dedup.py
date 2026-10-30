"""Skill document projection: one full body per Skill per request."""

from __future__ import annotations

import asyncio
import hashlib
import json

from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl, project_skill_documents
from PhyAgentOS.agent.core.types import Budget, CancellationToken, ExecutionContext


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        session_id="s",
        turn_id="t",
        step_id="t:step:1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


def _activation(session_id: str, turn_id: str, name: str, doc: str, call_id: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "activate_skill",
        "content": json.dumps(
            {
                "ok": True,
                "activation": {
                    "version": "skill_activation_v2",
                    "activation_id": f"act-{call_id}",
                    "skill_name": name,
                    "role": "primary",
                },
                "skill": doc,
            },
            ensure_ascii=False,
        ),
    }


def test_same_document_kept_once_and_metadata_kept() -> None:
    doc = "STEP 1 move\nSTEP 2 verify\n" + "D" * 400
    history = [
        {"role": "user", "content": "do the task"},
        _activation("s", "t", "robot", doc, "call-1"),
        {"role": "assistant", "content": "working"},
        _activation("s", "t", "robot", doc, "call-2"),
    ]
    messages = [dict(item) for item in history]
    omitted = project_skill_documents(messages)

    assert omitted == 1
    assert [m.get("tool_call_id") for m in messages if m.get("role") == "tool"] == ["call-1", "call-2"]
    first = json.loads(messages[1]["content"])
    second = json.loads(messages[3]["content"])
    assert "skill" not in first
    assert first["activation"]["activation_id"] == "act-call-1"
    assert first["skill_document_omitted"]["sha256"] == hashlib.sha256(doc.encode()).hexdigest()
    assert second["skill"] == doc


def test_summary_without_document_is_not_treated_as_a_copy() -> None:
    summary_only = {
        "role": "tool",
        "tool_call_id": "call-0",
        "name": "activate_skill",
        "content": json.dumps({"ok": True, "activation": {"skill_name": "robot"}, "summary": "short"}),
    }
    doc = "FULL BODY" + "D" * 400
    history = [summary_only, _activation("s", "t", "robot", doc, "call-1")]
    messages = [dict(item) for item in history]
    assert project_skill_documents(messages) == 0
    assert json.loads(messages[1]["content"])["skill"] == doc


def test_different_versions_each_keep_one_body() -> None:
    v1 = "STEP v1" + "A" * 400
    v2 = "STEP v2 changed" + "B" * 400
    history = [
        _activation("s", "t", "robot", v1, "call-1"),
        _activation("s", "t", "robot", v2, "call-2"),
        _activation("s", "t", "robot", v1, "call-3"),
    ]
    messages = [dict(item) for item in history]
    assert project_skill_documents(messages) == 1
    first = json.loads(messages[0]["content"])
    second = json.loads(messages[1]["content"])
    third = json.loads(messages[2]["content"])
    assert "skill" not in first
    assert first["skill_document_omitted"]["sha256"] == hashlib.sha256(v1.encode()).hexdigest()
    assert first["activation"]["activation_id"] == "act-call-1"
    assert second["skill"] == v2
    assert third["skill"] == v1


def test_different_skills_keep_their_own_body() -> None:
    history = [
        _activation("s", "t", "alpha", "A" * 400, "call-a"),
        _activation("s", "t", "beta", "B" * 400, "call-b"),
    ]
    messages = [dict(item) for item in history]
    assert project_skill_documents(messages) == 0
    assert "skill" in json.loads(messages[0]["content"])
    assert "skill" in json.loads(messages[1]["content"])


def test_assembler_projection_keeps_tool_pairing_and_adds_system_once() -> None:
    doc = "STEP" + "D" * 400
    history = [
        {"role": "user", "content": "hi"},
        _activation("s", "t", "robot", doc, "call-1"),
        _activation("s", "t", "robot", doc, "call-2"),
    ]
    bundle = asyncio.run(
        PromptAssemblerImpl("SYS").assemble(_ctx(), history=history, tools=())
    )
    messages = list(bundle.messages)
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert [m.get("tool_call_id") for m in messages if m.get("role") == "tool"] == ["call-1", "call-2"]
    assert sum(
        1
        for m in messages
        if m.get("role") == "tool" and isinstance(json.loads(m.get("content") or "{}"), dict)
        and "skill" in json.loads(m.get("content") or "{}")
    ) == 1
    # The caller's history is never mutated.
    assert "skill" in json.loads(history[1]["content"])


def test_short_duplicate_skill_body_is_never_replaced_by_a_longer_marker() -> None:
    short = "tiny"
    history = [
        _activation("s", "t", "robot", short, "call-1"),
        _activation("s", "t", "robot", short, "call-2"),
    ]
    messages = [dict(item) for item in history]
    before = [m["content"] for m in messages]
    assert project_skill_documents(messages) == 0
    assert [m["content"] for m in messages] == before
