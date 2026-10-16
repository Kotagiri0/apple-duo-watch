#!/usr/bin/env bash
# Install (or update) the watcher on a server over SSH.
#
#   ./deploy/remote-install.sh user@host [remote-dir]
#
# Copies the service and your .env over rsync, then builds and starts it with
# docker compose. Re-running it is how you deploy an update. The private repo
# is never cloned on the server, so the server needs no GitHub credentials.
set -euo pipefail

TARGET=${1:?usage: $0 user@host [remote-dir]}
REMOTE_DIR=${2:-/opt/apple-duo-watch}
HERE=$(cd "$(dirname "$0")/.." && pwd)

if [[ ! -f "$HERE/.env" ]]; then
  echo "error: $HERE/.env is missing — copy .env.example to .env and put the bot token in it" >&2
  exit 1
fi

echo "==> checking docker on $TARGET"
ssh "$TARGET" 'command -v docker >/dev/null || { echo "docker is not installed on this host" >&2; exit 1; }
              docker compose version >/dev/null 2>&1 || { echo "docker compose v2 is missing" >&2; exit 1; }'

echo "==> creating $REMOTE_DIR"
ssh "$TARGET" "mkdir -p '$REMOTE_DIR'"

echo "==> copying service files"
rsync -az --chmod=F600 \
  "$HERE/watcher.py" "$HERE/Dockerfile" "$HERE/docker-compose.yml" "$HERE/.env" \
  "$TARGET:$REMOTE_DIR/"

echo "==> building and starting"
ssh "$TARGET" "cd '$REMOTE_DIR' && docker compose up -d --build"

echo "==> waiting for the first check"
sleep 12
ssh "$TARGET" "cd '$REMOTE_DIR' && docker compose logs --tail 20"

cat <<DONE

Deployed to $TARGET:$REMOTE_DIR
  logs:    ssh $TARGET "cd $REMOTE_DIR && docker compose logs -f"
  restart: ssh $TARGET "cd $REMOTE_DIR && docker compose restart"
  stop:    ssh $TARGET "cd $REMOTE_DIR && docker compose down"

Now send /start to your Telegram bot so it learns which chat to notify.
DONE
