"""Terminal selectors for providers, models, and reasoning effort."""

from __future__ import annotations

import sys

import typer

from PhyAgentOS.providers.service import (
    ProviderError,
    ProviderService,
    SessionRuntimes,
    provider_spec,
)


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
            elif row["auth"] == "aws" and row["configured"]:
                status = "AWS credential chain (not tested)"
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
            current = "" if multiple or models[index] not in checked else " (current)"
            lines.append(
                f"  {'>' if index == cursor else ' '} "
                f"{marker if multiple else ''} {models[index]}{current}\n"
            )
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
    chosen = await _model_picker(
        list(choices),
        selected=selected,
        title=f"Select a model (current: {current.name}/{current.model})",
    ).run_async()
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
                    title=(
                        "Select reasoning effort "
                        f"(current: {current.effort or 'none'}; none = model default)"
                    ),
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
