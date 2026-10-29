"""Provider management commands and a small, inline configuration wizard."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from PhyAgentOS import __logo__
from PhyAgentOS.config.loader import load_config
from PhyAgentOS.config.schema import ProviderConfig
from PhyAgentOS.providers.service import (
    ProviderError,
    ProviderService,
    provider_spec,
)

from ..interaction import model_picker

provider_app = typer.Typer(help="Manage providers")
console = Console()


@contextmanager
def _provider_errors():
    """Never print raw validation exceptions: they can contain secret inputs."""
    try:
        yield
    except ProviderError as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1) from None
    except (KeyboardInterrupt, EOFError, typer.Abort):
        console.print("Cancelled; configuration unchanged.")
        raise typer.Exit(1) from None
    except BlockingIOError:
        console.print("Provider configuration is being updated by another process; retry shortly.")
        raise typer.Exit(1) from None
    except (ValueError, OSError):
        console.print(
            "Cannot read or save provider configuration; check format and file permissions."
        )
        raise typer.Exit(1) from None


def _provider_argument(provider: str | None, service: ProviderService, *, action: str) -> str:
    if provider:
        return provider_spec(provider).name
    if not model_picker._interactive():
        raise ProviderError("A provider name is required outside a TTY. Run 'paos provider list'.")
    return model_picker._select_provider(service, action=action)


def _prompt(message: str, *, default: str = "", secret: bool = False) -> str:
    from prompt_toolkit import prompt
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("escape")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    # Existing secrets are never used as a visible default or written to history.
    suffix = " (Enter to keep existing)" if secret and default else ""
    return (
        prompt(
            f"{message}{suffix}: ",
            default="" if secret else default,
            is_password=secret,
            key_bindings=bindings,
        )
        or default
    )


@provider_app.command("list")
def provider_list(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    config: Path | None = typer.Option(None, "--config", "-c"),
):
    """List built-in providers without displaying credentials."""
    with _provider_errors():
        rows = ProviderService.load(config).list()
        if as_json:
            typer.echo(json.dumps(rows, ensure_ascii=False))
            return
        table = Table("Provider", "Supported", "Configuration", "Default", "Auth")
        for row in rows:
            status = "configured" if row["configured"] else "not configured"
            if row["configured"] and row["auth"] == "oauth":
                status = "local credentials (not tested)"
            elif row["configured"] and row["auth"] == "aws":
                status = "AWS credential chain (not tested)"
            table.add_row(row["name"], "yes", status, "yes" if row["default"] else "—", row["auth"])
        console.print(table)


@provider_app.command("show")
def provider_show(
    provider: str | None = typer.Argument(None),
    config: Path | None = typer.Option(None, "--config", "-c"),
):
    """Show provider configuration with secrets masked."""
    with _provider_errors():
        service = ProviderService.load(config)
        provider = _provider_argument(provider, service, action="Show")
        console.print(
            json.dumps(service.show(provider), indent=2), markup=False
        )


@provider_app.command("configure")
def provider_configure(
    provider: str | None = typer.Argument(None),
    api_key_stdin: bool = typer.Option(
        False, "--api-key-stdin", help="Read the API key from stdin"
    ),
    api_key_env: str | None = typer.Option(
        None, "--api-key-env", help="Environment variable containing the API key"
    ),
    api_key_file: Path | None = typer.Option(
        None, "--api-key-file", help="Mounted secret file containing the API key"
    ),
    api_base: str | None = typer.Option(None, "--api-base"),
    model: str | None = typer.Option(None, "--model", help="Default model for this provider"),
    headers_file: Path | None = typer.Option(
        None, "--headers-file", help="Secret JSON file containing extra headers"
    ),
    config: Path | None = typer.Option(None, "--config", "-c"),
):
    """Configure a registered provider; keys are never command-line arguments."""
    with _provider_errors():
        interactive = model_picker._interactive() and not api_key_stdin
        service = ProviderService.load(config)
        if not provider:
            if not interactive:
                raise ProviderError(
                    "A provider name is required outside a TTY. Run 'paos provider list'."
                )
            provider = model_picker._select_provider(service)
        spec = provider_spec(provider)
        # Load stored settings separately so ambient secrets for unrelated providers
        # cannot accidentally be copied into config.json.
        stored = load_config(config, strict=True)
        candidate = getattr(stored.providers, spec.name).model_copy(deep=True)
        candidate.enabled = True
        sources = sum((api_key_stdin, api_key_env is not None, api_key_file is not None))
        if sources > 1:
            raise ProviderError(
                "Choose only one API key source: stdin, environment or secret file."
            )
        if sources and spec.is_oauth:
            raise ProviderError("This provider uses OAuth. Run 'paos provider login <provider>'.")
        if sources and spec.uses_aws_credentials:
            raise ProviderError("This provider uses the AWS credential chain; configure AWS credentials separately.")
        if api_key_stdin:
            key = sys.stdin.read().strip()
        elif api_key_env:
            key = os.environ.get(api_key_env, "").strip()
        elif api_key_file:
            key = api_key_file.read_text(encoding="utf-8").strip()
        else:
            key = candidate.api_key or getattr(service.config.providers, spec.name).api_key
        if sources and not key:
            raise ProviderError("The selected API key source is empty.")
        candidate.api_key = key
        if api_base is not None:
            candidate.api_base = api_base or None
        if model is not None:
            candidate.default_model = model
        if headers_file:
            headers = json.loads(headers_file.read_text(encoding="utf-8"))
            if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
            ):
                raise ProviderError("Headers file must contain a JSON object with string values.")
            candidate.extra_headers = headers
        if interactive:
            console.print(f"Configure {spec.label}", markup=False)
            if not (spec.is_oauth or spec.uses_aws_credentials) and not sources:
                candidate.api_key = _prompt("API Key", default=key, secret=True)
            candidate.api_base = (
                _prompt(
                    "API Base",
                    default=candidate.api_base or spec.default_api_base,
                )
                or None
            )
            if not headers_file:
                header_json = _prompt(
                    "Extra headers as JSON (hidden; blank keeps existing)", secret=True
                )
                if header_json:
                    candidate.extra_headers = ProviderConfig(
                        extra_headers=json.loads(header_json)
                    ).extra_headers
            test_now = _prompt(
                "Test connection and fetch models now? [Y/n]", default="Y"
            ).strip().lower() in {"y", "yes"}
            available = []
            if test_now:
                from PhyAgentOS.providers.discovery import ModelDiscoveryUnavailableError

                staged = stored.model_copy(deep=True)
                setattr(staged.providers, spec.name, candidate)
                console.print("Fetching models...")
                try:
                    available = asyncio.run(ProviderService(staged).discover_models(spec.name))
                except ModelDiscoveryUnavailableError as exc:
                    console.print(str(exc), markup=False)
                else:
                    console.print(
                        f"Model-list request successful: {len(available)} routable models. "
                        "Chat availability has not been tested."
                    )
                    if not available:
                        console.print("No routable models returned; select a saved model or enter one manually.")
            if available:
                additions = model_picker._select_models(
                    available,
                    selected=[*candidate.models, candidate.default_model] if candidate.default_model else candidate.models,
                    multiple=True,
                )
                candidate.models = list(dict.fromkeys([*candidate.models, *additions]))
                choices = list(dict.fromkeys([
                    *candidate.models, *([candidate.default_model] if candidate.default_model else []),
                ]))
                candidate.default_model = (
                    model_picker._select_models(choices, selected=[candidate.default_model] if candidate.default_model else [additions[0]])[0]
                    if len(choices) > 1 else choices[0]
                )
            elif model is None:
                choices = list(dict.fromkeys([
                    *candidate.models, *([candidate.default_model] if candidate.default_model else []),
                ]))
                if choices:
                    candidate.default_model = model_picker._select_models(
                        choices, selected=[candidate.default_model] if candidate.default_model else None,
                    )[0]
                else:
                    candidate.default_model = _prompt(
                        "Model ID (manual)",
                        default=stored.agents.defaults.model if service.routes(spec, stored.agents.defaults.model) else "",
                    )
        if not candidate.default_model:
            raise ProviderError("A model is required. Supply --model <model-id>.")
        staged = stored.model_copy(deep=True)
        setattr(staged.providers, spec.name, candidate)
        staged_service = ProviderService(staged)
        # Validate before saving; cancellation or a failed test leaves disk untouched.
        staged_service.selection(spec.name, candidate.default_model, "none")
        ProviderService.update(config, spec.name, candidate)
        console.print(
            f"Configured {spec.name}. Use 'paos provider use {spec.name}' to make it the default."
        )


@provider_app.command("test")
def provider_test(
    provider: str | None = typer.Argument(None),
    model: str | None = typer.Option(None, "--model"),
    config: Path | None = typer.Option(None, "--config", "-c"),
):
    """Send a minimal request and report safe connection diagnostics."""
    with _provider_errors():
        service = ProviderService.load(config)
        provider = _provider_argument(provider, service, action="Test")
        console.print(asyncio.run(service.test(provider, model)))


@provider_app.command("remove")
def provider_remove(
    provider: str | None = typer.Argument(None),
    config: Path | None = typer.Option(None, "--config", "-c"),
):
    """Clear this provider's stored credentials and endpoint, keeping its definition."""
    with _provider_errors():
        provider = _provider_argument(provider, ProviderService.load(config), action="Remove")
        spec = provider_spec(provider)
        ProviderService.remove(config, spec.name)
        console.print(
            "Provider settings cleared and removal saved. Reconfigure to enable it again. "
            "If it was the default, run 'paos provider use' to choose a new default. "
            "Environment and shared OAuth credentials are unchanged."
        )


@provider_app.command("use")
def provider_use(
    provider: str | None = typer.Argument(None),
    model: str | None = typer.Option(None, "--model"),
    reasoning_effort: str | None = typer.Option(
        None, "--reasoning-effort", help="Save a supported reasoning effort; none clears the saved override",
    ),
    config: Path | None = typer.Option(None, "--config", "-c"),
):
    """Set the default provider/model for future processes."""
    with _provider_errors():
        service = ProviderService.load(config)
        provider = _provider_argument(provider, service, action="Use")
        if model is None and model_picker._interactive():
            choices = service.models(provider)
            if choices:
                cfg = getattr(service.config.providers, provider_spec(provider).name)
                model = model_picker._select_models(
                    choices, selected=[cfg.default_model] if cfg.default_model else None,
                )[0]
        runtime = ProviderService.use(config, provider, model, reasoning_effort)
        console.print(
            f"Default: {runtime.name} / {runtime.model}, effort={runtime.effort or 'none'}. "
            "Running processes are unchanged."
        )


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str | None = typer.Argument(None, help="OAuth provider; omit to select interactively"),
):
    """Authenticate with an OAuth provider."""
    from PhyAgentOS.config.schema import Config
    from PhyAgentOS.providers.registry import PROVIDERS

    with _provider_errors():
        key = _provider_argument(provider, ProviderService(Config()), action="Login")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


def register_provider_commands(app) -> None:
    app.add_typer(provider_app, name="provider")
