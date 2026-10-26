# Contributing to PhyAgentOS

[English](CONTRIBUTING.md) | [简体中文](CONTRIBUTING_zh.md)

We welcome bug reports, documentation improvements and contributions to physical execution capabilities.
Read the development manuals before choosing an implementation approach. We recommend that developers
focus primarily on **TrackB: the Forge/Dora execution framework, Nodes and Skills**.
This is the recommended starting point for extending robot and environment support; Core fixes and
improvements are also welcome.

## Current availability

The resource platform does **not yet support external contributor submissions or external changes
to existing registered repositories**. These workflows will become available only after the approval
service is completed and opened for use. Current resource registration and publication are
administrator-managed; they are not a public contributor submission route.

For now, use this guide to prepare and validate development locally. References to platform
submission, review and publication in the development manuals describe the workflow to follow once
it becomes available, not an external service that is open today. GitHub source review follows each
repository's maintainer rules separately; a fork or PR does not grant platform approval, repository
write access or resource-publication rights.

## Start with the development manuals

| Goal | Read first |
| --- | --- |
| Understand TrackB architecture and repository responsibilities | [TrackB framework guide](docs/user_development_guide/track_b/README.md) and [repository catalog](docs/user_development_guide/track_b/REPOSITORIES.md) |
| Understand messages, units and Node interfaces | [Public protocols and important Node types](docs/user_development_guide/track_b/PROTOCOLS.md) |
| Understand Skill, Tool, Endpoint and Gateway relationships | [Runtime guide](docs/user_development_guide/track_b/RUNTIME.md) |
| Choose an extension mode | [Skill development manual](docs/user_development_guide/track_b/SKILL_DEVELOPMENT.md) |
| Adapt a simulated robot using existing Nodes | [Piper X MuJoCo demo](docs/user_development_guide/PIPER_X_DEMO.md) |
| Reference Bundle fields and Tool integration | [Bundle and Tool integration reference](docs/user_development_guide/README.md) |
| Change and test Core | [Core developer manual](docs/en/03-developer-manual.md) |

## Recommended contribution areas

- **Robot and environment support:** robot models, joint/frame mappings, simulation scenes and deployment profiles.
- **Reusable Nodes:** device and sensor Adapters, control/planning/perception Operators, and compatible ToolEndpoint providers.
- **Skills:** assemble compatible Nodes, expose documented Tools, and provide reproducible workflows and examples.
- **Integration quality:** protocol consistency, error handling, focused tests, troubleshooting and bilingual documentation.

Choose the smallest extension mode that supports the capability:

1. **Configure an existing profile** when compatible Nodes already support the required behavior.
2. **Add a profile** when another robot or scene can reuse compatible Nodes.
3. **Develop Nodes and assemble a Skill** when new device protocols, algorithms or interfaces are needed.

The [Skill development manual](docs/user_development_guide/track_b/SKILL_DEVELOPMENT.md) explains the difficulty,
required skills and steps for each mode. Use the [Piper X demo](docs/user_development_guide/PIPER_X_DEMO.md)
as a worked example, checking its version and model assumptions before adapting it.

## Prepare development and future submissions

1. Reproduce the baseline and record compatible Core, Forge, Dora, Skill and Node versions as relevant.
   For a new interface or a change spanning repositories, open an
   [issue](https://github.com/PhyAgentOS/PhyAgentOS-core/issues) to discuss the scope first.
2. Use the repository catalog to locate the owning source repository. Node changes belong to the
   corresponding Node repository; Core changes and Core documentation belong here. Keep changes focused.
3. Work in a local copy or a personal fork. If the owning repository's maintainers accept source PRs,
   open one from your fork against the agreed target branch. This source-review process is separate
   from the external platform submission and repository-change workflows that are not yet open.
4. Run checks appropriate to the change. For robot integration, verify model/joint/frame consistency,
   startup, representative native Tool calls, failure handling and shutdown. Validate in simulation
   before attempting hardware execution. Explain the resulting behavior and any validation limits.
5. Update documentation alongside configuration or API changes. Keep repository `README.md` in English
   and `README_zh.md` in Chinese, with reciprocal links at the top. For Node repositories, document
   Node count and entry points, Tool support, Endpoint IDs/operations and Tool API mappings; state
   explicitly when a capability is not provided. See the
   [Node README checklist](docs/user_development_guide/track_b/SKILL_DEVELOPMENT.md).
6. Preserve model/source licenses and immutable resource versions. Source changes that alter a Node
   artifact require a new version and matching Skill locks. Local installation, platform registration
   and publication are separate steps. External contributors must wait for the approval service and
   these workflows to open before submitting resources or requesting changes to existing registered
   repositories; then follow the platform's published instructions.

Do not include credentials, private environment configuration or unrelated local logs in a contribution.
