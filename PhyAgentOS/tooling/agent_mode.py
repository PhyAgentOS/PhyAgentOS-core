"""Agent-mode management tool.

This is the shared implementation moved from the legacy module. The legacy
module re-exports :class:`AgentModeTool`, and the Core adapter in
:mod:`PhyAgentOS.agent.capabilities.agent_mode_capability` delegates to the
same methods when a compatible provider manager is available.

The tool changes only the provider manager's in-memory modes. It never writes
the configuration file and never invents a provider or model that the manager
did not accept.
"""

from __future__ import annotations

from typing import Any

from PhyAgentOS.tooling.base import Tool


class AgentModeTool(Tool):
    """
    Unified tool for agent mode management.

    Supports 5 methods:
    - add: Add a new model configuration
    - remove: Remove an existing model configuration
    - update: Update an existing model configuration
    - list: List all available models
    - switch: Switch to a specific model mode
    """

    def __init__(self, provider: Any):
        """Initialize the agent mode tool with the provider manager."""
        self.provider = provider

    @property
    def name(self) -> str:
        return "agent_mode"

    @property
    def description(self) -> str:
        return (
            "Unified tool for managing agent LLM modes. "
            "Supports: add (add model), remove (remove model), update (update model), "
            "list (list all models), switch (switch to a mode)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["add", "remove", "update", "list", "switch"],
                    "description": "The method to perform: add, remove, update, list, or switch",
                },
                "mode": {
                    "type": "string",
                    "description": "Mode name (e.g., 'common', 'coding', 'multimodal'). Required for all methods except list.",
                    "default": "common",
                },
                # Additional parameters for add/update
                "model": {
                    "type": "string",
                    "description": "Model identifier (e.g., 'openai/qwen3.5:cloud'). Required for add and update methods.",
                },
                # Additional parameters for add
                "describe": {
                    "type": "string",
                    "description": (
                        "Description of the model. Required for add; update changes only "
                        "the model because that is the only operation the provider manager supports."
                    ),
                },
            },
            "required": ["method"],
        }

    async def execute(self, method: str, mode: str = "common", **kwargs: Any) -> str:
        """Execute the specified method."""
        method = method.lower()
        try:
            if method == "list":
                return await self._list_modes()
            elif method == "switch":
                return await self._switch_mode(mode)
            elif method == "add":
                return await self._add_model(
                    mode, kwargs.get("model"), kwargs.get("describe")
                )
            elif method == "remove":
                return await self._remove_model(mode)
            elif method == "update":
                # The manager's update_mode takes (mode, model_id) only;
                # ``describe`` is part of the shared schema but is not an
                # operation this manager supports changing.
                return await self._update_model(mode, kwargs.get("model"))
            else:
                return f"Error: Unknown method '{method}'. Supported methods: add, remove, update, list, switch"
        except (ValueError, KeyError, AssertionError) as exc:
            return f"Error: {exc}"

    def _modes(self) -> list[dict[str, Any]]:
        return list(self.provider.list_models())

    def _mode_exists(self, mode: str) -> bool:
        list_models = getattr(self.provider, "list_models", None)
        if not callable(list_models):
            # A manager that cannot list modes still owns the operation; let
            # add/remove/update/switch report its own result.
            return True
        return any(item.get("name") == mode for item in self._modes())

    async def _list_modes(self) -> str:
        """List all available modes."""
        models = self._modes()
        lines = [
            "## Available Models",
            "",
            "| Mode | Model | Description |",
            "|------|-------|-------------|",
        ]
        for model in models:
            name = model.get("name", "unknown")
            model_id = model.get("model", "unknown")
            desc = model.get("describe", "unknown")
            lines.append(f"| {name} | {model_id} | {desc} |")
        lines.append(f"**Default Mode**: {self.provider.get_default_mode()}")
        return "\n".join(lines)

    async def _switch_mode(self, mode: str) -> str:
        """Switch to a specific mode."""
        if not mode:
            return "Error: 'mode' parameter is required for switch method"
        if not self._mode_exists(mode):
            return f"Error: Mode '{mode}' does not exist."
        old_mode = self.provider.get_default_mode()
        self.provider.set_default_mode(mode)
        return f"✓ Mode switched: {old_mode} → {mode}"

    async def _add_model(self, mode: str, model_id: str | None, describe: str | None) -> str:
        """Add a new model configuration."""
        if not mode:
            return "Error: 'mode' parameter is required for add method"
        if not model_id:
            return "Error: 'model' parameter is required for add method"
        self.provider.add_mode(mode, model_id, describe or "")
        return f"✓ Model added: {mode}\n  Model: {model_id}\n  Description: {describe}"

    async def _remove_model(self, mode: str) -> str:
        """Remove a model configuration."""
        if not mode:
            return "Error: 'mode' parameter is required for remove method"
        if not self._mode_exists(mode):
            return f"Error: Mode '{mode}' does not exist."
        self.provider.remove_mode(mode)
        return f"✓ Model removed: {mode}"

    async def _update_model(self, mode: str, model_id: str | None) -> str:
        """Update an existing model configuration.

        The provider manager supports changing the model only; the description
        is not part of its update operation.
        """
        if not mode:
            return "Error: 'mode' parameter is required for update method"

        if not model_id:
            return "Error: 'model' parameter is required for update method"
        self.provider.update_mode(mode, model_id)
        return f"✓ Model updated: {mode}\n  Model: {model_id}"
