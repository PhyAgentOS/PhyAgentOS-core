"""Bounded, content-keyed prompt-estimate caching.

The cache only avoids repeated tokenization; it never invents a count and it is
invalidated by any content, tool-schema or model change.
"""

from __future__ import annotations

import pytest

import PhyAgentOS.utils.helpers as helpers


@pytest.fixture(autouse=True)
def _fresh_cache():
    helpers.clear_prompt_estimate_cache()
    yield
    helpers.clear_prompt_estimate_cache()


def _count_encodes(monkeypatch) -> dict[str, int]:
    calls = {"count": 0}
    original = helpers._encode_text

    def wrapped(enc, text):
        calls["count"] += 1
        return original(enc, text)

    monkeypatch.setattr(helpers, "_encode_text", wrapped)
    return calls


def _messages() -> list[dict]:
    return [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "u" * 200},
    ]


def _tools(description: str = "move the arm") -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "move",
                "description": description,
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


def test_repeated_request_encodes_only_once(monkeypatch):
    calls = _count_encodes(monkeypatch)
    first = helpers.estimate_prompt_tokens(_messages())
    after_first = calls["count"]
    second = helpers.estimate_prompt_tokens(_messages())
    assert first == second > 0
    assert after_first == 2  # one per message
    assert calls["count"] == after_first  # second call is a full cache hit


def test_appended_message_encodes_only_the_new_message(monkeypatch):
    calls = _count_encodes(monkeypatch)
    messages = _messages()
    helpers.estimate_prompt_tokens(messages)
    baseline = calls["count"]
    messages.append({"role": "assistant", "content": "a" * 200})
    grown = helpers.estimate_prompt_tokens(messages)
    assert calls["count"] == baseline + 1
    assert grown > helpers.estimate_prompt_tokens(messages[:2])


def test_changed_message_is_a_cache_miss(monkeypatch):
    calls = _count_encodes(monkeypatch)
    messages = _messages()
    before = helpers.estimate_prompt_tokens(messages)
    messages[1]["content"] = "z" * 400
    after = helpers.estimate_prompt_tokens(messages)
    assert calls["count"] == 3  # two cached + the edited one
    assert after > before


def test_changed_tool_schema_is_a_cache_miss(monkeypatch):
    calls = _count_encodes(monkeypatch)
    messages = _messages()
    first = helpers.estimate_prompt_tokens(messages, _tools("move the arm"))
    baseline = calls["count"]
    second = helpers.estimate_prompt_tokens(messages, _tools("move the arm"))
    assert second == first
    assert calls["count"] == baseline

    changed = helpers.estimate_prompt_tokens(messages, _tools("move the gripper"))
    assert changed != first
    assert calls["count"] == baseline + 1  # only the tool schema was re-encoded


def test_removed_tools_and_empty_messages_stay_consistent(monkeypatch):
    _count_encodes(monkeypatch)
    with_tools = helpers.estimate_prompt_tokens(_messages(), _tools())
    without_tools = helpers.estimate_prompt_tokens(_messages())
    assert without_tools < with_tools
    assert helpers.estimate_prompt_tokens([]) == 0


class _Counter:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def estimate_prompt_tokens(self, messages, tools, model):
        self.calls.append(model)
        return (1_000 if model == "a" else 2_000), "counter"


def test_provider_counter_results_are_not_shared_across_models():
    counter = _Counter()
    messages = _messages()
    assert helpers.estimate_prompt_tokens_chain(counter, "a", messages) == (1_000, "counter")
    assert helpers.estimate_prompt_tokens_chain(counter, "a", messages) == (1_000, "counter")
    assert len(counter.calls) == 1  # exact request cached

    assert helpers.estimate_prompt_tokens_chain(counter, "b", messages) == (2_000, "counter")
    assert counter.calls == ["a", "b"]
    assert helpers.estimate_prompt_tokens_chain(counter, "a", messages) == (1_000, "counter")
    assert counter.calls == ["a", "b"]  # returning to model a hits its own entry


def test_provider_counter_cache_is_invalidated_by_messages_and_tools():
    counter = _Counter()
    messages = _messages()
    helpers.estimate_prompt_tokens_chain(counter, "a", messages, _tools())
    helpers.estimate_prompt_tokens_chain(counter, "a", messages, _tools())
    assert counter.calls == ["a"]

    helpers.estimate_prompt_tokens_chain(counter, "a", messages, _tools("gripper"))
    assert counter.calls == ["a", "a"]

    messages.append({"role": "assistant", "content": "new"})
    helpers.estimate_prompt_tokens_chain(counter, "a", messages, _tools("gripper"))
    assert counter.calls == ["a", "a", "a"]


def test_provider_caches_do_not_leak_between_provider_objects():
    first, second = _Counter(), _Counter()
    helpers.estimate_prompt_tokens_chain(first, "a", _messages())
    helpers.estimate_prompt_tokens_chain(second, "a", _messages())
    assert first.calls == ["a"]
    assert second.calls == ["a"]


def test_local_estimate_source_reports_heuristic_without_a_tokenizer(monkeypatch):
    monkeypatch.setattr(helpers, "_get_encoder", lambda: None)
    tokens, source = helpers.estimate_prompt_tokens_chain(None, "m", _messages())
    assert tokens > 0
    assert source == "heuristic"


def test_automatic_path_checks_the_local_cache_once_but_warm_retries(monkeypatch):
    calls = {"count": 0}

    def counted():
        calls["count"] += 1
        return None

    monkeypatch.setattr(helpers, "_local_tokenizer_bytes", counted)
    _reset_encoder(monkeypatch)
    helpers.estimate_prompt_tokens(_messages())
    helpers.estimate_prompt_tokens(_messages())
    assert calls["count"] == 1  # no per-render stat of the cache
    assert helpers.warm_token_encoder() is False
    assert calls["count"] == 2  # an explicit warm retries the local check


def test_missing_tokenizer_is_not_downloaded(monkeypatch):
    if helpers.tiktoken is None:  # pragma: no cover - optional dependency
        pytest.skip("tiktoken is not installed")
    monkeypatch.setattr(helpers, "_local_tokenizer_bytes", lambda: None)
    monkeypatch.setattr(helpers, "_encoder_checked", False)
    monkeypatch.setattr(helpers, "_encoder", None)
    from tiktoken import load as tiktoken_load

    fetches = []

    def no_fetch(*args, **kwargs):
        fetches.append(args)
        raise AssertionError("hidden download")

    monkeypatch.setattr(tiktoken_load, "read_file", no_fetch)
    assert helpers.warm_token_encoder() is False
    assert fetches == []


def _reset_encoder(monkeypatch) -> None:
    monkeypatch.setattr(helpers, "_encoder_checked", False)
    monkeypatch.setattr(helpers, "_encoder", None)


def test_corrupt_local_tokenizer_is_ignored_without_fetch_or_delete(tmp_path, monkeypatch):
    import hashlib as _hashlib

    if helpers.tiktoken is None:  # pragma: no cover - optional dependency
        pytest.skip("tiktoken is not installed")
    cache = tmp_path / "data-gym-cache"
    cache.mkdir()
    name = _hashlib.sha1(helpers._TOKENIZER_BLOB.encode()).hexdigest()
    corrupt = cache / name
    corrupt.write_bytes(b"not a bpe file")
    before = corrupt.read_bytes()
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(cache))
    _reset_encoder(monkeypatch)
    from tiktoken import load as tiktoken_load

    fetches = []

    def no_fetch(*args, **kwargs):
        fetches.append(args)
        raise AssertionError("hidden download")

    monkeypatch.setattr(tiktoken_load, "read_file", no_fetch)

    assert helpers.warm_token_encoder() is False
    assert fetches == []  # the corrupt cache was never refetched
    assert corrupt.exists()
    assert corrupt.read_bytes() == before  # not deleted, truncated or rewritten


def test_warm_upgrades_a_heuristic_session(monkeypatch):
    if helpers.tiktoken is None:  # pragma: no cover - optional dependency
        pytest.skip("tiktoken is not installed")
    real_loader = helpers._local_tokenizer_bytes
    if real_loader() is None:
        pytest.skip("no local tokenizer cache to upgrade from")

    monkeypatch.setattr(helpers, "_local_tokenizer_bytes", lambda: None)
    _reset_encoder(monkeypatch)
    first, source = helpers.estimate_prompt_tokens_chain(None, "m", _messages())
    assert source == "heuristic" and first > 0

    monkeypatch.setattr(helpers, "_local_tokenizer_bytes", real_loader)
    assert helpers.warm_token_encoder() is True
    second, source = helpers.estimate_prompt_tokens_chain(None, "m", _messages())
    assert source == "tiktoken"
    assert second > 0
    # Heuristic entries were dropped; the tokenizer count is a fresh result.
    assert (second, source) != (first, "heuristic")


def test_cached_tokenizer_matches_the_installed_reference(monkeypatch):
    if helpers.tiktoken is None:  # pragma: no cover - optional dependency
        pytest.skip("tiktoken is not installed")
    if helpers._local_tokenizer_bytes() is None:
        pytest.skip("no local tokenizer cache")
    _reset_encoder(monkeypatch)
    assert helpers.warm_token_encoder() is True
    sample = "hello world, 中文 token estimate"
    reference = helpers.tiktoken.get_encoding("cl100k_base")
    assert helpers._get_encoder().encode(sample) == reference.encode(sample)


def test_tool_key_order_cannot_share_a_different_count(monkeypatch):
    calls = _count_encodes(monkeypatch)
    messages = _messages()
    tools_a = [
        {
            "type": "function",
            "function": {
                "name": "move",
                "description": "move the arm",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    tools_b = [
        {
            "function": {
                "parameters": {"properties": {}, "type": "object"},
                "description": "move the arm",
                "name": "move",
            },
            "type": "function",
        }
    ]
    first = helpers.estimate_prompt_tokens(messages, tools_a)
    baseline = calls["count"]
    second = helpers.estimate_prompt_tokens(messages, tools_b)
    assert second == first
    assert calls["count"] == baseline  # canonical form, not two counts for one key


def test_provider_counter_has_an_explicit_invalidation():
    class MutableCounter:
        value = 100

        def estimate_prompt_tokens(self, messages, tools, model):
            return self.value, "counter"

    counter = MutableCounter()
    messages = _messages()
    assert helpers.estimate_prompt_tokens_chain(counter, "a", messages) == (100, "counter")
    counter.value = 200
    assert helpers.estimate_prompt_tokens_chain(counter, "a", messages) == (100, "counter")
    helpers.clear_prompt_estimate_cache(counter)
    assert helpers.estimate_prompt_tokens_chain(counter, "a", messages) == (200, "counter")
