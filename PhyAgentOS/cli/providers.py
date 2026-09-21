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

from PhyAgentOS.config.loader import load_config
from PhyAgentOS.config.schema import ProviderConfig
from PhyAgentOS.providers.service import (
    ProviderError,
    ProviderService,
    SessionRuntimes,
    provider_spec,
)

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
    except (ValueError, OSError):
        console.print(
            "Cannot read or save provider configuration; check format and file permissions."
        )
        raise typer.Exit(1) from None


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _select_provider(service: ProviderService, *, action: str = "Configure") -> str:
    """Keyboard-only, inline selection; never uses the terminal alternate screen."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    rows = service.list()
    if action in {"Test", "Use"}:
        rows = [row for row in rows if row["configured"]]
    elif action in {"Show", "Remove"}:
        rows = [
            row for row in rows
            if row["configured"]
            or getattr(service.config.providers, row["name"]).model_dump(
                exclude_defaults=True, exclude={"enabled"},
            )
        ]
    elif action == "Login":
        rows = [row for row in rows if row["auth"] == "oauth"]
    if not rows:
        raise ProviderError("No providers available. Run 'paos provider configure' first.")
    selected = next((i for i, row in enumerate(rows) if row["default"]), 0)
    bindings = KeyBindings()

    @bindings.add("up")
    def previous(event):
        nonlocal selected
        selected = (selected - 1) % len(rows)

    @bindings.add("down")
    def following(event):
        nonlocal selected
        selected = (selected + 1) % len(rows)

    @bindings.add("enter")
    def choose(event):
        event.app.exit(result=rows[selected]["name"])

    @bindings.add("escape")
    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    def render():
        lines = [f"Select a provider to {action.lower()}\n\n"]
        for index, row in enumerate(rows):
            status = "configured" if row["configured"] else "not configured"
            if row["auth"] == "oauth" and row["configured"]:
                status = "OAuth logged in (local credentials; not tested)"
            if row["default"]:
                status += " · default"
            lines.append(f"  {'●' if index == selected else '○'} {row['label']}  {status}\n")
        lines.append(f"\n↑/↓ Select   Enter {action}   Esc / Ctrl+C Cancel\n")
        return [("", "".join(lines))]

    return Application(
        layout=Layout(Window(FormattedTextControl(render))),
        key_bindings=bindings,
        full_screen=False,
        mouse_support=False,
    ).run()


def _provider_argument(provider: str | None, service: ProviderService, *, action: str) -> str:
    if provider:
        return provider_spec(provider).name
    if not _interactive():
        raise ProviderError("A provider name is required outside a TTY. Run 'paos provider list'.")
    return _select_provider(service, action=action)


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


def _model_picker(
    models: list[str], *, selected: list[str] | None = None, multiple: bool = False,
    title: str | None = None,
):
    """Inline model picker with a bounded viewport, also usable for a single default."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    if not models:
        raise ProviderError("No saved models. Run 'paos provider configure <provider>' first.")
    checked = set(selected or []).intersection(models)
    cursor = next((i for i, model in enumerate(models) if model in checked), 0)
    bindings = KeyBindings()

    @bindings.add("up")
    def previous(event):
        nonlocal cursor
        cursor = (cursor - 1) % len(models)

    @bindings.add("down")
    def following(event):
        nonlocal cursor
        cursor = (cursor + 1) % len(models)

    @bindings.add(" ")
    def toggle(event):
        if multiple:
            model = models[cursor]
            if model in checked:
                checked.remove(model)
            else:
                checked.add(model)

    @bindings.add("enter")
    def choose(event):
        if multiple and checked:
            event.app.exit(result=[model for model in models if model in checked])
        elif not multiple:
            event.app.exit(result=[models[cursor]])

    @bindings.add("escape")
    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    def render():
        heading = title or ("Select models to add" if multiple else "Select a model")
        start = max(0, min(cursor - 5, len(models) - 12))
        lines = [f"{heading} ({cursor + 1}/{len(models)})\n\n"]
        for index in range(start, min(start + 12, len(models))):
            marker = "[x]" if models[index] in checked else "[ ]"
            lines.append(f"  {'>' if index == cursor else ' '} {marker if multiple else ''} {models[index]}\n")
        action = f"Space Toggle ({len(checked)} selected; at least one required)   " if multiple else ""
        lines.append(f"\n↑/↓ Select   {action}Enter Confirm   Esc / Ctrl+C Cancel\n")
        return [("", "".join(lines))]

    return Application(
        layout=Layout(Window(FormattedTextControl(render))),
        key_bindings=bindings,
        full_screen=False,
        mouse_support=False,
    )


def _select_models(
    models: list[str], *, selected: list[str] | None = None, multiple: bool = False,
) -> list[str]:
    return _model_picker(models, selected=selected, multiple=multiple).run()


async def _switch_session_model(sessions: SessionRuntimes, key: str) -> str:
    """Choose and atomically switch a saved provider/model from the active chat loop."""
    current = sessions.get(key)
    choices: dict[str, tuple[str, str]] = {}
    for row in sessions.service.list():
        if not row["configured"]:
            continue
        models = sessions.service.models(row["name"])
        if row["name"] == current.name and current.model not in models:
            models.append(current.model)
        for model in models:
            choices[f"{row['label']} / {model}"] = (row["name"], model)
    selected = [label for label, value in choices.items() if value == (current.name, current.model)]
    chosen = await _model_picker(list(choices), selected=selected).run_async()
    name, model = choices[chosen[0]]
    return sessions.select(key, name, model)


async def _interactive_model_command(
    command: str, sessions: SessionRuntimes | None, key: str,
) -> str | None:
    parts = command.strip().lower().split(maxsplit=1)
    if not parts or not _interactive() or sessions is None:
        return None
    if parts[0] == "/effort":
        from PhyAgentOS.providers.effort import effort_error, supported_efforts

        current = sessions.get(key)
        try:
            argument = parts[1] if len(parts) > 1 else ""
            if not argument:
                levels = supported_efforts(provider_spec(current.name), current.model)
                if not levels:
                    raise ProviderError(effort_error())
                chosen = await _model_picker(
                    ["none", *levels],
                    selected=[current.effort or "none"],
                    title="Select reasoning effort (none = model default)",
                ).run_async()
                argument = chosen[0]
            return sessions.command(key, f"/effort {argument}")
        except ProviderError as exc:
            return str(exc)
        except (KeyboardInterrupt, EOFError, typer.Abort):
            return "Effort selection cancelled; current effort unchanged."
    if command.strip().lower() != "/model":
        return None
    try:
        return await _switch_session_model(sessions, key)
    except (KeyboardInterrupt, EOFError, typer.Abort):
        return "Model selection cancelled; current model unchanged."
    except ProviderError as exc:
        return str(exc)


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
        interactive = _interactive() and not api_key_stdin
        service = ProviderService.load(config)
        if not provider:
            if not interactive:
                raise ProviderError(
                    "A provider name is required outside a TTY. Run 'paos provider list'."
                )
            provider = _select_provider(service)
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
            if not spec.is_oauth and not sources:
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
                additions = _select_models(
                    available,
                    selected=[*candidate.models, candidate.default_model] if candidate.default_model else candidate.models,
                    multiple=True,
                )
                candidate.models = list(dict.fromkeys([*candidate.models, *additions]))
                choices = list(dict.fromkeys([
                    *candidate.models, *([candidate.default_model] if candidate.default_model else []),
                ]))
                candidate.default_model = (
                    _select_models(choices, selected=[candidate.default_model] if candidate.default_model else [additions[0]])[0]
                    if len(choices) > 1 else choices[0]
                )
            elif model is None:
                choices = list(dict.fromkeys([
                    *candidate.models, *([candidate.default_model] if candidate.default_model else []),
                ]))
                if choices:
                    candidate.default_model = _select_models(
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
    config: Path | None = typer.Option(None, "--config", "-c"),
):
    """Set the default provider/model for future processes."""
    with _provider_errors():
        service = ProviderService.load(config)
        provider = _provider_argument(provider, service, action="Use")
        if model is None and _interactive():
            choices = service.models(provider)
            if choices:
                cfg = getattr(service.config.providers, provider_spec(provider).name)
                model = _select_models(
                    choices, selected=[cfg.default_model] if cfg.default_model else None,
                )[0]
        runtime = ProviderService.use(config, provider, model)
        console.print(
            f"Default: {runtime.name} / {runtime.model}. Running processes are unchanged."
        )
