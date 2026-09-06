# Franky egress proxy: a Squid forward-proxy enforcing a default-deny domain allowlist.
# It does blind HTTPS CONNECT (no TLS termination), so the agent's creds tunnel through
# without the proxy ever seeing them. The task container has NO internet route except via
# this proxy (Docker --internal network), so this allowlist is the egress wall.
FROM alpine:3.23

# squid is the proxy; curl + ca-certificates are for the HEALTHCHECK below.
RUN apk add --no-cache squid ca-certificates curl

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# HEALTHCHECK asserts DEFAULT-DENY, not mere liveness: a proxied request to a known-denied
# sentinel host MUST be refused (Squid 403). Proves the allowlist actually loaded - if Squid
# came up allowing everything, this check would NOT see a 403 and the container stays
# unhealthy, so run_in_container's fail-closed gate refuses to start the task.
HEALTHCHECK --interval=2s --timeout=3s --start-period=2s --retries=10 \
  CMD curl -s -o /dev/null -w "%{http_code}" -x http://127.0.0.1:3128 http://denied.invalid/ | grep -q 403 || exit 1

# Alpine owns gid 13 as `news`; keep the numeric uid/gid expected by the host runtime.
USER 13:13
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
