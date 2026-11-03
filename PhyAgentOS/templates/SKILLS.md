# Agent Skills

Skills are Markdown instructions that extend the agent's behavior.

## Locations

- Workspace skills: `skills/<skill-name>/SKILL.md`
- Built-in skills: packaged under `PhyAgentOS/skills/<skill-name>/SKILL.md`
- Workspace skills override built-in skills with the same name.

## Frontmatter

Each skill may start with YAML frontmatter:

```yaml
---
name: example-skill
description: Short user-facing summary.
metadata: {"PhyAgentOS":{"always":false,"available":true}}
---
```

The metadata key may be `PhyAgentOS` or `openclaw`; both are accepted.

## Loading Rules

- Skills with `always: true` are loaded directly into context when requirements are met.
- Other available skills appear in the skills summary. When task experience evolution is enabled,
  use `activate_skill` before executing a matching workflow; it loads the Skill and only its
  applicable scoped lessons while recording the task-to-Skill binding.
- Reading a `SKILL.md` directly does not activate it for experience attribution.
- Skills with unmet requirements are listed as unavailable.
- Dependency requirements can declare CLI binaries or environment variables under `requires`.

## Built-in Skills

- Forge execution uses the Gateway Tool API tools `forge_tool_*` only.
- Before choosing a Tool, call `forge_tool_list` to read the current Gateway catalog, then call
  `forge_tool_context(tool_id)` for the exact ToolSpec and live readiness. The list shows what the
  Gateway currently exposes; it does not expand the Skill allowlist or guarantee readiness.
- Use `forge_task_*` when Tool calls must be aggregated and semantically verified as one task.
- A robot-specific Skill must discover live ToolSpec/context and must not construct Gateway HTTP
  requests or use the retired `/agent/sessions` execution path.
- A workspace Skill at `skills/<skill-name>/SKILL.md` overrides the installed bundle with the same
  name. Treat that as a local override source: after a Skill upgrade, check whether the workspace
  copy is stale before following it, and update or remove the override deliberately.

## Authoring Rules

- Keep a skill focused on one capability or workflow.
- Put reusable scripts and references inside the skill directory.
- Do not duplicate or invent Forge Action Manifest entries in a skill.
