# Franky build image: one image bundles all coding-agent engines AND
# an always-on rootless Docker daemon (issue #12) so a task can build/test repos whose suites
# need local infra (docker compose, testcontainers, `docker build`). Hardened at run time (see
# franky/container.py); the image pre-bakes the toolchain so no task needs runtime root apt.
#
# WHY rootless Docker is baked in (vs the repo's toolchain): franky's tools stay in THIS image;
# the repo's build/base images are pulled by the nested daemon at run time - clean separation,
# the franky image never mixes with repo toolchains.

# ---- stage 1: fetch Docker's STATIC binaries (glibc-safe, lean - only what we need) ----------
# Static binaries off download.docker.com avoid the full docker-ce apt package set (systemd
# units, recommends, rootful service) and the musl/glibc mismatch of COPY-ing from the Alpine
# docker:dind image. Versions are pinned + overridable.
FROM debian:bookworm-slim AS docker-dl
ARG DOCKER_VERSION=27.3.1
ARG BUILDX_VERSION=v0.17.1
ARG COMPOSE_VERSION=v2.29.7
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) dockerarch=x86_64;  plugarch=amd64 ;; \
        arm64) dockerarch=aarch64; plugarch=arm64 ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    mkdir -p /out/bin /out/cli-plugins; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${dockerarch}/docker-${DOCKER_VERSION}.tgz" \
        | tar -xz -C /tmp; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${dockerarch}/docker-rootless-extras-${DOCKER_VERSION}.tgz" \
        | tar -xz -C /tmp; \
    cp /tmp/docker/* /out/bin/; \
    cp /tmp/docker-rootless-extras/* /out/bin/; \
    curl -fsSL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-${plugarch}" \
        -o /out/cli-plugins/docker-buildx; \
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${dockerarch}" \
        -o /out/cli-plugins/docker-compose; \
    chmod +x /out/bin/* /out/cli-plugins/*

# ---- stage 2: the franky runtime image -------------------------------------------------------
FROM node:22-slim

# Toolchain: git + gh (GitHub CLI via its apt keyring) + python3 + ripgrep + build deps, PLUS the
# rootless-Docker runtime deps that are NOT in the static tarballs: uidmap (newuidmap/newgidmap
# for the subordinate uid/gid map), slirp4netns (rootless network backend), fuse-overlayfs
# (rootless storage fallback), iptables + iproute2 (nested container networking).
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
        uidmap \
        libcap2-bin \
        slirp4netns \
        fuse-overlayfs \
        iptables \
        iproute2 \
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

# Rootless Docker binaries from stage 1: engine/CLI/runtime in /usr/local/bin, buildx+compose as
# CLI plugins in the system plugin dir.
COPY --from=docker-dl /out/bin/ /usr/local/bin/
COPY --from=docker-dl /out/cli-plugins/ /usr/local/lib/docker/cli-plugins/

# All engines are node CLIs, so one npm install bundles them.
#   pi     -> @earendil-works/pi-coding-agent  (bin: pi)
#   claude -> @anthropic-ai/claude-code        (bin: claude)
#   codex  -> @openai/codex                    (bin: codex)
#   opencode -> opencode-ai                    (bin: opencode)
# Clean the npm cache in the SAME layer - otherwise ~100MB of /root/.npm download
# cache commits into the image (it is dead weight at runtime; npm refetches on demand).
RUN npm install -g \
        @earendil-works/pi-coding-agent \
        @anthropic-ai/claude-code \
        @openai/codex \
        opencode-ai \
    && npm cache clean --force

# Non-root: the agent (and the rootless Docker daemon) run as this unprivileged user inside the
# disposable container. Pin uid 1001 EXPLICITLY - container.py's tmpfs uid=, the subuid/subgid
# map, and the rootless data-root ownership all hardcode 1001, so it must not drift with the
# base image's next-free uid. Set the subordinate uid/gid range rootlesskit maps into.
#
# WHY file CAPABILITIES on newuidmap/newgidmap instead of the Debian setuid bit: under our run
# profile (--cap-drop=ALL + only CAP_SETUID/SETGID added), Debian's setuid-root newuidmap fails
# to write a child's /proc/PID/uid_map ("open of uid_map failed: Permission denied"), whereas the
# file-capability model (what Alpine's docker:dind-rootless ships, mode 755 + cap_setuid=ep)
# works. So drop the setuid bit and grant the exact file caps - matching the proven Alpine setup.
RUN useradd --uid 1001 --create-home --shell /bin/bash franky \
    && mkdir -p /work \
    && chown 1001:1001 /work \
    && printf 'franky:100000:65536\n' > /etc/subuid \
    && printf 'franky:100000:65536\n' > /etc/subgid \
    && setcap cap_setuid+ep /usr/bin/newuidmap \
    && setcap cap_setgid+ep /usr/bin/newgidmap \
    && chmod u-s /usr/bin/newuidmap /usr/bin/newgidmap

# The DinD entrypoint starts rootless dockerd then execs the engine argv (supplied as CMD by
# franky/container.py).
COPY franky-dind-entrypoint.sh /usr/local/bin/franky-dind-entrypoint.sh
RUN chmod +x /usr/local/bin/franky-dind-entrypoint.sh

# Point the docker CLI at the rootless daemon's socket for EVERY process in the container (not
# just the entrypoint's exec'd child) so `docker` works for the engine AND any `docker exec`
# debugging session. XDG_RUNTIME_DIR matches where dockerd-rootless.sh puts the socket.
ENV XDG_RUNTIME_DIR=/run/user/1001 \
    DOCKER_HOST=unix:///run/user/1001/docker.sock

WORKDIR /work
USER franky

ENTRYPOINT ["/usr/local/bin/franky-dind-entrypoint.sh"]
# This image is always invoked with an explicit command by franky/container.py. The default is
# just a hint for anyone who runs it bare.
CMD ["echo", "Run via the franky CLI: franky build <issue-url | prose>"]
