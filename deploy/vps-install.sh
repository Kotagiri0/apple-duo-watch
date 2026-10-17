#!/usr/bin/env bash
# Install the outside watchdog on the proxy VPS.
#
#   ./deploy/vps-install.sh [-p PORT] [-i IDENTITY] user@host
#
# Two pieces, both on the VPS, where neither the watcher's process nor its
# network can take them down with it:
#
#   duo-heartbeat-sink   loopback HTTP endpoint the watcher pings after every
#                        successful check, through the existing SSH tunnel
#   duo-deadman.timer    every two minutes, shouts if those pings stopped
#
# Alert channels come from deploy/vps-deadman.env (copy the .example).
# Re-running this is how you deploy an update.
set -euo pipefail

PORT=${SSH_PORT:-22}
IDENTITY=${SSH_IDENTITY:-}

while getopts ":p:i:" opt; do
  case $opt in
    p) PORT=$OPTARG ;;
    i) IDENTITY=$OPTARG ;;
    *) echo "usage: $0 [-p PORT] [-i IDENTITY] user@host" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))

TARGET=${1:?usage: $0 [-p PORT] [-i IDENTITY] user@host}
HERE=$(cd "$(dirname "$0")" && pwd)

SSH_OPTS=(-p "$PORT" -o ConnectTimeout=20)
[[ -n $IDENTITY ]] && SSH_OPTS+=(-i "$IDENTITY" -o IdentitiesOnly=yes)
ssh_run() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

# The alert channels can live either here or already on the server; keeping a
# second copy of them on your laptop is optional, not required.
SEND_ENV=1
if [[ -f "$HERE/vps-deadman.env" ]]; then
  if ! grep -qE '^(NTFY_URL|TELEGRAM_BOT_TOKEN)=.+' "$HERE/vps-deadman.env"; then
    echo "error: vps-deadman.env sets no alert channel — a watchdog with nowhere to shout is pointless" >&2
    exit 1
  fi
elif ssh "${SSH_OPTS[@]}" "$TARGET" 'grep -qE "^(NTFY_URL|TELEGRAM_BOT_TOKEN)=.+" /etc/duo-deadman.env' 2>/dev/null; then
  echo "==> keeping the alert channels already in $TARGET:/etc/duo-deadman.env"
  SEND_ENV=0
else
  echo "error: no alert channels — copy $HERE/vps-deadman.env.example to vps-deadman.env and fill it in, or put them in $TARGET:/etc/duo-deadman.env" >&2
  exit 1
fi

echo "==> connecting to $TARGET (port $PORT)"
ssh_run 'echo "connected to $(hostname)"'

echo "==> copying the watchdog"
# No --chmod here: the rsync macOS ships with does not support it.
rsync -az -e "ssh ${SSH_OPTS[*]}" \
  "$HERE/duo-heartbeat-sink.py" "$TARGET:/usr/local/lib/duo-heartbeat-sink.py"
rsync -az -e "ssh ${SSH_OPTS[*]}" \
  "$HERE/duo-deadman.sh" "$TARGET:/usr/local/bin/duo-deadman.sh"
rsync -az -e "ssh ${SSH_OPTS[*]}" \
  "$HERE/duo-heartbeat-sink.service" "$HERE/duo-deadman.service" \
  "$HERE/duo-deadman.timer" "$TARGET:/etc/systemd/system/"
if (( SEND_ENV )); then
  rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$HERE/vps-deadman.env" "$TARGET:/etc/duo-deadman.env"
fi

echo "==> starting"
ssh_run 'set -e
chmod 755 /usr/local/bin/duo-deadman.sh /usr/local/lib/duo-heartbeat-sink.py
chmod 600 /etc/duo-deadman.env
systemctl daemon-reload
systemctl enable --now duo-heartbeat-sink.service
systemctl restart duo-heartbeat-sink.service
# One beat of our own, so a fresh install does not read as an outage before
# the watcher has had a chance to send its first real one.
curl -fsS --max-time 10 -o /dev/null -X POST --data-binary "installed" \
  http://127.0.0.1:8899/beat
rm -f /var/lib/duo-deadman/alerted
systemctl enable --now duo-deadman.timer
systemctl is-active duo-heartbeat-sink.service
systemctl is-active duo-deadman.timer'

echo "==> checking the tunnel key may forward the heartbeat port"
# The watcher-side key is restricted with permitopen, so a new forward has to
# be allowed explicitly or the tunnel refuses to come up at all.
if ssh_run 'grep -q "permitopen=\"127.0.0.1:8888\"" /root/.ssh/authorized_keys \
            && ! grep -q "permitopen=\"127.0.0.1:8899\"" /root/.ssh/authorized_keys'; then
  cat >&2 <<'WARN'

warning: the watcher's key in /root/.ssh/authorized_keys allows forwarding to
127.0.0.1:8888 but not to 127.0.0.1:8899, so the heartbeat cannot get through.
Add the second port to that line's permitopen (comma separated), for example:

  restrict,port-forwarding,permitopen="127.0.0.1:8888",permitopen="127.0.0.1:8899",command="/bin/false" ssh-ed25519 …

WARN
fi

SSH_SHOW="ssh -p $PORT${IDENTITY:+ -i $IDENTITY} $TARGET"
cat <<DONE

Installed on $TARGET
  heartbeat:  $SSH_SHOW "stat -c %y /run/duo-watch/heartbeat"
  last check: $SSH_SHOW "systemctl status duo-deadman.timer --no-pager"
  test now:   $SSH_SHOW "systemctl start duo-deadman.service && journalctl -t duo-deadman -n 5"

On the watcher, set HEALTHCHECK_URL=http://172.17.0.1:8899/beat and redeploy.
DONE
