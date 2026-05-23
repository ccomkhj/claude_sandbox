#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH=/etc/squid/conf.d/allowlist.conf

echo "[sandbox-proxy] waiting for $CONFIG_PATH to be cp'd in..." >&2
while [ ! -s "$CONFIG_PATH" ]; do
    sleep 0.5
done
echo "[sandbox-proxy] config detected; starting squid" >&2

# The ubuntu/squid image's actual startup script varies between versions.
# When delegating to the upstream entrypoint, pass the standard squid args
# so the real squid process starts (not just the -Nz cache-init run).
# Fallback: start squid directly in foreground mode.
if [ -x /usr/local/bin/entrypoint.sh ]; then
    exec /usr/local/bin/entrypoint.sh -f /etc/squid/squid.conf -NYC
elif [ -x /usr/sbin/entrypoint.sh ]; then
    exec /usr/sbin/entrypoint.sh -f /etc/squid/squid.conf -NYC
elif [ -x /entrypoint.sh ]; then
    exec /entrypoint.sh -f /etc/squid/squid.conf -NYC
fi
exec squid -N -f /etc/squid/squid.conf
