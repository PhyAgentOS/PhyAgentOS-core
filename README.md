<div align="center">
  <img src="docs/imgs/logo_en.png" alt="PhyAgentOS-G" width="700">

  <h1>PhyAgentOS-G</h1>

  <h3>General Game Agent — A Decoupled Research Framework for Embodied Intelligence Behavior</h3>

  <p>
    <a href="./README.md">English</a> | <a href="./README_zh.md">中文</a>
  </p>

  <p>
    <img src="https://img.shields.io/badge/version-v0.0.5-blue" alt="Version">
    <img src="https://img.shields.io/badge/python-≥3.11-3776AB?logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-3DA639" alt="License">
    <a href="https://github.com/PhyAgentOS/PhyAgentOS">
      <img src="https://img.shields.io/badge/PRs-Welcome-2EA44F" alt="PRs">
    </a>
    <a href="https://github.com/PhyAgentOS/PhyAgentOS/stargazers">
      <img src="https://img.shields.io/github/stars/PhyAgentOS/PhyAgentOS?style=social" alt="Stars">
    </a>
  </p>
  <br>
  <p>
    <sub>Migrated from main branch v0.1.4 · Focused on game agent behavior research · Simulation & real-robot validation</sub>
  </p>
</div>

---

## 📢 Changelog

| Version | Date | Update |
|:------|:-----|:-------|
| ![v0.0.5](https://img.shields.io/badge/v0.0.5-47A882) | 2026-06-12 | Hierarchical memory system (Hermes mechanism): LESSONS.md filtering, MEMORY.md refinement, auto-skill from experience |
| ![v0.0.4](https://img.shields.io/badge/v0.0.4-47A882) | 2026-06-12 | Self-evolution with self-reflection: complete complex tasks in unknown scenarios and summarize experience |
| ![v0.0.3](https://img.shields.io/badge/v0.0.3-11648A) | 2026-06-11 | Integrated as Agent Loop, supporting complex task completion |
| ![v0.0.2](https://img.shields.io/badge/v0.0.2-11648A) | 2026-05-29 | Minecraft pipeline optimization: issue commands from terminal and in-game chat |
| ![v0.0.1](https://img.shields.io/badge/v0.0.1-11648A) | 2026-05-29 | Minecraft ready: cloud Agent connects to user's local Minecraft server |

> **v0.0.3 note**: PhyAgentOS-G is rebuilt from [PhyAgentOS main branch v0.1.4](https://github.com/PhyAgentOS/PhyAgentOS). The Session-Centered Runtime core is retained, and the project is now focused on game agent behavior research. Versioning starts from 0.0.x to track the Game Agent branch independently.

> **v0.0.5 new features**: Hierarchical 3-tier memory system (inspired by Hermes). Episodic tier — LESSONS.md ≤25 entries rule-based filtering. Semantic tier — MEMORY.md ≤4000 chars hard limit triggers LLM compaction. Methodological tier — ≥2 successful verifications trigger agent to auto-create reusable skills via skill-creator. Full 9-step Reflection loop: Plan → Wait → Check → Reflect → Learn → Retry → Escalate → Abstract → Convert to Skill.

---

## 🤔 Why PhyAgentOS-G?

By moving embodied intelligence learning and validation into game environments, we explore core intelligent behavior capabilities at minimal cost — long-term decision-making, spatial reasoning, task planning — then transfer proven strategies to simulation and real-robot environments.

<table>
<tr><td width="32">🎮</td><td width="165"><b>Low-Cost Validation</b></td><td>Game environments naturally provide complex interactions, long-term memory, and open worlds — iterate Agent capabilities without hardware cost.</td></tr>
<tr><td>🔄</td><td><b>Game→Sim→Real Transfer</b></td><td>The same Session protocol runs identically across game, simulation, and real_robot targets — zero-friction migration.</td></tr>
<tr><td>📋</td><td><b>Fully Auditable</b></td><td>State, actions, and perception results are written to Markdown + YAML files; every step is traceable and reproducible.</td></tr>
<tr><td>🧩</td><td><b>Three-Way Decoupling</b></td><td>RolloutTarget + SkillRuntime + TargetAdapter separation — add a new game or hardware in ~100 lines of code.</td></tr>
</table>

<br>

<div align="center">
  <img src="docs/imgs/framework.png" alt="Architecture" width="960">
  <p><sub>▲ Session-Centered Runtime Architecture Overview</sub></p>
</div>

---

## ✨ Core Features

<table>
<tr>
  <td width="32">🔄</td>
  <td width="165"><b>Session-Centered Runtime</b></td>
  <td><code>WatchdogSupervisor</code> → <code>SessionRunner</code> → <code>SkillRuntime</code> → <code>TargetSessionHandle</code> execution pipeline, with Session as the scheduling unit</td>
</tr>
<tr>
  <td>🎯</td>
  <td><b>Target-Configured</b></td>
  <td>Four target kinds — <code>game</code> / <code>debug</code> / <code>simulation</code> / <code>real_robot</code> — registered in <code>TARGETS.md</code>, adapters attached on demand</td>
</tr>
<tr>
  <td>🧩</td>
  <td><b>Adapter + Bridge</b></td>
  <td><code>TargetAdapter</code> + <code>PolicyAdapter</code> + <code>ActionBridge</code> three-way decoupling with explicit observation/action contracts; <code>AdapterPlan</code> auto-composed</td>
</tr>
<tr>
  <td>⚡</td>
  <td><b>Dual Skill Runtimes</b></td>
  <td><code>PolicySkillRuntime</code> maintains policy closed-loop + <code>BuiltinSkillRuntime</code> manages Agent interactive loop</td>
</tr>
<tr>
  <td>🛡️</td>
  <td><b>Strict Preflight</b></td>
  <td>Runtime validation checks (target / sensor / perception / adapter contract / action contract / tool); failures are <code>rejected</code></td>
</tr>
<tr>
  <td>📝</td>
  <td><b>File Protocol Matrix</b></td>
  <td><code>TARGETS.md</code> · <code>SKILLRUNTIME.md</code> · <code>SESSIONS.md</code> · <code>ENVIRONMENT.md</code> · <code>LESSONS.md</code> + external YAML configs</td>
</tr>
<tr>
  <td>🔐</td>
  <td><b>Multi-Layer Safety</b></td>
  <td>Critic validation → Preflight contract checks → Target-side SafetyGuard</td>
</tr>
  <tr>
    <td>🎮</td>
    <td><b>Game Agent CLI</b></td>
    <td><code>paos minecraft</code> direct control of Minecraft bot, 16 action types supported</td>
  </tr>
  <tr>
    <td>🧠</td>
    <td><b>3-Tier Hierarchical Memory</b></td>
    <td>Episodic tier LESSONS.md ≤25 filtering + Semantic tier MEMORY.md ≤4000 chars LLM compaction + Methodological tier skills/ auto-deposition</td>
  </tr>
  <tr>
    <td>🔄</td>
    <td><b>9-Step Reflection Loop</b></td>
    <td>Plan→Wait→Check→Reflect→Learn→Retry→Escalate→Abstract→Convert to Skill, experience auto-converted to reusable agent skills</td>
  </tr>
</table>

---

### Independent game-agent workflows

The [game_agents directory](PhyAgentOS/game_agents/README.md) contains separate implementations:

| Module | Entry | Purpose |
|:-------|:------|:--------|
| [Stardew](PhyAgentOS/game_agents/stardew/README.md) | `paos general-game` | Bounded Planner–Actor sessions using Core targets and observed completion checks. |
| [Minecraft](PhyAgentOS/game_agents/minecraft/README.md) | `paos minecraft warmup` / `benchmark` | Fixed warm-up, evidence-backed Skill Graph and synchronous benchmark accumulation. |

Each workflow retains its own execution and memory policy and reuses the relevant Core interfaces.

## 🚀 5-Minute Quick Start

<table>
<tr>
<td width="28" align="center">1</td>
<td>

**Install**

```bash
git clone https://github.com/PhyAgentOS/PhyAgentOS.git && cd PhyAgentOS
pip install -e .            # Python ≥ 3.11
```
</td>
</tr>
<tr>
<td align="center">2</td>
<td>

**Initialize Workspace**

```bash
paos onboard
```
</td>
</tr>
<tr>
<td align="center">3</td>
<td>

**Start Agent**

```bash
paos agent --workspace workspaces/minecraft
```
</td>
</tr>
<tr>
<td align="center">4</td>
<td>

**Run First Task**

```bash
paos agent "Come to me"
```
</td>
</tr>
<tr>
<td align="center">5</td>
<td>

**Optional: Agent Perspective Observation**

```bash
...
```

**Optional: Simulation Validation**

```bash
...
```

**Optional: Real-Robot Validation**

```bash
...
```

</td>
</tr>
</table>

---

## 🗂️ Protocol Files

| Context Loading | File | Owner | Purpose |
|:--|:--|:--|:--|
| Always loaded into the agent system prompt | `AGENTS.md` / `SOUL.md` / `USER.md` | Agent workspace | Agent operating rules, identity, user preferences |
| Always loaded into the agent system prompt | `TOOLS.md` | Agent workspace | Tool usage policy and available tool guidance |
| Always loaded into the agent system prompt | `SKILLS.md` | Agent workspace | Agent-facing skill discovery and loading rules |
| Always loaded into the agent system prompt | `memory/MEMORY.md` | Agent workspace | Long-term memory; Agent writes abstract principles; ≤4000 chars hard limit triggers LLM compaction |
| Loaded when present; filtered by enabled runtime targets | `EMBODIED.md` | Agent workspace | Human-readable target capability descriptions |
| Loaded when present as state | `ENVIRONMENT.md` | Agent/runtime workspace | Current target, scene, and environment state |
| Loaded when present, ≤25 entries after filtering | `LESSONS.md` | Agent workspace | YAML-format operational lessons; dedup + recent 15 + succeeded entries prioritized |
| Runtime protocol; read before scheduling sessions | `TARGETS.md` | Runtime workspace | Enabled targets, endpoint/adapter/config, supported skill runtimes |
| Runtime protocol; read before scheduling sessions | `SKILLRUNTIME.md` | Runtime workspace | Policy/builtin skill runtime registry and execution contracts |
| Runtime queue/state; written by Agent and watchdog | `SESSIONS.md` | Runtime workspace | Pending/running/completed sessions and results |
| Written by watchdog (not injected into prompt) | `memory/HISTORY.md` | Agent workspace | Session completion timeline + MEMORY refinement events; Agent grep-on-demand |

`SKILLS.md` is for Agent capabilities and skill discovery. `SKILLRUNTIME.md` is for runtime execution contracts. `memory/MEMORY.md` + `HISTORY.md` form the long-term memory system, `skills/<name>/SKILL.md` provides reusable methodological guidance.

---

## 📦 Project Structure

```
PhyAgentOS-G/
│
├── PhyAgentOS/agent/          # Track A — Agent Brain
│   ├── loop.py                #   Main agent loop
│   ├── context.py             #   Context window builder
│   ├── memory.py              #   Short/long-term memory system
│   ├── skills.py              #   Skill loading and execution
│   └── tools/                 #   Built-in tools (file, shell, web)
│
├── PhyAgentOS/runtime/        # Track B — Execution Plane
│   ├── watchdog/              #   WatchdogSupervisor · Session scheduling
│   ├── sessions/              #   SessionRunner · TargetSessionHandle
│   ├── targets/               #   RolloutTarget (game · debug · sim · real)
│   │   ├── game/              #   Minecraft game target
│   │   ├── local/             #   DummySimTarget smoke tests
│   │   ├── remote/libero/     #   LIBERO benchmark TargetWS server + proxy
│   │   ├── sim/               #   Simulation targets (in development)
│   │   └── real/              #   Real-robot targets (in development)
│   ├── skillruntime/          #   PolicySkillRuntime · BuiltinSkillRuntime
│   │   ├── policy/            #   OpenPI policy runtime (pi05)
│   │   └── game/              #   Minecraft skill runtime
│   ├── adapters/              #   TargetAdapter · PolicyAdapter · Bridge
│   │   ├── libero/            #   LIBERO target adapter
│   │   ├── openpi/            #   OpenPI policy adapters
│   │   └── minecraft/         #   Minecraft adapter
│   ├── policy/openpi/         #   OpenPI client · LeRobot pi0-family server
│   ├── perception/            #   Perception runtime · EnvironmentWriter
│   ├── preflight/             #   RuntimeCompatibilityPreflight
│   ├── schemas/               #   Pydantic Session/Contract Schema
│   └── workspace/             #   Runtime workspace lifecycle management
│
├── PhyAgentOS/cli/            # CLI entry (paos agent / onboard / minecraft)
├── PhyAgentOS/skills/         # Agent built-in skills (benchmarking, etc.)
├── PhyAgentOS/config/         # Pydantic configuration model
├── PhyAgentOS/templates/      # Workspace templates (TARGETS.md / SESSIONS.md)
│   └── configs/runtime/       # Sensor / Perception / Contract YAML
├── scripts/                   # Utility scripts (E2E acceptance, workspace init)
├── bridge/                    # TypeScript bridge layer
├── docs/                      # Documentation (Chinese/English)
│   ├── zh/                    #   Chinese docs
│   ├── en/                    #   English docs
│   └── scenarios/game/        #   Minecraft deployment & usage guides
└── pyproject.toml             # Python package config
```

---

## 🏷️ Supported Targets

| | Kind | Location | Example | Status |
|:--|:-----|:-----|:-----|:-----|
| 🎮 | `game` | Local | Minecraft | ✅ Supported |
| 🧪 | `simulation` | Remote | LIBERO benchmark + pi0.5 policy | ✅ Supported |

> All targets registered in `TARGETS.md`, identified by `target_adapter://` URI.

---

## 📖 Documentation

| Document | Audience | Description |
|:-----|:-----|:-----|
| [Framework Introduction](docs/en/01-framework-introduction.md) | Everyone | Design philosophy, architecture, progress, roadmap |
| [User Manual](docs/en/02-user-manual.md) | Users | Installation, game/simulation scenarios, troubleshooting |
| [Developer Manual](docs/en/03-developer-manual.md) | Developers | API reference, Target/Adapter/Skill development, coding style |
| [Minecraft Deployment Guide](docs/scenarios/game/minecraft/0_start.md) | Users | Windows bridge + Linux Agent complete deployment |
| [Minecraft Usage Guide](docs/scenarios/game/minecraft/1_hello.md) | Users | CLI control, action space, troubleshooting |
| [Minecraft Agent Loop](docs/scenarios/game/minecraft/2_agent_loop.md) | Developers | Agent→Watchdog execution pipeline |
| [Minecraft Self-Evolution](docs/scenarios/game/minecraft/3_self_evo.md) | Developers | 3-tier hierarchical memory + 9-step reflection loop |
| [Minecraft Benchmark and Skill Graph](docs/scenarios/game/minecraft/4_benchmark.md) | Developers | Warm-up, synchronous skill accumulation, CLI/Python API, and result layout |
| [Minecraft Local Linux Setup](docs/scenarios/game/minecraft/01_linux_start.md) | Users | Local Paper, bridge, PhyAgentOS startup, and smoke-test commands |

---

## 🤝 Contributing

PRs and Issues are welcome!

---

<div align="center">

Built on **[nanobot](https://github.com/HKUDS/nanobot)**

**PhyAgentOS-G** is the Game Agent fork of [PhyAgentOS](https://github.com/PhyAgentOS/PhyAgentOS)

Jointly developed by **Sun Yat-sen University HCP Lab** & **Peng Cheng Laboratory**

<br>

<img src="docs/imgs/SYSU.png" alt="SYSU" height="128">
&nbsp;&nbsp;&nbsp;
<img src="docs/imgs/Pengcheng.png" alt="Pengcheng" height="128">
&nbsp;&nbsp;&nbsp;
<img src="docs/imgs/HCP.jpg" alt="HCP" height="128">

<br>
<sub>MIT License · Copyright © 2025-2026 PhyAgentOS</sub>

</div>
