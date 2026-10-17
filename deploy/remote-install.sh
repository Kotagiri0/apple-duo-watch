#!/usr/bin/env bash
# Install (or update) the watcher on a server over SSH.
#
#   ./deploy/remote-install.sh [-p PORT] [-i IDENTITY] user@host [remote-dir]
#
# Copies the service and your .env with rsync, then builds and starts it with
# docker compose; installs Docker first if the host has none. Re-running this
# is how you deploy an update. The private repo is never cloned on the server,
# so the server needs no GitHub credentials.
set -euo pipefail

PORT=${SSH_PORT:-22}
IDENTITY=${SSH_IDENTITY:-}

while getopts ":p:i:" opt; do
  case $opt in
    p) PORT=$OPTARG ;;
    i) IDENTITY=$OPTARG ;;
    *) echo "usage: $0 [-p PORT] [-i IDENTITY] user@host [remote-dir]" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))

TARGET=${1:?usage: $0 [-p PORT] [-i IDENTITY] user@host [remote-dir]}
REMOTE_DIR=${2:-/opt/apple-duo-watch}
HERE=$(cd "$(dirname "$0")/.." && pwd)

SSH_OPTS=(-p "$PORT" -o ConnectTimeout=20)
[[ -n $IDENTITY ]] && SSH_OPTS+=(-i "$IDENTITY" -o IdentitiesOnly=yes)
ssh_run() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

if [[ ! -f "$HERE/.env" ]]; then
  echo "error: $HERE/.env is missing — copy .env.example to .env and put the bot token in it" >&2
  exit 1
fi

echo "==> connecting to $TARGET (port $PORT)"
ssh_run 'echo "connected to $(hostname)"'

echo "==> ensuring docker is present"
ssh_run 'set -e
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "installing docker..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker --version && docker compose version'

echo "==> copying service files to $REMOTE_DIR"
ssh_run "mkdir -p '$REMOTE_DIR'"
rsync -az --chmod=F600 -e "ssh ${SSH_OPTS[*]}" \
  "$HERE/watcher.py" "$HERE/Dockerfile" "$HERE/docker-compose.yml" "$HERE/.env" \
  "$TARGET:$REMOTE_DIR/"

echo "==> building and starting"
ssh_run "cd '$REMOTE_DIR' && docker compose up -d --build"

echo "==> waiting for the first check"
sleep 15
ssh_run "cd '$REMOTE_DIR' && docker compose logs --tail 20"

SSH_SHOW="ssh -p $PORT${IDENTITY:+ -i $IDENTITY} $TARGET"
cat <<DONE

Deployed to $TARGET:$REMOTE_DIR
  logs:    $SSH_SHOW "cd $REMOTE_DIR && docker compose logs -f"
  restart: $SSH_SHOW "cd $REMOTE_DIR && docker compose restart"
  stop:    $SSH_SHOW "cd $REMOTE_DIR && docker compose down"

Now send /start to your Telegram bot so it learns which chat to notify.
DONE
