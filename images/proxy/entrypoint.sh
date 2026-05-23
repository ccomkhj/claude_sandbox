#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH=/etc/squid/conf.d/allowlist.conf

echo "[sandbox-proxy] waiting for $CONFIG_PATH to be cp'd in..." >&2
while [ ! -s "$CONFIG_PATH" ]; do
    sleep 0.5
done
echo "[sandbox-proxy] config detected; starting squid" >&2

# The ubuntu/squid image's actual startup script varies between versions.
# Try the most common upstream entrypoint paths; fall back to direct squid.
if [ -x /usr/sbin/entrypoint.sh ]; then
    exec /usr/sbin/entrypoint.sh "$@"
elif [ -x /entrypoint.sh ]; then
    exec /entrypoint.sh "$@"
fi
exec squid -N -f /etc/squid/squid.conf
