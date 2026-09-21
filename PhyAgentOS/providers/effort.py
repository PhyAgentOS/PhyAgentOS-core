"""Reasoning controls supported by the actual request adapter.

Reasoning output alone is not evidence that a model accepts effort levels.
LiteLLM owns the native thinking/budget translation for its adapters; we
require both model capability and an adapter mapping, and never drop effort.
"""

import re

from PhyAgentOS.providers.registry import ProviderSpec

EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
_BASE_LEVELS = ("low", "medium", "high")


class EffortError(ValueError):
    """A local, safe-to-display capability or mapping error."""


def is_openai_reasoning_model(model: str) -> bool:
    """Models that need reasoning-compatible token and sampling parameters."""
    return bool(re.match(r"(?:gpt-5(?:[.-]|$)|o[134](?:-|$))", model.split("/")[-1].lower()))


def routed_model(spec: ProviderSpec, model: str) -> str:
    prefix = spec.litellm_prefix
    if "/" in model and model.split("/", 1)[0].replace("-", "_") == spec.name:
        model = f"{prefix}/{model.split('/', 1)[1]}" if prefix else model.split("/", 1)[1]
    if spec.strip_model_prefix:
        model = model.split("/")[-1]
    if prefix and not model.startswith(prefix + "/") and not any(
        model.startswith(p) for p in spec.skip_prefixes
    ):
        model = f"{prefix}/{model}"
    return model


def supports_effort(spec: ProviderSpec, model: str) -> bool:
    return bool(supported_efforts(spec, model))


def _catalog_levels(info: dict) -> tuple[str, ...]:
    """Honor explicit exclusions; extra levels require positive catalog evidence."""
    declared = info.get("reasoning_effort_levels")
    levels = []
    for level in EFFORT_LEVELS:
        capability = info.get(f"supports_{level}_reasoning_effort")
        if capability is False:
            continue
        if isinstance(declared, (list, tuple)):
            allowed = level in declared
        else:
            allowed = capability is True or level in _BASE_LEVELS
        if allowed:
            levels.append(level)
    return tuple(levels)


def supported_efforts(spec: ProviderSpec, model: str) -> tuple[str, ...]:
    """Selectable overrides, excluding the application's 'none' reset option."""
    bare = model.split("/")[-1].lower()
    if spec.name in {"openai_codex", "azure_openai", "custom"}:
        import litellm

        # Direct OpenAI-compatible adapters pass effort through unchanged. Unknown
        # deployment aliases cannot establish support for additional effort levels.
        if not re.match(r"(?:gpt-5(?:[.-]|$)|o[134](?:-|$)|gpt-oss(?:-|$))", bare):
            return ()
        info = litellm.model_cost.get(f"openai/{bare}") or litellm.model_cost.get(bare, {})
        if info.get("supports_reasoning") is False:
            return ()
        return _catalog_levels(info)
    return supported_routed_efforts(routed_model(spec, model))


def supports_routed_effort(model: str) -> bool:
    return bool(supported_routed_efforts(model))


def supported_routed_efforts(model: str) -> tuple[str, ...]:
    import litellm
    from litellm.utils import get_optional_params

    # DeepSeek's adapter translates every positive level to the same on/off
    # switch. That is not an adjustable reasoning effort control.
    if "deepseek" in model.lower():
        return ()
    try:
        if not litellm.supports_reasoning(model=model) or "reasoning_effort" not in (
            litellm.get_supported_openai_params(model=model) or []
        ):
            return ()
        native_model, adapter, _, _ = litellm.get_llm_provider(model=model)
        # Prefer route-specific metadata. Only native OpenAI/Anthropic routes may
        # use bare entries; gateway capability is not inferred from another API.
        info = litellm.model_cost.get(model) or litellm.model_cost.get(f"{adapter}/{native_model}")
        if info is None and adapter in {"openai", "anthropic"}:
            info = litellm.model_cost.get(native_model)
        candidates = _catalog_levels(info or {})
    except Exception:
        return ()
    supported = []
    for level in candidates:
        try:
            native = get_optional_params(
                model=native_model, custom_llm_provider=adapter,
                reasoning_effort=level, drop_params=False,
            )
            # A capability flag alone is insufficient if this adapter drops it.
            fields = {**native, **(native.get("extra_body") or {})}
            if any(fields.get(key) for key in (
                "reasoning_effort", "reasoning", "thinking", "thinkingConfig", "thinking_config",
            )):
                supported.append(level)
        except Exception:
            continue
    return tuple(supported)


def validate_effort_level(effort: str | None, supported: tuple[str, ...]) -> None:
    if effort is None or effort == "none":
        return
    if effort not in EFFORT_LEVELS:
        raise EffortError("Reasoning effort must be " + ", ".join((*EFFORT_LEVELS, "none")) + ".")
    if effort not in supported:
        raise EffortError(
            effort_error() + " Available options: " + ", ".join(("none", *supported)) + "."
        )


def effort_error() -> str:
    return (
        "Reasoning effort is not supported by this provider/model in the local "
        "capability catalog, or its adapter cannot map effort levels. "
        "Use /effort none (CLI: --reasoning-effort none) or choose a supported model."
    )


def apply_litellm_effort(kwargs: dict, effort: str | None) -> None:
    if not effort or effort == "none":
        return
    model = kwargs["model"]
    validate_effort_level(effort, supported_routed_efforts(model))
    kwargs["reasoning_effort"] = effort
    kwargs["drop_params"] = False
    # Several reasoning APIs forbid sampling overrides. Let the model choose.
    kwargs.pop("temperature", None)
    from litellm import get_llm_provider

    native_model, adapter, _, _ = get_llm_provider(model=model)
    if adapter == "anthropic":
        from litellm.llms.anthropic.chat.transformation import AnthropicConfig

        native = AnthropicConfig().map_openai_params(
            {"reasoning_effort": effort}, {}, native_model, False,
        )
        budget = native.get("thinking", {}).get("budget_tokens", 0)
        # Legacy Claude requires max_tokens > thinking budget. Preserve the
        # configured output allowance in addition to the mapped thinking budget.
        if budget:
            kwargs["max_tokens"] += budget
