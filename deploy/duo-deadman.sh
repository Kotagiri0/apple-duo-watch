#!/usr/bin/env bash
# Shout when the watcher stops saying it is alive.
#
# Runs on the proxy VPS, on a systemd timer, outside the watcher's machine and
# network. The watcher's own alarm cannot report that the watcher died: a dead
# process, a stopped container and a NanoPi with no power all look exactly
# like "nothing changed" from the inside. This notices the silence instead.
#
# Settings come from /etc/duo-deadman.env:
#   NTFY_URL            topic to post to — the same one the watcher uses
#   NTFY_TOKEN          bearer token, if that topic needs one
#   TELEGRAM_BOT_TOKEN  optional: this machine reaches api.telegram.org
#   TELEGRAM_CHAT_ID    chat for the above
#   STALE_AFTER         seconds of silence before alerting (default 900)
#   HEARTBEAT_FILE      file the sink rewrites (/run/duo-watch/heartbeat)
set -uo pipefail

ENV_FILE=${ENV_FILE:-/etc/duo-deadman.env}
# shellcheck source=/dev/null
[[ -f $ENV_FILE ]] && . "$ENV_FILE"

HEARTBEAT_FILE=${HEARTBEAT_FILE:-/run/duo-watch/heartbeat}
STALE_AFTER=${STALE_AFTER:-900}
FLAG=${FLAG:-/var/lib/duo-deadman/alerted}
NTFY_URL=${NTFY_URL:-}
NTFY_TOKEN=${NTFY_TOKEN:-}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-}
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID:-}

mkdir -p "$(dirname "$FLAG")"

# One message per incident, not one per timer tick: the flag file is what
# remembers that this outage was already reported.
say() {
  local text=$1 delivered=0
  local -a auth=()
  if [[ -n $NTFY_URL ]]; then
    [[ -n $NTFY_TOKEN ]] && auth=(-H "Authorization: Bearer $NTFY_TOKEN")
    if curl -fsS --max-time 20 "${auth[@]}" \
         -H "Title: apple-duo-watch deadman" \
         -H "Content-Type: text/plain; charset=utf-8" \
         --data-binary "$text" "$NTFY_URL" >/dev/null; then
      delivered=1
    fi
  fi
  if [[ -n $TELEGRAM_BOT_TOKEN && -n $TELEGRAM_CHAT_ID ]]; then
    if curl -fsS --max-time 20 \
         -d "chat_id=$TELEGRAM_CHAT_ID" \
         -d "disable_web_page_preview=true" \
         --data-urlencode "text=$text" \
         "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
         >/dev/null; then
      delivered=1
    fi
  fi
  if (( delivered )); then
    logger -t duo-deadman "alert delivered"
  else
    # Nothing took it, so the flag stays unset and the next tick tries again.
    logger -t duo-deadman "no channel accepted the alert"
  fi
  (( delivered ))
}

now=$(date +%s)
if [[ -f $HEARTBEAT_FILE ]]; then
  beat=$(stat -c %Y "$HEARTBEAT_FILE")
  silent=$(( now - beat ))
  last=$(date -d "@$beat" --iso-8601=seconds)
else
  # No file at all: either nothing has ever beaten, or the sink is down.
  silent=$(( STALE_AFTER + 1 ))
  last="никогда (файла $HEARTBEAT_FILE нет)"
fi

if (( silent > STALE_AFTER )); then
  [[ -f $FLAG ]] && exit 0
  say "🚨 Наблюдатель замолчал

Последний heartbeat: $last
Тишина: $(( silent / 60 )) мин (порог $(( STALE_AFTER / 60 )) мин).

Это говорит VPS, а не сам наблюдатель, — значит он не просто ничего не нашёл,
а не отвечает вообще: упал контейнер, выключился NanoPi или не поднимается
туннель. Проверь docker compose ps и systemctl status telegram-tunnel." \
    && touch "$FLAG"
elif [[ -f $FLAG ]]; then
  say "✅ Наблюдатель снова на связи (heartbeat $last)." && rm -f "$FLAG"
fi
