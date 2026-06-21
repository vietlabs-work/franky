# Franky egress proxy: a Squid forward-proxy enforcing a default-deny domain allowlist.
# It does blind HTTPS CONNECT (no TLS termination), so the agent's creds tunnel through
# without the proxy ever seeing them. The task container has NO internet route except via
# this proxy (Docker --internal network), so this allowlist is the egress wall.
FROM debian:stable-slim

# squid is the proxy; curl + ca-certificates are for the HEALTHCHECK below.
# --no-install-recommends keeps the layer lean; clean apt lists after install.
RUN apt-get update && apt-get install -y --no-install-recommends squid ca-certificates curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# HEALTHCHECK asserts DEFAULT-DENY, not mere liveness: a proxied request to a known-denied
# sentinel host MUST be refused (Squid 403). Proves the allowlist actually loaded - if Squid
# came up allowing everything, this check would NOT see a 403 and the container stays
# unhealthy, so run_in_container's fail-closed gate refuses to start the task.
HEALTHCHECK --interval=2s --timeout=3s --start-period=2s --retries=10 \
  CMD curl -s -o /dev/null -w "%{http_code}" -x http://127.0.0.1:3128 http://denied.invalid/ | grep -q 403 || exit 1

# Run as squid's own unprivileged user (debian package user `proxy`), never root.
USER proxy
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
