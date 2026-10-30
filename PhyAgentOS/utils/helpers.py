"""Utility functions for PhyAgentOS."""

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

try:
    import tiktoken
except ModuleNotFoundError:  # pragma: no cover - depends on optional install extras
    tiktoken = None

_YAML_BLOCK_RE = re.compile(
    r"(?P<fence>`{3,}|~{3,})\s*(?P<lang>json|yaml|yml)\s*\n(?P<body>.*?)(?:\n(?P=fence)\s*)",
    re.DOTALL | re.IGNORECASE,
)


def load_environment_doc(path: Path) -> dict[str, Any]:
    """Load ENVIRONMENT.md and extract the fenced YAML/JSON block as a dict.

    Supports both the v2 format (fenced JSON/YAML block) and falls back
    gracefully to an empty dict if the file is missing or unparseable.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    match = _YAML_BLOCK_RE.search(text)
    if match is None:
        return {}
    body = match.group("body")
    try:
        lang = match.group("lang").lower()
        if lang == "json":
            data = json.loads(body)
        else:
            data = yaml.safe_load(body) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def detect_image_mime(data: bytes) -> str | None:
    """Detect image MIME type from magic bytes, ignoring file extension."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')

def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def split_message(content: str, max_len: int = 2000) -> list[str]:
    """
    Split content into chunks within max_len, preferring line breaks.

    Args:
        content: The text content to split.
        max_len: Maximum length per chunk (default 2000 for Discord compatibility).

    Returns:
        List of message chunks, each within max_len.
    """
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        # Try to break at newline first, then space, then hard break
        pos = cut.rfind('\n')
        if pos <= 0:
            pos = cut.rfind(' ')
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


def build_assistant_message(
    content: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning_content: str | None = None,
    thinking_blocks: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a provider-safe assistant message with optional reasoning fields."""
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    if thinking_blocks:
        msg["thinking_blocks"] = thinking_blocks
    return msg


_IMAGE_TOKEN_ESTIMATE = 1024
_IMAGE_CHARS_PER_TOKEN = 16

# The tokenizer is loaded once and reused. ``tiktoken`` downloads the BPE file
# on first use; that download would be a hidden network request from a status
# render, so it is only loaded when the file is already in the local cache.
# Without it the estimator uses a character heuristic instead of blocking or
# fetching anything.
_TOKENIZER_ENCODING = "cl100k_base"
_TOKENIZER_BLOB = (
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
)
# Published sha256 of the cl100k BPE file. A local file is used only when it
# matches; a mismatch is treated as "no tokenizer" without deleting or
# rewriting the user's cache file.
_TOKENIZER_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
# The published cl100k_base split pattern and special tokens. Building the
# encoding from the verified local bytes avoids tiktoken's loader, which
# refetches after a corrupt or hash-mismatched cache file.
_CL100K_PAT_STR = (
    r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+|"""
    r""" ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
)
_CL100K_SPECIAL_TOKENS = {
    "<|endoftext|>": 100257,
    "<|fim_prefix|>": 100258,
    "<|fim_middle|>": 100259,
    "<|fim_suffix|>": 100260,
    "<|endofprompt|>": 100276,
}
_PROMPT_ESTIMATE_CACHE_SIZE = 4096
_PROVIDER_EXACT_CACHE_SIZE = 8
_HEURISTIC_ESTIMATOR = "heuristic"

_encoder_lock = threading.Lock()
_encoder_checked = False
_encoder: Any = None

_cache_lock = threading.Lock()
_message_token_cache: OrderedDict[tuple[str, bytes], int] = OrderedDict()
_tools_token_cache: OrderedDict[tuple[str, bytes], int] = OrderedDict()


def _tokenizer_cache_dir() -> str:
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        return os.environ["TIKTOKEN_CACHE_DIR"]
    if "DATA_GYM_CACHE_DIR" in os.environ:
        return os.environ["DATA_GYM_CACHE_DIR"]
    return os.path.join(tempfile.gettempdir(), "data-gym-cache")


def _local_tokenizer_bytes() -> bytes | None:
    """Read the cached BPE file and verify it without touching the network.

    A missing, unreadable or hash-mismatched file yields ``None``. Nothing is
    removed, renamed or rewritten: a corrupt cache is the user's file to fix.
    """
    cache_dir = _tokenizer_cache_dir()
    if not cache_dir:
        return None
    key = hashlib.sha1(_TOKENIZER_BLOB.encode()).hexdigest()
    path = os.path.join(cache_dir, key)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != _TOKENIZER_SHA256:
        return None
    return data


def _load_encoder() -> Any:
    """Build the tokenizer from verified local bytes; never fetch anything."""
    if tiktoken is None:
        return None
    data = _local_tokenizer_bytes()
    if data is None:
        return None
    try:
        mergeable_ranks: dict[bytes, int] = {}
        for line in data.splitlines():
            if not line:
                continue
            token, rank = line.split()
            mergeable_ranks[base64.b64decode(token)] = int(rank)
        return tiktoken.Encoding(
            name=_TOKENIZER_ENCODING,
            pat_str=_CL100K_PAT_STR,
            mergeable_ranks=mergeable_ranks,
            special_tokens=dict(_CL100K_SPECIAL_TOKENS),
        )
    except Exception:
        return None


def _get_encoder() -> Any:
    """Return the reused tokenizer, or ``None`` when it is not local.

    The first automatic call decides once. Only an explicit
    :func:`warm_token_encoder` re-checks the local cache, so a session that
    started without a tokenizer can upgrade later without a per-render stat.
    """
    global _encoder, _encoder_checked
    with _encoder_lock:
        if not _encoder_checked:
            _encoder = _load_encoder()
            _encoder_checked = True
        return _encoder


def warm_token_encoder() -> bool:
    """Retry loading the shared tokenizer from the local cache.

    Returns True when exact tokenization is available. Safe to call at any
    point: it never performs a network request, never writes to the user's
    cache, and on success drops counts produced by the heuristic so later
    estimates cannot report heuristic numbers as tokenizer results. It
    returns quickly when the tokenizer is already loaded.
    """
    global _encoder, _encoder_checked
    with _encoder_lock:
        if _encoder is not None:
            return True
        _encoder = _load_encoder()
        _encoder_checked = True
        loaded = _encoder is not None
    if loaded:
        with _cache_lock:
            _message_token_cache.clear()
            _tools_token_cache.clear()
    return loaded


def clear_prompt_estimate_cache(provider: Any = None) -> None:
    """Drop cached estimates.

    Pass ``provider`` to also drop that provider counter's exact-request
    results. A counter is opaque and may change behavior in place; the owner of
    such a counter must call this after the change instead of reusing stale
    entries.
    """
    with _cache_lock:
        _message_token_cache.clear()
        _tools_token_cache.clear()
    if provider is not None:
        cache = getattr(provider, "_prompt_estimate_cache", None)
        if isinstance(cache, OrderedDict):
            cache.clear()


def _estimator_tag(enc: Any) -> str:
    if enc is None:
        return _HEURISTIC_ESTIMATOR
    name = getattr(enc, "name", None) or _TOKENIZER_ENCODING
    return f"tokenizer:{name}"


def _fingerprint(value: object) -> bytes | None:
    """Stable digest of one request component, or ``None`` when not hashable."""
    try:
        encoded = json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")
    except Exception:
        return None
    return hashlib.blake2b(encoded, digest_size=16).digest()


def _cache_get(
    cache: OrderedDict[tuple[str, bytes], int], key: tuple[str, bytes]
) -> int | None:
    with _cache_lock:
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
        return value


def _cache_put(
    cache: OrderedDict[tuple[str, bytes], int], key: tuple[str, bytes], value: int
) -> None:
    if value <= 0:
        return
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _PROMPT_ESTIMATE_CACHE_SIZE:
            cache.popitem(last=False)


def _encode_text(enc: Any, text: str) -> int:
    if not text:
        return 0
    if enc is not None:
        return len(enc.encode(text))
    # Heuristic: about four characters per token. It is an estimate, not a
    # tokenizer result, and callers report it as such.
    return max(1, len(text) // 4)


def _estimate_message_tokens(enc: Any, message: dict[str, Any]) -> int:
    """Estimate one message, charging images conservatively instead of by URL length."""
    parts: list[str] = []
    image_tokens = 0
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                parts.append(str(part))
                continue
            if part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    parts.append(text)
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                size = len(url) if isinstance(url, str) else 0
                image_tokens += max(_IMAGE_TOKEN_ESTIMATE, min(size // _IMAGE_CHARS_PER_TOKEN, 20_000))
            else:
                parts.append(json.dumps(part, ensure_ascii=False, sort_keys=True))
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False, sort_keys=True))

    for key in ("name", "tool_call_id", "reasoning_content"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if message.get("thinking_blocks"):
        parts.append(json.dumps(message["thinking_blocks"], ensure_ascii=False, sort_keys=True))
    if message.get("tool_calls"):
        parts.append(json.dumps(message["tool_calls"], ensure_ascii=False, sort_keys=True))

    payload = "\n".join(parts)
    text_tokens = _encode_text(enc, payload)
    return max(1, text_tokens + image_tokens)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate prompt tokens contributed by one persisted message."""
    enc = _get_encoder()
    estimator = _estimator_tag(enc)
    fingerprint = _fingerprint(message)
    cache_key = (estimator, fingerprint) if fingerprint is not None else None
    if cache_key is not None:
        cached = _cache_get(_message_token_cache, cache_key)
        if cached is not None:
            return cached
    try:
        tokens = _estimate_message_tokens(enc, message)
    except Exception:
        tokens = max(1, len(json.dumps(message, ensure_ascii=False)) // 4)
    if cache_key is not None:
        _cache_put(_message_token_cache, cache_key, tokens)
    return tokens


def _estimate_tool_tokens(enc: Any, tools: list[dict[str, Any]]) -> int:
    estimator = _estimator_tag(enc)
    fingerprint = _fingerprint(tools)
    cache_key = (estimator, fingerprint) if fingerprint is not None else None
    if cache_key is not None:
        cached = _cache_get(_tools_token_cache, cache_key)
        if cached is not None:
            return cached
    try:
        # Canonical key order, matching the fingerprint: two tools lists that
        # differ only in dict insertion order must not share one cached count.
        tokens = _encode_text(enc, json.dumps(tools, ensure_ascii=False, sort_keys=True))
    except Exception:
        tokens = 0
    if cache_key is not None and tokens > 0:
        _cache_put(_tools_token_cache, cache_key, tokens)
    return tokens


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> int:
    """Estimate prompt tokens, including tool calls, reasoning and images.

    Per-message results are cached by exact content, so extending a history
    re-tokenizes only the new messages. Hashing the input still costs time
    proportional to its size; the saving is repeated tokenization, not a
    zero-cost recount. A changed message or tool definition is a cache miss.
    """
    try:
        enc = _get_encoder()
        total = sum(estimate_message_tokens(message) for message in messages)
        if tools:
            total += _estimate_tool_tokens(enc, tools)
        return total
    except Exception:
        return 0


def _provider_exact_cache(provider: Any) -> OrderedDict[Any, tuple[int, str]] | None:
    """Session-local cache for one provider counter's exact request results.

    A counter is opaque: it may depend on more than the message list, so only
    the whole ``(model, messages, tools)`` request is cached, never a prefix
    count. The cache lives on the provider object, so a new provider or a new
    model selection cannot read another one's entries.
    """
    cache = getattr(provider, "_prompt_estimate_cache", None)
    if isinstance(cache, OrderedDict):
        return cache
    cache = OrderedDict()
    try:
        setattr(provider, "_prompt_estimate_cache", cache)
    except Exception:
        return None
    return cache


def _exact_request_key(
    model: str | None, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> tuple[Any, ...] | None:
    message_keys = []
    for message in messages:
        key = _fingerprint(message)
        if key is None:
            return None
        message_keys.append(key)
    tools_key = _fingerprint(tools) if tools else None
    return (model, tuple(message_keys), tools_key)


def estimate_prompt_tokens_chain(
    provider: Any,
    model: str | None,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Estimate prompt tokens via provider counter first, then tiktoken fallback."""
    provider_counter = getattr(provider, "estimate_prompt_tokens", None)
    if callable(provider_counter):
        cache = _provider_exact_cache(provider)
        key = _exact_request_key(model, messages, tools) if cache is not None else None
        if cache is not None and key is not None:
            hit = cache.get(key)
            if hit is not None:
                cache.move_to_end(key)
                return hit
        try:
            tokens, source = provider_counter(messages, tools, model)
            if isinstance(tokens, (int, float)) and tokens > 0:
                result = (int(tokens), str(source or "provider_counter"))
                if cache is not None and key is not None:
                    cache[key] = result
                    cache.move_to_end(key)
                    while len(cache) > _PROVIDER_EXACT_CACHE_SIZE:
                        cache.popitem(last=False)
                return result
        except Exception:
            pass

    estimated = estimate_prompt_tokens(messages, tools)
    if estimated > 0:
        source = "tiktoken" if _get_encoder() is not None else "heuristic"
        return int(estimated), source
    return 0, "none"


def sync_workspace_templates(
    workspace: Path,
    silent: bool = False,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files
    try:
        tpl = pkg_files("PhyAgentOS") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    effective_exclude = set(exclude or ())

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    for item in tpl.iterdir():
        if not item.name.endswith(".md") or item.name.startswith("."):
            continue
        if include is not None and item.name not in include:
            continue
        if item.name in effective_exclude:
            continue
        _write(item, workspace / item.name)
    _write(tpl / "memory" / "MEMORY.md", workspace / "memory" / "MEMORY.md")
    _write(None, workspace / "memory" / "HISTORY.md")
    (workspace / "skills").mkdir(exist_ok=True)

    configs_tpl = tpl / "configs"
    if configs_tpl.is_dir():
        for item in configs_tpl.iterdir():
            if item.suffix.lower() == ".json" and item.is_file():
                _write(item, workspace / "configs" / item.name)

    if added and not silent:
        from rich.console import Console
        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added
