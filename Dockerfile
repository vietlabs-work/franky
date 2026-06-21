# Franky build image: one image bundles both coding-agent engines (pi default, claude alt).
# Hardened at run time (see franky/container.py); the image itself only pre-bakes the toolchain
# so no task needs runtime root apt.
FROM node:22-slim

# Toolchain: git + gh (GitHub CLI via its apt keyring) + python3 + ripgrep + build deps.
# --no-install-recommends keeps the layer lean; gh needs its own signed apt repo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        python3 \
        ripgrep \
        build-essential \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Both engines are node CLIs, so one npm install bundles them.
#   pi     -> @earendil-works/pi-coding-agent  (bin: pi)
#   claude -> @anthropic-ai/claude-code        (bin: claude)
# Clean the npm cache in the SAME layer - otherwise ~100MB of /root/.npm download
# cache commits into the image (it is dead weight at runtime; npm refetches on demand).
RUN npm install -g \
        @earendil-works/pi-coding-agent \
        @anthropic-ai/claude-code \
    && npm cache clean --force

# Non-root: the agent runs as an unprivileged user inside the disposable container.
RUN useradd --create-home --shell /bin/bash franky
WORKDIR /work
USER franky

# This image is always invoked with an explicit command by franky/container.py. The default
# is just a hint for anyone who runs it bare.
CMD ["echo", "Run via the franky CLI: franky build <issue-url | prose>"]
