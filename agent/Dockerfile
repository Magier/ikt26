# A box with five coding agents in it.
#
# Check out a repo into /workspace, point an agent at it. Driven from a shell
# (`podman exec -it`) or from the web UI on :8080 - both run the same scripts.
#
# node:22-bookworm, not the netshoot/Alpine base the other app uses: these
# agents ship glibc binaries and npm packages with native deps, and musl breaks
# several of them. The full (non-slim) tag already carries git, curl and
# build-essential, which the runtime-install story needs anyway.
FROM node:22-bookworm

LABEL org.opencontainers.image.source="https://github.com/Magier/ikt26" \
      org.opencontainers.image.description="Coding-agent workbench: claude, codex, agy, pi, hermes"

ENV DEBIAN_FRONTEND=noninteractive

# python3-venv and xz-utils are not optional: the hermes installer unpacks a
# .tar.xz, and company tooling will want a venv.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq less nano ripgrep unzip xz-utils \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Debian marks its python as externally-managed, so a plain `pip install` fails.
# Deliberate: this container is for installing things at runtime, and being
# told to pass --break-system-packages every time is friction with no payoff
# here. Revisit when the sandbox work happens.
RUN rm -f /usr/lib/python3*/EXTERNALLY-MANAGED

# gh: the agents drive PR work through it, and it is not in Debian's repos.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:${PATH}"

# uv, for pulling python tooling in at runtime without fighting apt.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# --- the agents -------------------------------------------------------------
# One RUN per install source, not one big layer: when a vendor breaks their
# installer, the build fails on that line and the others stay cached, instead
# of one opaque failure for all five.

# claude, codex, pi. Unpinned on purpose - these ship near-daily and a pin here
# goes stale faster than anyone will remember to bump it.
RUN npm install -g \
        @anthropic-ai/claude-code \
        @openai/codex \
        @mariozechner/pi-coding-agent \
    && npm cache clean --force

# agy (Google Antigravity CLI) - a Go binary, installs to ~/.local/bin.
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash

# hermes (Nous Research) - brings its own node/python runtime along.
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

COPY bin/ /usr/local/bin/
COPY server.py /opt/agentbox/server.py
COPY entrypoint.sh /usr/local/bin/entrypoint
RUN chmod +x /usr/local/bin/checkout /usr/local/bin/agent-run /usr/local/bin/entrypoint

# Every agent reads the same SKILL.md standard but looks in its own directory.
# entrypoint symlinks all of them at /skills so you write a skill once.
ENV SKILLS_DIR=/skills \
    WORKSPACE=/workspace \
    PORT=8080 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /skills /workspace

WORKDIR /workspace
EXPOSE 8080
ENTRYPOINT ["entrypoint"]
CMD ["python3", "/opt/agentbox/server.py"]
