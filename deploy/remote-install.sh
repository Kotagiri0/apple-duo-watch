#!/usr/bin/env bash
# Install (or update) the watcher on a server over SSH.
#
#   ./deploy/remote-install.sh [-p PORT] [-i IDENTITY] [-e ENVFILE] user@host [remote-dir]
#
# Copies the service and your .env with rsync, then builds and starts it with
# docker compose; installs Docker first if the host has none. Re-running this
# is how you deploy an update. The private repo is never cloned on the server,
# so the server needs no GitHub credentials.
set -euo pipefail

PORT=${SSH_PORT:-22}
IDENTITY=${SSH_IDENTITY:-}

ENVFILE=${ENVFILE:-}

while getopts ":p:i:e:" opt; do
  case $opt in
    p) PORT=$OPTARG ;;
    i) IDENTITY=$OPTARG ;;
    e) ENVFILE=$OPTARG ;;
    *) echo "usage: $0 [-p PORT] [-i IDENTITY] [-e ENVFILE] user@host [remote-dir]" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))

TARGET=${1:?usage: $0 [-p PORT] [-i IDENTITY] user@host [remote-dir]}
REMOTE_DIR=${2:-/opt/apple-duo-watch}
HERE=$(cd "$(dirname "$0")/.." && pwd)

SSH_OPTS=(-p "$PORT" -o ConnectTimeout=20)
[[ -n $IDENTITY ]] && SSH_OPTS+=(-i "$IDENTITY" -o IdentitiesOnly=yes)
ssh_run() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@"; }

echo "==> connecting to $TARGET (port $PORT)"
ssh_run 'echo "connected to $(hostname)"'

# The token can live here or already on the server. Keeping a second copy of
# it on your laptop is optional: an update does not need to resend the .env.
# A second instance — the standby on another host — is this same image with
# another env file, so -e says which local file is installed as .env there.
ENVFILE=${ENVFILE:-$HERE/.env}
[[ $ENVFILE = /* ]] || ENVFILE=$HERE/$ENVFILE
SEND_ENV=1
if [[ ! -f "$ENVFILE" ]]; then
  if ssh_run "grep -qE '^TELEGRAM_BOT_TOKEN=.+' '$REMOTE_DIR/.env'" 2>/dev/null; then
    echo "==> keeping the .env already in $TARGET:$REMOTE_DIR"
    SEND_ENV=0
  else
    echo "error: $ENVFILE is missing — copy an .env.example to it and put the bot token in it" >&2
    exit 1
  fi
fi

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
# No --chmod here: the rsync macOS ships with does not support it.
rsync -az -e "ssh ${SSH_OPTS[*]}" \
  "$HERE/watcher.py" "$HERE/Dockerfile" "$HERE/docker-compose.yml" \
  "$TARGET:$REMOTE_DIR/"
if (( SEND_ENV )); then
  rsync -az -e "ssh ${SSH_OPTS[*]}" "$ENVFILE" "$TARGET:$REMOTE_DIR/.env"
fi
ssh_run "chmod 600 '$REMOTE_DIR/.env'"

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
