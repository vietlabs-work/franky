#!/bin/sh
# Render a self-contained, default-deny Squid config to a writable tmpfs path (the root FS is
# --read-only) and serve it in the foreground as PID 1. Fail-closed at every step: refuse an
# empty allowlist, and abort if the rendered config does not parse.
set -eu

if [ -z "${FRANKY_ALLOWED_DOMAINS:-}" ]; then
    echo "franky-proxy: FRANKY_ALLOWED_DOMAINS is empty - refusing to start (fail-closed)" >&2
    exit 1
fi

# Comma-separated -> space-separated for the squid dstdomain acl. $DOMAINS is interpolated
# unquoted on purpose (squid dstdomain takes space-separated args); the `squid -k parse` gate
# below is the fail-closed backstop if the value is ever malformed.
DOMAINS=$(echo "$FRANKY_ALLOWED_DOMAINS" | tr ',' ' ')

CONF=/run/squid.conf

# Everything writable points at /run (tmpfs): the root FS is read-only. HTTPS-ONLY egress:
# port 443 is the only Safe_port, so plain-HTTP (port 80) GETs are denied outright. This is
# deliberate - allowing port 80 to allowlisted hosts would be a cleartext, proxy-VISIBLE
# channel (the proxy fetches HTTP itself and sees the full URL/body), defeating the
# blind-CONNECT property that keeps creds opaque to the proxy. Default-deny order: deny
# non-443 ports, deny CONNECT to non-443, ALLOW the allowlist, then deny everything else.
cat > "$CONF" <<EOF
# Skip hostname discovery and its repeated DNS probes during startup.
visible_hostname franky-proxy
acl allowed_domains dstdomain $DOMAINS
acl SSL_ports port 443
acl Safe_ports port 443
acl CONNECT method CONNECT
http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
http_access allow allowed_domains
http_access deny all
http_port 3128
cache deny all
cache_mem 0 MB
access_log stdio:/run/squid-access.log
cache_log /run/squid-cache.log
pid_filename /run/squid.pid
EOF

# Validate FAIL-CLOSED before serving: a malformed allowlist must never reach a running
# state (a parse error here aborts the container, so the host startup probe cannot pass).
squid -k parse -f "$CONF"

# Foreground (-N), no daemonize: squid becomes PID 1 so the container lifecycle tracks it.
exec squid -N -f "$CONF"
