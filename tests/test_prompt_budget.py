"""Budget estimates must include the parts that actually reach the provider."""

from __future__ import annotations

from PhyAgentOS.utils.helpers import estimate_message_tokens, estimate_prompt_tokens


def _assistant_with_tool_call(payload: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "write_file", "arguments": payload},
            }
        ],
    }


def test_prompt_estimate_counts_tool_call_arguments() -> None:
    plain = [{"role": "assistant", "content": ""}]
    with_call = [_assistant_with_tool_call('{"path": "' + "a" * 800 + '"}')]

    assert estimate_prompt_tokens(with_call) > estimate_prompt_tokens(plain) + 100
    assert estimate_message_tokens(with_call[0]) > estimate_message_tokens(plain[0]) + 100


def test_prompt_estimate_counts_reasoning_and_images() -> None:
    base = [{"role": "assistant", "content": ""}]
    reasoning = [{"role": "assistant", "content": "", "reasoning_content": "x" * 500}]
    assert estimate_prompt_tokens(reasoning) > estimate_prompt_tokens(base) + 30

    image = [{
        "role": "user",
        "content": [{
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + "A" * 4000},
        }],
    }]
    assert estimate_prompt_tokens(image) >= 1024
