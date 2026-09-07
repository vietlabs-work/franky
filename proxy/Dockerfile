# Franky egress proxy: a Squid forward-proxy enforcing a default-deny domain allowlist.
# It does blind HTTPS CONNECT (no TLS termination), so the agent's creds tunnel through
# without the proxy ever seeing them. The task container has NO internet route except via
# this proxy (Docker --internal network), so this allowlist is the egress wall.
FROM alpine:3.23

# squid is the proxy; curl + ca-certificates support the bounded host startup probe.
RUN apk add --no-cache squid ca-certificates curl

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Alpine owns gid 13 as `news`; keep the numeric uid/gid expected by the host runtime.
USER 13:13
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
