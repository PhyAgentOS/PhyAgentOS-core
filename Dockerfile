FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

LABEL org.opencontainers.image.title="PhyAgentOS" \
      org.opencontainers.image.version="1.1.1" \
      org.opencontainers.image.source="https://github.com/PhyAgentOS/PhyAgentOS-core"

# Install Node.js 20 for the WhatsApp bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (cached layer)
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p PhyAgentOS bridge && touch PhyAgentOS/__init__.py && \
    uv pip install --system --no-cache . && \
    rm -rf PhyAgentOS bridge

# Copy the full source and install
COPY PhyAgentOS/ PhyAgentOS/
COPY bridge/ bridge/
RUN uv pip install --system --no-cache .

# Build the WhatsApp bridge from the committed lock file. A failed dependency
# install or TypeScript compilation must fail the image build.
WORKDIR /app/bridge
RUN npm ci && npm run build
WORKDIR /app

# Release smoke check: verifies the installed console entry point and runtime version.
RUN paos --version

# Create config directory
RUN mkdir -p /root/.PhyAgentOS

# NOTE: The gateway is a message-bus service (agent + channels + cron +
# heartbeat). It makes outbound connections only and does NOT bind an inbound
# port, so no EXPOSE is needed. If an inbound HTTP endpoint is added later,
# re-add EXPOSE <port> here.
ENTRYPOINT ["paos"]
CMD ["agent"]
