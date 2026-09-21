"""Shared provider configuration and isolated, process-local session runtimes."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PhyAgentOS.config.loader import load_config, save_config
from PhyAgentOS.config.schema import Config, ProviderConfig
from PhyAgentOS.providers.base import GenerationSettings, LLMProvider
from PhyAgentOS.providers.errors import describe_provider_error
from PhyAgentOS.providers.registry import PROVIDERS, ProviderSpec, find_by_name


class ProviderError(ValueError):
    """Safe, actionable errors suitable for display without credential leakage."""


def provider_spec(name: str) -> ProviderSpec:
    spec = find_by_name(name.lower().replace("-", "_"))
    if spec is None:
        raise ProviderError("Unknown provider. Run 'paos provider list' for supported names.")
    return spec


def mask_secret(value: str | None) -> str:
    if not value:
        return "not set"
    return value[:4] + "************" + value[-4:] if len(value) > 16 else "****"


def safe_endpoint(value: str | None) -> str:
    if not value:
        return "provider default (not tested)"
    try:
        parts = urlsplit(value)
        # Paths can contain embedded access tokens too; only expose the origin.
        return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], "", "", ""))
    except ValueError:
        return "configured (not tested)"


def oauth_configured(name: str) -> bool:
    """Inspect local credentials only; never log in, refresh or import tokens."""
    try:
        if name == "openai_codex":
            from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
            from oauth_cli_kit.storage import FileTokenStorage

            token = FileTokenStorage(
                token_filename=OPENAI_CODEX_PROVIDER.token_filename,
                import_codex_cli=False,
            ).load()
            if token and token.access and token.account_id:
                return True
            data = json.loads((Path.home() / ".codex/auth.json").read_text())
            tokens = data.get("tokens", {})
            return bool(tokens.get("access_token") and tokens.get("account_id"))
        directory = Path(
            os.environ.get(
                "GITHUB_COPILOT_TOKEN_DIR", str(Path.home() / ".config/litellm/github_copilot")
            )
        )
        return bool(
            (directory / os.environ.get("GITHUB_COPILOT_ACCESS_TOKEN_FILE", "access-token"))
            .read_text()
            .strip()
        )
    except (OSError, ValueError, ImportError, AttributeError):
        return False


@dataclass(frozen=True)
class ProviderSelection:
    name: str
    model: str
    effort: str | None
    endpoint: str | None


@dataclass(frozen=True)
class RuntimeSelection(ProviderSelection):
    provider: LLMProvider = field(repr=False, compare=False)


class ProviderService:
    """Own a deep configuration snapshot. No runtime operation writes global state."""

    def __init__(self, config: Config):
        self.config = config.model_copy(deep=True)
        # Read environment credentials once, without exporting or persisting them.
        for spec in PROVIDERS:
            cfg = getattr(self.config.providers, spec.name)
            if not cfg.enabled:
                continue
            env_name = f"PAOS_{spec.name.upper()}_API_KEY"
            key = os.environ.get(env_name)
            # Gateway aliases sharing OPENAI_API_KEY must not borrow OpenAI credentials.
            shared_gateway_key = spec.is_gateway and spec.env_key == "OPENAI_API_KEY"
            if not key and spec.env_key and not shared_gateway_key:
                key = os.environ.get(spec.env_key)
            for alias, value in spec.env_extras:
                if value == "{api_key}" and not key:
                    key = os.environ.get(alias)
                elif value == "{api_base}" and not cfg.api_base:
                    cfg.api_base = os.environ.get(alias) or None
            if key and not cfg.api_key:
                cfg.api_key = key

    @classmethod
    def load(cls, path: Path | None = None) -> ProviderService:
        return cls(load_config(path, strict=True))

    def configured(self, spec: ProviderSpec) -> bool:
        cfg = getattr(self.config.providers, spec.name)
        if not cfg.enabled:
            return False
        if spec.is_oauth:
            return oauth_configured(spec.name)
        if spec.is_local:
            return bool(cfg.api_base or (
                spec.default_api_base and (cfg.api_key or cfg.default_model or cfg.models)
            ))
        if spec.name == "custom":
            return bool(cfg.api_base)
        return bool(cfg.api_key and (spec.name != "azure_openai" or cfg.api_base))

    def list(self) -> list[dict]:
        default = self.config.get_provider_name()
        rows = []
        for spec in PROVIDERS:
            if spec.is_oauth:
                auth = "oauth"
            elif spec.is_local or spec.name == "custom":
                auth = "optional key"
            else:
                auth = "api key"
            rows.append(
                dict(
                    name=spec.name,
                    label=spec.label,
                    supported=True,
                    configured=self.configured(spec),
                    default=spec.name == default and self.configured(spec),
                    auth=auth,
                )
            )
        return rows

    def show(self, name: str) -> dict:
        spec = provider_spec(name)
        cfg = getattr(self.config.providers, spec.name)
        return dict(
            next(item for item in self.list() if item["name"] == spec.name),
            api_key=mask_secret(cfg.api_key),
            endpoint=(
                safe_endpoint(cfg.api_base or spec.default_api_base)
                if cfg.enabled and (cfg.api_base or self.configured(spec)) else "not set"
            ),
            model=cfg.default_model,
            models=self.models(spec.name),
            extra_headers={key: "****" for key in (cfg.extra_headers or {})},
        )

    @staticmethod
    def validate_endpoint(value: str | None) -> None:
        if value:
            try:
                parts = urlsplit(value)
                valid = (
                    parts.scheme in {"http", "https"}
                    and parts.hostname
                    and not parts.username
                    and not parts.password
                    and not parts.fragment
                )
                _ = parts.port
            except ValueError:
                valid = False
            if not valid:
                raise ProviderError(
                    "Endpoint configuration error: use an http(s) URL without embedded credentials."
                )

    @staticmethod
    def validate_headers(headers: dict[str, str] | None) -> None:
        for key, value in (headers or {}).items():
            if (
                not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", key)
                or "\n" in value
                or "\r" in value
            ):
                raise ProviderError("Invalid extra header name or value.")

    @staticmethod
    def routes(spec: ProviderSpec, model: str) -> bool:
        if not model or any(c.isspace() for c in model):
            return False
        prefix, _, bare = model.partition("/")
        normalized = prefix.lower().replace("-", "_")
        if spec.is_gateway or spec.is_local or spec.name in {"custom", "azure_openai"}:
            return True
        if bare and normalized == spec.name:
            return ProviderService.routes(spec, bare)
        if spec.name == "groq" and model.startswith(("meta-llama/", "qwen/", "openai/gpt-oss")):
            return True
        if bare and find_by_name(normalized):
            return False
        lower = model.lower()
        if spec.name in {"openai", "openai_codex"}:
            matches = bool(re.match(r"(?:gpt-|o[134](?:-|$)|chatgpt-)", lower))
        elif spec.name == "github_copilot":
            matches = lower.startswith(("gpt-", "claude-", "gemini-", "o1", "o3", "o4"))
        elif spec.name == "groq":
            matches = lower.startswith(("llama", "meta-llama/", "qwen/", "gemma", "openai/gpt-oss"))
        else:
            matches = any(keyword in lower for keyword in spec.keywords)
        if matches:
            return True

        # Match only this provider's chat catalog, including vendor namespaces
        # such as groq/moonshotai/..., never another provider's bare model entry.
        import litellm

        catalog_prefix = spec.litellm_prefix or spec.name
        routed = model if model.startswith(catalog_prefix + "/") else f"{catalog_prefix}/{model}"
        return litellm.model_cost.get(routed, {}).get("mode") == "chat"

    def background_provider_name(
        self, name: str | None = None, model: str | None = None,
    ) -> str | None:
        """Honor an explicit background provider, or infer its separate model."""
        if name and name != "auto":
            return provider_spec(name).name
        if name != "auto" and (not model or model == self.config.agents.defaults.model):
            return self.config.get_provider_name()
        automatic = self.config.model_copy(deep=True)
        automatic.agents.defaults.provider = "auto"
        return automatic.get_provider_name(model)

    def background_effort(self, name: str | None, model: str) -> str | None:
        """Only inherit main effort when the background target is the same."""
        defaults = self.config.agents.defaults
        if name == self.config.get_provider_name() and model == defaults.model:
            return defaults.reasoning_effort
        return None

    def models(self, name: str) -> list[str]:
        """Saved choices, including defaults from configurations predating model lists."""
        spec = provider_spec(name)
        cfg = getattr(self.config.providers, spec.name)
        if not cfg.enabled:
            return []
        values = [*cfg.models, cfg.default_model]
        if self.config.get_provider_name() == spec.name:
            values.append(self.config.agents.defaults.model)
        return list(dict.fromkeys(m for m in values if m and self.routes(spec, m)))

    async def discover_models(self, name: str) -> list[str]:
        """Test the model-list endpoint without requiring a model or sending chat requests."""
        from PhyAgentOS.providers.discovery import fetch_models

        spec = provider_spec(name)
        cfg = getattr(self.config.providers, spec.name)
        self.validate_endpoint(cfg.api_base)
        self.validate_headers(cfg.extra_headers)
        if not self.configured(spec):
            raise ProviderError("Provider is not configured; supply credentials and API Base first.")
        models = await fetch_models(spec, cfg)
        return list(dict.fromkeys(m for m in models if self.routes(spec, m)))

    @staticmethod
    def validate_effort(spec: ProviderSpec, model: str, effort: str | None) -> None:
        if effort is None:
            return
        # 'none' clears our override and uses the model's default behavior.
        if effort == "none":
            return
        from PhyAgentOS.providers.effort import (
            EffortError,
            supported_efforts,
            validate_effort_level,
        )

        try:
            validate_effort_level(effort, supported_efforts(spec, model))
        except EffortError as exc:
            raise ProviderError(str(exc)) from None

    def selection(
        self, name: str | None = None, model: str | None = None, effort: str | None = None
    ) -> ProviderSelection:
        """Validate configuration without opening clients or making API requests."""
        defaults = self.config.agents.defaults
        chosen = name or defaults.provider
        if chosen == "auto":
            automatic = self.config.model_copy(deep=True)
            automatic.agents.defaults.provider = "auto"
            chosen = automatic.get_provider_name(model) or ""
        if not chosen:
            raise ProviderError("No configured provider. Run 'paos provider configure <provider>'.")
        spec = provider_spec(chosen)
        cfg = getattr(self.config.providers, spec.name)
        model = model or (cfg.default_model if name else None) or defaults.model
        effort = effort if effort is not None else defaults.reasoning_effort
        self.validate_endpoint(cfg.api_base)
        self.validate_headers(cfg.extra_headers)
        if not self.configured(spec):
            action = "login" if spec.is_oauth and cfg.enabled else "configure"
            raise ProviderError(
                f"Provider is not configured. Run 'paos provider {action} {spec.name}'."
            )
        if not self.routes(spec, model):
            raise ProviderError(
                f"Model cannot be routed by {spec.name}. Supply --model or configure its default model."
            )
        self.validate_effort(spec, model, effort)
        if effort == "none":
            effort = None
        endpoint = cfg.api_base or spec.default_api_base or None
        return ProviderSelection(spec.name, model, effort, endpoint)

    def resolve(
        self, name: str | None = None, model: str | None = None, effort: str | None = None
    ) -> RuntimeSelection:
        selection = self.selection(name, model, effort)
        spec = provider_spec(selection.name)
        cfg = getattr(self.config.providers, spec.name)
        try:
            provider = self.create_provider(spec, cfg, selection.model, selection.endpoint)
        except Exception:
            raise ProviderError(
                "Provider initialization failed; check credentials and endpoint configuration."
            ) from None
        defaults = self.config.agents.defaults
        provider.generation = GenerationSettings(
            temperature=defaults.temperature,
            max_tokens=defaults.max_tokens,
            reasoning_effort=selection.effort,
        )
        return RuntimeSelection(
            selection.name, selection.model, selection.effort, selection.endpoint, provider
        )

    @staticmethod
    def create_provider(
        spec: ProviderSpec, cfg: ProviderConfig, model: str, endpoint: str | None
    ) -> LLMProvider:
        if spec.name == "openai_codex":
            from PhyAgentOS.providers.openai_codex_provider import OpenAICodexProvider

            return OpenAICodexProvider(
                default_model=model, api_base=endpoint, extra_headers=cfg.extra_headers
            )
        if spec.name == "custom":
            from PhyAgentOS.providers.custom_provider import CustomProvider

            return CustomProvider(
                api_key=cfg.api_key or "no-key",
                api_base=endpoint,
                default_model=model,
                extra_headers=cfg.extra_headers,
            )
        if spec.name == "azure_openai":
            from PhyAgentOS.providers.azure_openai_provider import AzureOpenAIProvider

            return AzureOpenAIProvider(
                api_key=cfg.api_key,
                api_base=endpoint,
                default_model=model,
                extra_headers=cfg.extra_headers,
            )
        from PhyAgentOS.providers.litellm_provider import LiteLLMProvider

        return LiteLLMProvider(
            api_key=cfg.api_key or ("no-key" if spec.is_local else None),
            api_base=endpoint,
            default_model=model,
            extra_headers=cfg.extra_headers,
            provider_name=spec.name,
        )

    @staticmethod
    def update(path: Path | None, name: str, cfg: ProviderConfig) -> None:
        spec = provider_spec(name)
        ProviderService.validate_endpoint(cfg.api_base)
        values = [*cfg.models, *([cfg.default_model] if cfg.default_model else [])]
        if any(not ProviderService.routes(spec, model) for model in values):
            raise ProviderError("Model cannot be routed by this provider.")
        cfg = cfg.model_copy(update={"models": list(dict.fromkeys(values))})
        ProviderService.validate_headers(cfg.extra_headers)
        config = load_config(path, strict=True)
        setattr(config.providers, spec.name, cfg)
        save_config(config, path)

    @staticmethod
    def remove(path: Path | None, name: str) -> None:
        # OAuth stores are shared with external tools and running jobs; never revoke them here.
        spec = provider_spec(name)
        config = load_config(path, strict=True)
        current = ProviderService(config).config.get_provider_name()
        if config.agents.defaults.provider == spec.name or current == spec.name:
            # An empty selection requires an explicit 'use'; do not silently select another account.
            config.agents.defaults.provider = ""
            config.agents.defaults.model = ""
            config.agents.defaults.reasoning_effort = None
        setattr(config.providers, spec.name, ProviderConfig(enabled=False))
        save_config(config, path)

    @staticmethod
    def use(path: Path | None, name: str, model: str | None) -> ProviderSelection:
        config = load_config(path, strict=True)
        runtime = ProviderService(config).selection(name, model)
        config.agents.defaults.provider = runtime.name
        config.agents.defaults.model = runtime.model
        save_config(config, path)
        return runtime

    async def test(self, name: str, model: str | None = None) -> str:
        runtime = self.resolve(name, model, "none")
        try:
            response = await asyncio.wait_for(
                runtime.provider.chat(
                    messages=[{"role": "user", "content": "Reply OK."}],
                    model=runtime.model,
                    max_tokens=16,
                    temperature=0,
                ),
                timeout=30,
            )
            if response.finish_reason != "error":
                return "Connection successful."
            detail = response.content or ""
        except Exception as exc:
            detail = f"{type(exc).__name__} {exc}"
        raise ProviderError(describe_provider_error(detail))


class SessionRuntimes:
    """Atomically replace selections; in-flight turns retain their original objects."""

    def __init__(self, service: ProviderService, startup: RuntimeSelection):
        self.service = service
        self.startup = startup
        self._overrides: dict[str, RuntimeSelection] = {}

    def get(self, key: str) -> RuntimeSelection:
        return self._overrides.get(key, self.startup)

    def clear(self, key: str) -> None:
        self._overrides.pop(key, None)

    def select(self, key: str, name: str, model: str, effort: str | None = None) -> str:
        """Validate the entire choice before replacing this session's runtime."""
        if effort is None:
            effort = self.get(key).effort or "none"
        candidate = self.service.resolve(name, model, effort)
        self._overrides[key] = candidate
        return (
            f"Updated this session: provider={candidate.name}, model={candidate.model}, "
            f"effort={candidate.effort or 'none'}. Applies to subsequent turns; running tasks are unchanged."
        )

    def command(self, key: str, content: str) -> str | None:
        parts = content.strip().split(maxsplit=1)
        if not parts:
            return None
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) == 2 else ""
        if command not in {"/provider", "/model", "/effort", "/status"}:
            return None
        current = self.get(key)
        if command == "/status":
            return (
                f"Session: {key}\nProvider: {current.name}\nModel: {current.model}\n"
                f"Effort: {current.effort or 'none (model default)'}\n"
                f"Endpoint: {safe_endpoint(current.endpoint)} (not tested)\n"
                "Scope: this session; changes apply to subsequent turns only."
            )
        if not argument or argument == "list":
            if command == "/provider":
                return f"Current provider: {current.name}\n" + "\n".join(
                    f"{row['name']}: {'configured' if row['configured'] else 'not configured'}"
                    for row in self.service.list()
                )
            if command == "/model":
                return (
                    f"Current model: {current.model}\nSaved models for {current.name}:\n"
                    + "\n".join(
                        f"{index}. {model}"
                        for index, model in enumerate(self.service.models(current.name), 1)
                    )
                    + "\nUse /model <number> or /model <model-id> to select a model."
                )
            from PhyAgentOS.providers.effort import supported_efforts

            options = ", ".join(("none (model default)", *supported_efforts(provider_spec(current.name), current.model)))
            return f"Current effort: {current.effort or 'none'}\nOptions: {options}"
        if argument == "reset":
            self.clear(key)
            return "Session overrides cleared; subsequent turns use startup settings."
        name, model, effort = current.name, current.model, current.effort or "none"
        if command == "/provider":
            spec = provider_spec(argument)
            name = spec.name
            cfg = getattr(self.service.config.providers, name)
            model = cfg.default_model or model
        elif command == "/model":
            model = argument
            if argument.isascii() and argument.isdigit():
                models = self.service.models(name)
                index = int(argument) - 1
                if not 0 <= index < len(models):
                    raise ProviderError("Invalid model number. Use /model list to see saved models.")
                model = models[index]
            if not self.service.routes(provider_spec(name), model):
                candidates = [
                    s.name
                    for s in PROVIDERS
                    if not (s.is_gateway or s.is_local or s.is_direct)
                    and self.service.routes(s, model)
                    and self.service.configured(s)
                ]
                if not candidates:
                    raise ProviderError(
                        "No configured provider can route this model. Use /provider first."
                    )
                name = candidates[0]
        else:
            effort = argument.lower()
        return self.select(key, name, model, effort)
