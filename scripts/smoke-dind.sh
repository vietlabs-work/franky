#!/usr/bin/env bash
# Manual smoke test for always-on rootless Docker-in-Docker (issue #12).
#
# WHY this is a script, not a pytest: the hermetic suite never touches real Docker (a
# load-bearing rule - see CLAUDE.md). The Dockerfile + entrypoint + the relaxed run profile can
# only be exercised against a real daemon, so this is the gate to run before merging any change
# to the Dockerfile, franky-dind-entrypoint.sh, container.py's _HARDENING, or egress.py.
#
# It builds both images, stands up franky's real egress cage (internal network + Squid proxy +
# killed DNS), runs the franky image's nested rootless daemon inside it, and asserts:
#   - rootless dockerd starts (no host socket, no --privileged)
#   - nested pull + `docker compose up` test infra works THROUGH the proxy
#   - off-allowlist egress (build FROM / build RUN / a nested container) is refused (cage holds)
#
# Usage: scripts/smoke-dind.sh   (needs Docker running; ~5 min on a cold cache)
set -euo pipefail

FRANKY_IMG="${FRANKY_IMG:-franky}"
PROXY_IMG="${PROXY_IMG:-franky-proxy}"
NET="smoke-dind-net-$$"
PROXY="smoke-dind-proxy-$$"
TASK="smoke-dind-task-$$"
# Derive policy from the production modules.
ALLOW="$(python3 -c 'from franky.egress import DOCKER_REGISTRY_DOMAINS, GITHUB_DOMAINS; print(",".join(DOCKER_REGISTRY_DOMAINS + GITHUB_DOMAINS))')"

cleanup() { docker rm -f -v "$TASK" "$PROXY" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

echo "== build images =="
if [ "${SMOKE_SKIP_BUILD:-0}" != 1 ]; then
  docker build -t "$FRANKY_IMG" .
  docker build -t "$PROXY_IMG" proxy/
fi

echo "== stand up the egress cage =="
cleanup
docker network create --internal --driver bridge "$NET" >/dev/null
python3 scripts/smoke-task.py "$PROXY_IMG" "$PROXY" proxy "$ALLOW" >/dev/null
docker network connect "$NET" "$PROXY"
for _ in $(seq 1 20); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$PROXY" 2>/dev/null)" = healthy ] && break; sleep 1
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$PROXY" 2>/dev/null)" = healthy ] || fail "proxy not healthy"

echo "== run the franky image (real _HARDENING profile) in the cage =="
SMOKE_NETWORK="$NET" SMOKE_PROXY_URL="http://$PROXY:3128" \
  python3 scripts/smoke-task.py "$FRANKY_IMG" "$TASK" normal sleep 300 >/dev/null

echo "== 1. rootless dockerd readiness =="
ready=0
for _ in $(seq 1 30); do docker exec "$TASK" docker version >/dev/null 2>&1 && { ready=1; break; }; sleep 2; done
[ "$ready" = 1 ] || { docker exec "$TASK" cat /tmp/dockerd.log 2>&1 | tail -20 || true; fail "rootless dockerd did not come up"; }
echo "   OK"

echo "== 2. nested compose up test infra THROUGH the proxy =="
docker exec "$TASK" sh -c 'mkdir -p /tmp/p && cat > /tmp/p/docker-compose.yml <<EOF
services:
  db:
    image: postgres:16-alpine
    environment: { POSTGRES_PASSWORD: smoke }
    healthcheck: { test: ["CMD-SHELL","pg_isready -U postgres"], interval: 2s, retries: 20 }
EOF
cd /tmp/p && docker compose up -d --wait' >/dev/null 2>&1 || fail "compose up failed"
docker exec "$TASK" sh -c 'cd /tmp/p && docker compose exec -T db psql -U postgres -tAc "select 1"' | grep -q 1 || fail "psql query failed"
docker exec "$TASK" sh -c 'cd /tmp/p && docker compose down -v' >/dev/null 2>&1
echo "   OK"

echo "== 3. off-allowlist docker build FROM is refused by the proxy (cage holds) =="
if docker exec "$TASK" sh -c 'mkdir -p /tmp/b && printf "FROM cr.example.com/x/y:latest\n" > /tmp/b/Dockerfile && cd /tmp/b && timeout 40 docker build -t t . ' >/dev/null 2>&1; then
  fail "off-allowlist FROM unexpectedly SUCCEEDED - egress cage breached"
fi
echo "   OK (build refused)"

echo "== 4. nested container has no route to the internet (proxies unset, raw IP) =="
if docker exec "$TASK" sh -c 'docker run --rm -e HTTP_PROXY= -e HTTPS_PROXY= -e http_proxy= -e https_proxy= alpine:3.20 wget -T 8 -q -O /dev/null https://1.1.1.1/' >/dev/null 2>&1; then
  fail "nested container reached the internet directly - egress cage breached"
fi
echo "   OK (no route)"

echo "SMOKE PASS: rootless DinD works and stays inside the egress cage."
