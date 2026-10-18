#!/usr/bin/env bash
# Install the standby watcher on a host whose address differs from the
# primary's — the point being that Apple rate-limiting one address leaves the
# other working.
#
#   ./deploy/standby-install.sh [-i IDENTITY] user@standby-host primary-ssh-spec
#
# Example, as deployed:
#   ./deploy/standby-install.sh -i ~/.ssh/duo_tunnel root@188.213.0.80 \
#     'ssh -p 2222 -i /root/.ssh/read_primary -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 root@37.204.22.209'
#
# The primary-ssh-spec must print the primary's last successful poll as an ISO
# timestamp. On the primary, that key is restricted to exactly that one
# command with no shell:
#   restrict,command="/usr/local/bin/duo-heartbeat" ssh-ed25519 …
set -euo pipefail

IDENTITY=${SSH_IDENTITY:-}
while getopts ":i:" opt; do
  case $opt in
    i) IDENTITY=$OPTARG ;;
    *) echo "usage: $0 [-i IDENTITY] user@host primary-ssh-spec" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))

TARGET=${1:?usage: $0 [-i IDENTITY] user@host primary-ssh-spec}
PRIMARY_SSH=${2:?second argument: the ssh command that prints the primary heartbeat}
REMOTE_DIR=${REMOTE_DIR:-/opt/apple-duo-watch-standby}
HERE=$(cd "$(dirname "$0")/.." && pwd)

SSH_OPTS=(-o ConnectTimeout=20)
[[ -n $IDENTITY ]] && SSH_OPTS+=(-i "$IDENTITY" -o IdentitiesOnly=yes)
ssh_run() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

if [[ ! -f "$HERE/.env.standby" ]]; then
  echo "error: $HERE/.env.standby is missing — copy deploy/standby.env.example to it" >&2
  exit 1
fi

echo "==> connecting to $TARGET"
ssh_run 'echo "connected to $(hostname)"'

echo "==> installing the heartbeat refresher on the host"
# The container has no ssh client by design, so the host fetches the primary's
# heartbeat on a timer and leaves it in a file mounted read-only. A stale file
# fails safe: it reads as "the primary is quiet".
scp -q "${SSH_OPTS[@]}" "$HERE/deploy/standby-heartbeat.sh" "$TARGET:/usr/local/bin/standby-heartbeat.sh"
scp -q "${SSH_OPTS[@]}" "$HERE/deploy/standby-heartbeat.service" \
                        "$HERE/deploy/standby-heartbeat.timer" "$TARGET:/etc/systemd/system/"
ssh_run "chmod 755 /usr/local/bin/standby-heartbeat.sh
         printf '%s\n' 'PRIMARY_SSH=$PRIMARY_SSH' > /etc/duo-standby.env
         chmod 600 /etc/duo-standby.env
         mkdir -p /var/lib/duo-standby
         systemctl daemon-reload
         systemctl enable --now standby-heartbeat.timer >/dev/null 2>&1
         systemctl start standby-heartbeat.service"

echo "==> verifying the heartbeat is readable"
ssh_run 'test -s /var/lib/duo-standby/primary-heartbeat \
  && echo "primary last polled: $(cat /var/lib/duo-standby/primary-heartbeat)" \
  || { echo "heartbeat file is empty — check /etc/duo-standby.env and the restricted key" >&2; exit 1; }'

echo "==> copying the service to $REMOTE_DIR"
ssh_run "mkdir -p '$REMOTE_DIR'"
rsync -az -e "ssh ${SSH_OPTS[*]}" "$HERE/watcher.py" "$HERE/Dockerfile" "$TARGET:$REMOTE_DIR/"
rsync -az -e "ssh ${SSH_OPTS[*]}" "$HERE/docker-compose.standby.yml" "$TARGET:$REMOTE_DIR/docker-compose.yml"
rsync -az -e "ssh ${SSH_OPTS[*]}" "$HERE/.env.standby" "$TARGET:$REMOTE_DIR/.env"
ssh_run "chmod 600 '$REMOTE_DIR/.env'"

echo "==> building and starting"
ssh_run "cd '$REMOTE_DIR' && docker compose up -d --build"
sleep 18
ssh_run "cd '$REMOTE_DIR' && docker compose logs --tail 12 --no-log-prefix"

cat <<DONE

Standby deployed to $TARGET:$REMOTE_DIR
It polls the store continuously and stays silent while the primary answers.

To rehearse the failover (it sends two real messages):
  ssh $TARGET "systemctl stop standby-heartbeat.timer
               echo 2020-01-01T00:00:00+00:00 > /var/lib/duo-standby/primary-heartbeat"
  # wait out PRIMARY_CHECK_EVERY, expect "taking over alerts" in the log
  ssh $TARGET "systemctl start standby-heartbeat.timer; systemctl start standby-heartbeat.service"
  # expect "going quiet"
DONE
