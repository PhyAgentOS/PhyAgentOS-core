# PhyAgentOS Documentation

[English](README.md) | [简体中文](README_zh.md)

Version: **1.1.1**<br>
Implementation baseline: **Forge Skill binding, Query/Action/Session Tool API, AgentTask recovery, and version-scoped experience, 2026-10-23**

This directory serves users, operators and developers. Source, schemas and tests define current behavior; historical design reports do not replace runtime contracts.
Start development with the [contribution guide](../CONTRIBUTING.md) → [TrackB overview](user_development_guide/track_b/README.md) → [three extension modes](user_development_guide/track_b/SKILL_DEVELOPMENT.md) → [Piper X example](user_development_guide/PIPER_X_DEMO.md).
External platform submissions and changes to existing registered repositories must wait for completion and opening of the approval service; local development and validation can proceed.

## Core manuals

1. [Framework Introduction](en/01-framework-introduction.md): positioning, control-plane boundaries, execution/evidence/verdict separation, lifecycle, and implemented scope.
2. [User Manual](en/02-user-manual.md): installation, provider and Forge configuration, task description, verification modes, artifacts, and troubleshooting.
3. [Core Developer Manual](en/03-developer-manual.md): AgentTask, Tool API client, Skill Runtime, evidence, verifier, recovery, and testing.
4. [Forge Configuration Reference](en/04-forge-configuration-reference.md): exact Forge Tool API, Resource Registry, evidence, verification, AgentTask, and embodiment fields.
5. [Agent Experience and Skill Evolution](en/05-agent-experience-and-skill-evolution.md): Skill activation and attribution, episodes, Lesson clustering, Skill promotion, persistence, and guardrails.

## Focused manuals

- [Benchmark documentation](benchmarks.md): results, time and token usage, and evaluation context.

- [Governed Interaction Foundation](interaction/README.md): opt-in contracts, task governance, background monitoring, recovery gates and fake-world acceptance.

- [Operations Manual](user_manual/README.md): preflight checklist, startup order, observation, cancellation, restart recovery, backup, and failure layers.
- [Docker Deployment Guide](user_manual/DOCKER.md): Docker-based quick deployment with a one-click script for build, init, and run (outbound-only gateway, no inbound port).
- [Bundle and Tool Integration Reference](user_development_guide/README.md): ToolSpec and manifest-v2 examples, startup hooks, Bundle packaging, the local loop and conformance checks.
- [Core Communication Architecture](user_development_guide/COMMUNICATION.md): Agent messages, Forge HTTP/WebSocket, system events, SQLite, and artifact boundaries.
- [Forge Tool API Integration Contract](forge/README.md): Query/Action/Session, immutable Skill binding, ToolInvocation, AgentTask, evidence, verification, recovery, and Skill Runtime contracts.

- [TrackB framework guide](user_development_guide/track_b/README.md).
- [Piper X development demo](user_development_guide/PIPER_X_DEMO.md).

## Suggested reading paths

| Goal | Suggested path |
|:-----|:---------------|
| Understand why execution and task success differ | [Framework Introduction](en/01-framework-introduction.md) → [Forge Integration Contract](forge/README.md) |
| Deploy Agent + Forge for the first time | [User Manual](en/02-user-manual.md) → [Configuration Reference](en/04-forge-configuration-reference.md) |
| Operate a long-running service | [Operations Manual](user_manual/README.md) → [Core Communication Architecture](user_development_guide/COMMUNICATION.md) |
| Deploy quickly with Docker | [Docker Deployment Guide](user_manual/DOCKER.md) |
| Add a new robot action in Gateway | [TrackB development manual](user_development_guide/track_b/SKILL_DEVELOPMENT.md) → [Node protocols](user_development_guide/track_b/PROTOCOLS.md) |
| Develop and package a Forge Skill; publish when available | [TrackB Skill development](user_development_guide/track_b/SKILL_DEVELOPMENT.md) → [Bundle reference](user_development_guide/README.md#5-package-publish-and-close-the-local-loop) |
| Change evidence, verification, recovery, or persistence | [Core Developer Manual](en/03-developer-manual.md) → [Forge Integration Contract](forge/README.md) |
| Use or extend task experience, Lessons, or Skill evolution | [Agent Experience and Skill Evolution](en/05-agent-experience-and-skill-evolution.md) → [Core Developer Manual](en/03-developer-manual.md) |

## Documentation ownership

- Core developer manual: modules, transactions, verification and Runtime implementation.
- Forge Tool API contract: canonical HTTP routes, identities and invocation/task semantics.
- TrackB: Node protocols, source catalog, assembly relationships and Skill development modes.
- Bundle and Tool integration reference: schemas, manifest, startup hooks, packaging and conformance.
- Core communication reference: Agent bus, observation, verification-service and storage boundaries.
- Operations and Docker manuals: deployment/runbooks; interaction remains a separate restricted extension.

## Runtime and compatibility boundaries

PAOS supports Forge Query, Action, and Session through `/tools` and `/invocations`. The Agent-side aggregate
is AgentTask, while physical execution remains owned by Gateway ToolInvocation and ToolEndpoint.
Skill Runtime manages manifest-v2 bundles and named Dora profiles; it is distinct from the removed
Markdown queue Runtime. Existing evolution, experience, verification, and Agent workspace data are
read in place. Registry downloads are explicit and digest-verified.
