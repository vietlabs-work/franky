#!/usr/bin/env bash
# Franky DinD entrypoint (issue #12): bring up a rootless Docker daemon INSIDE this hardened
# container, then exec the coding-agent engine. Always on - franky's value is building+testing
# repos, and many repos' suites need local infra (docker compose, testcontainers, `docker build`).
#
# Egress: the daemon inherits this container's HTTP(S)_PROXY/NO_PROXY env, so the DAEMON's own
# registry pulls AND `docker build`/BuildKit `FROM` fetches go through the Squid egress proxy. We
# ALSO render ~/.docker/config.json so INNER containers inherit the proxy - but that is only a
# convenience for well-behaved clients; the real enforcement is the outer --internal network (no
# route to the internet) + Squid. We NEVER write any secret (GH_TOKEN / provider key) into the
# docker config or the daemon env - only proxy URLs.
set -u

uid="$(id -u)"
export XDG_RUNTIME_DIR="/run/user/${uid}"
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/docker.sock"

# rootless dockerd wants the runtime dir private (0700). We own it (tmpfs uid=1001).
chmod 700 "${XDG_RUNTIME_DIR}" 2>/dev/null || true

# Make INNER containers inherit the egress proxy (proxy URLs ONLY - never creds). With no proxy
# env set (e.g. local dev), write nothing so docker keeps its defaults.
mkdir -p "${HOME}/.docker"
if [ -n "${HTTP_PROXY:-}${HTTPS_PROXY:-}" ]; then
    cat > "${HOME}/.docker/config.json" <<JSON
{
  "proxies": {
    "default": {
      "httpProxy": "${HTTP_PROXY:-}",
      "httpsProxy": "${HTTPS_PROXY:-}",
      "noProxy": "${NO_PROXY:-localhost,127.0.0.1}"
    }
  }
}
JSON
fi

# Start rootless dockerd in the background. It inherits HTTP(S)_PROXY/NO_PROXY from this env, so
# its registry pulls + BuildKit builds traverse the proxy. slirp4netns is the network backend
# (needs /dev/net/tun, granted by the run profile); storage is native/fuse overlayfs.
dockerd-rootless.sh >/tmp/dockerd.log 2>&1 &

# Wait for the daemon socket - HARD-CAPPED (never an unbounded wait): 30 * 1s = 30s.
ready=0
for _ in $(seq 1 30); do
    if docker version >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done

if [ "${ready}" != 1 ]; then
    # Non-fatal on purpose: tasks that do not need Docker must still run. Emit a loud,
    # redaction-safe marker so the operator log distinguishes "infra did not come up" from an
    # agent bug; docker-dependent build/test steps will then fail inside the task.
    echo "franky: WARNING rootless dockerd did not become ready in 30s - docker-dependent" \
         "build/test steps will fail (see /tmp/dockerd.log inside the container)." >&2
fi

# Unpack operator profile bundle (skills/instructions/knowledge) into HOME if provided.
# The bundle is a gzip-compressed tar of curated, secret-scrubbed prose files packed
# with paths relative to HOME.  It is extracted here - before exec-ing the engine - so
# the in-container agent finds the operator's skills/instructions as it would locally.
# The var is unset after extraction so it does not leak into the agent environment.
# WHY safe: the bundle is assembled host-side from an explicit allowlist and refused
# (fail-closed) by the host if any credential pattern is detected (see franky/profile.py).
if [ -n "${FRANKY_PROFILE_BUNDLE:-}" ]; then
    printf '%s' "${FRANKY_PROFILE_BUNDLE}" | base64 -d | tar -xz -C "${HOME}"
    unset FRANKY_PROFILE_BUNDLE
fi

exec "$@"
