# Docker Deployment Guide

> Version: 1.1.1 · [中文](DOCKER_zh.md)

PhyAgentOS ships with a Docker-based quick deployment that requires no manual Python / Node.js setup. Together with the [`scripts/install.sh`](../../scripts/install.sh) one-click script, you can build, initialize, and run in under a minute.

> Image base: `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` (CPU, a few hundred MB)

---

## 📦 What's in the Image

| Component | Description |
|:-----|:-----|
| Python 3.12 runtime | All `pyproject.toml` deps installed via `uv` |
| `paos` CLI | Registered as the entrypoint (`ENTRYPOINT`) |
| Node.js 20 | Strictly builds the WhatsApp bridge from locked npm dependencies |
| Config directory | `/root/.PhyAgentOS` (persisted to the host via a volume mount) |
| Default service | Interactive CLI (`paos agent`); switchable to a long-running gateway |

The image does **not** include Dora CLI, concrete Forge Skills or nodes, GPU / CUDA, Isaac Sim,
BEHAVIOR-1K, or other robot Runtime dependencies. The stock image runs the general Agent and its
message-bus gateway; it does not provide a managed Forge Skill Runtime. Run that Runtime from a
host-native PhyAgentOS installation following the
[user manual](../en/02-user-manual.md#dora-cli-for-managed-skill-profiles), or build a custom image
containing Dora CLI v0.4.1 with `dora-message` v0.7.0 and every prerequisite of the selected Skill
profile. Installing Dora only on the host does not make it visible inside the stock container.

> **About the gateway port**: `paos gateway` is a message-bus service (Agent + channels + Cron + Heartbeat + Forge orchestration) that makes **outbound connections only** (LLM providers, Telegram/DingTalk, etc.) and does **not** bind an inbound port. `gateway.port` in `config.json` is currently shown only in the startup log and is not bound to a socket, so the container needs **no** `-p` port mapping.

---

## ✅ Prerequisites

- **Docker 20.10+** (with the daemon running)
- Optional: **Docker Compose v2** (if you use `docker-compose.yml`)

Check your environment:

```bash
docker --version
docker info   # confirm the daemon is up
```

---

## 🚀 One-click install & run

> The `./scripts/install.sh` commands below are run from the **repository root**.

Minimal flow, two commands:

```bash
# 1. Build the image + write the default config (first run)
./scripts/install.sh

# 2. Edit the config and add your LLM API key
#    macOS / Linux:  vi ~/.PhyAgentOS/config.json
#    Put it under providers.<name>.apiKey

# 3. Enter the interactive CLI
./scripts/install.sh chat
```

### Full command list

| Command | Purpose |
|:-----|:-----|
| `./scripts/install.sh` | (default) build image + initialize config |
| `./scripts/install.sh build` | build the image only |
| `./scripts/install.sh onboard` | write the default config to `~/.PhyAgentOS/config.json` only |
| `./scripts/install.sh chat` | interactive CLI (default service) |
| `./scripts/install.sh gateway` | long-running gateway (message bus, outbound only, no inbound port) |
| `./scripts/install.sh status` | show PhyAgentOS status |
| `./scripts/install.sh stop` | stop the gateway container |
| `./scripts/install.sh logs` | tail gateway logs |
| `./scripts/install.sh help` | show help |

---

## ⚙️ Configuration & environment

### Data directory

All config, workspace, and session history live on the host under `~/.PhyAgentOS`, injected into the container via a volume mount (`-v`):

```
Host ~/.PhyAgentOS  ⟷  Container /root/.PhyAgentOS
```

So **removing the image does not lose data**; re-running the script restores everything.

### Configurable environment variables

| Variable | Default | Description |
|:-----|:-------|:-----|
| `PAOS_DATA_DIR` | `~/.PhyAgentOS` | Config & workspace directory |

Example: start the gateway with a custom data directory.

```bash
PAOS_DATA_DIR=/data/paos ./scripts/install.sh gateway
```

### API key configuration

Both the interactive CLI and the gateway need an LLM provider API key. The install script writes a default config template during `onboard`; edit the `providers` section:

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "YOUR_API_KEY"
    }
  }
}
```

See `paos provider login` for supported providers and OAuth flows.

---

## 🧩 Using Docker Compose

The repo ships a [`docker-compose.yml`](../../docker-compose.yml) for scenarios that need a persistent service.

```bash
# Start the gateway (background, auto-restart)
docker compose up -d phyagentos-gateway

# View logs
docker compose logs -f phyagentos-gateway

# Enter the interactive CLI (separate profile)
docker compose run --rm phyagentos-cli
```

---

## 🔧 Using Docker directly (without the script)

For full manual control (`docker build` must be run from the **repository root**):

```bash
# Build
docker build -t phyagentos:latest .

# Initialize config (first run)
docker run --rm -v ~/.PhyAgentOS:/root/.PhyAgentOS phyagentos:latest onboard

# Interactive CLI
docker run --rm -it -v ~/.PhyAgentOS:/root/.PhyAgentOS phyagentos:latest agent

# Long-running gateway (message bus, outbound only, no port mapping needed)
docker run -d --name phyagentos-gateway \
  -v ~/.PhyAgentOS:/root/.PhyAgentOS \
  phyagentos:latest gateway
```

---

## ⚠️ Known limitations

### 1. Managed Forge Skill Runtime is not included

The stock image has no Dora CLI or concrete Forge Runtime artifacts. `paos skill start` therefore
is not supported in this image without an explicitly extended image containing Dora CLI v0.4.1,
`dora-message` v0.7.0, and the selected Skill's platform dependencies. Agent-only and
message-channel use does not require Dora.

### 2. Container runs as root

To stay consistent with the `~/.PhyAgentOS:/root/.PhyAgentOS` volume-mount convention, the image runs as root by default. For production hardening, consider adding a non-root user and a healthcheck later.

### 3. No GPU support

This is a CPU image and does not support Isaac Sim / BEHAVIOR-1K or other CUDA-based simulation. For GPU, switch the base image to `nvidia/cuda` and run with `--gpus all`.

---

## 🧯 Troubleshooting

| Symptom | Solution |
|:-----|:---------|
| `docker: command not found` | Install Docker: <https://docs.docker.com/get-docker> |
| `Cannot connect to the Docker daemon` | Start Docker Desktop or `sudo systemctl start docker` |
| Build OOM / slow | Increase Docker memory; first build downloads deps, later builds use cache |
| `paos agent` reports `No API key configured` | Edit `~/.PhyAgentOS/config.json` and set `apiKey` |
| Gateway exits immediately after start | Usually a missing API key; confirm with `./scripts/install.sh logs` |
| Gateway container fails to start | `docker rm -f phyagentos-gateway` and retry |

---

## 📚 Related files

- [`Dockerfile`](../../Dockerfile) — image build definition
- [`docker-compose.yml`](../../docker-compose.yml) — Compose orchestration
- [`.dockerignore`](../../.dockerignore) — build-ignore rules
- [`scripts/install.sh`](../../scripts/install.sh) — one-click install script
