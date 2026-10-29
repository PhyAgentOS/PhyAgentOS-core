"""Agent-side capability adapters.

Capabilities live outside :mod:`PhyAgentOS.agent.core`: they translate core
tool calls into a domain service and back, while the core stays independent
of Forge and other domains.

The Forge adapter is split by responsibility: ``forge_task_tools`` owns the
five AgentTask definitions and operations, ``forge_tool_api`` owns the ten
Gateway Tool API definitions and operations, and ``forge_capability`` wires
them into Core registrations and maps their responses to ``ToolOutcome``.
This package does not import ``PhyAgentOS.agent_legacy``.
"""
