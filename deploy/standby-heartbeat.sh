#!/bin/sh
# Refresh the primary watcher's heartbeat for the standby container.
#
# The standby reads the primary's last successful poll, but the command runs
# inside the container, which deliberately has no ssh client and no key. So
# the host fetches it on a timer and leaves it in a file the container mounts
# read-only. If this script fails, the file simply goes stale — and a stale
# file is exactly the signal to take over, so a broken ssh path fails safe.
set -eu

: "${PRIMARY_SSH:?set PRIMARY_SSH to the ssh command that prints the primary heartbeat}"
OUT=${HEARTBEAT_OUT:-/var/lib/duo-standby/primary-heartbeat}

mkdir -p "$(dirname "$OUT")"
stamp=$(sh -c "$PRIMARY_SSH" 2>/dev/null) || exit 1
case "$stamp" in
  20[0-9][0-9]-[0-1][0-9]-[0-3][0-9]T*) ;;
  *) echo "unexpected heartbeat: $stamp" >&2; exit 1 ;;
esac
printf '%s\n' "$stamp" > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
