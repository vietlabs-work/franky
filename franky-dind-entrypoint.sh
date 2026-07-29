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

if [ "${ready}" = 1 ]; then
    # Positive marker (issue #69): host-side diagnostics capture asserts DinD readiness from
    # this exact string rather than only inferring it from the ABSENCE of the failure warning
    # below - a positive signal beats an "I didn't see a complaint" one.
    echo "franky: rootless dockerd ready" >&2
else
    # Non-fatal on purpose: tasks that do not need Docker must still run. Emit a loud,
    # redaction-safe marker so the operator log distinguishes "infra did not come up" from an
    # agent bug; docker-dependent build/test steps will then fail inside the task.
    echo "franky: WARNING rootless dockerd did not become ready in 30s - docker-dependent" \
         "build/test steps will fail (see /tmp/dockerd.log inside the container)." >&2
fi

# Operator profile (skills/instructions/commands/agent definitions, or a whole swept
# `~/.claude`-style setup): the host streams a gzip tar of curated, secret-scrubbed files into
# HOME over `docker exec -i` and touches the marker LAST, once the untar is clean. We only wait
# for that marker here - the extraction is host-driven, because the bundle cannot ride the argv
# (a swept setup is ~1 MB, past Linux's 128 KB per-argument limit) and `docker cp` into this
# --read-only container is refused by the daemon.
# Wait is capped (never unbounded). No marker means the profile is absent or half-unpacked, and
# running the engine anyway would silently produce a build without the operator's setup - so we
# exit nonzero and let the host classify it as a failure.
# WHY safe: the bundle is assembled host-side from a bounded allowlist and refused (fail-closed)
# if any credential pattern is detected (see franky/profile.py and franky/setups.py).
if [ -n "${FRANKY_PROFILE_WAIT:-}" ]; then
    ready=0
    for _ in $(seq 1 120); do
        if [ -f "${HOME}/.franky-profile-ready" ]; then ready=1; break; fi
        sleep 1
    done
    if [ "${ready}" != 1 ]; then
        echo "franky: operator profile was never injected (marker absent after 120s) -" \
             "refusing to run without it" >&2
        exit 78
    fi
    rm -f "${HOME}/.franky-profile-ready" 2>/dev/null || true
    unset FRANKY_PROFILE_WAIT
fi

# Resume mode (issue #71): the host docker-cp's the prior workspace into this started
# container's tmpfs /work and signals by touching the marker. Wait (capped, never unbounded).
# If the marker never arrives we must NOT run the engine on an empty /work (that would silently
# become a fresh run) - exit nonzero so the host classifies it as a failed restore.
if [ -n "${FRANKY_RESUME_WAIT:-}" ]; then
    ready=0
    for _ in $(seq 1 120); do
        if [ -f "/work/.franky-resume-ready" ]; then ready=1; break; fi
        sleep 1
    done
    if [ "${ready}" != 1 ]; then
        echo "franky: resume workspace was never restored (marker absent after 120s) - refusing to run on an empty /work" >&2
        exit 75
    fi
    rm -f "/work/.franky-resume-ready" 2>/dev/null || true
    unset FRANKY_RESUME_WAIT
fi

exec "$@"
