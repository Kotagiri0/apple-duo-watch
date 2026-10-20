#!/usr/bin/env bash
# A watcher on this Mac that covers what the others cannot, and only then.
#
#   deploy/fallback-mac.sh status   what each watcher is doing right now
#   deploy/fallback-mac.sh tick     start, switch or stop by the rules below
#   deploy/fallback-mac.sh stop     stop it now, whatever the rules say
#
# The rules, from the top. Exactly one watcher announces delivery at a time:
#
#   NanoPi, at home     answers                        -> this Mac stays off
#   server in Germany   stays silent while the NanoPi
#                       answers; takes delivery when   -> this Mac: pickup only
#                       the NanoPi stops                  (Apple answers the
#                                                         data-centre address
#                                                         with HTTP 541, so the
#                                                         server cannot see
#                                                         pickup; this address can)
#   neither answers                                    -> this Mac: everything
#
# "Answers" means the last successful Apple poll is newer than STALE_AFTER.
# A watcher that starts always begins from a fresh baseline, so it does not
# re-announce what the NanoPi already sent while this one was off.
#
# This Mac reaches Telegram directly, so TELEGRAM_PROXY is ignored here. It
# never pings the NanoPi's heartbeat: a second pinger would make a dead NanoPi
# look alive, which is the one thing the heartbeat exists to prevent.
#
# `tick` is what launchd calls every minute (deploy/com.appleduo.fallback.plist),
# but that job is not loaded by default: loading it is the owner's decision.
set -uo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
DIR=${FALLBACK_DIR:-$HOME/.apple-duo-watch/fallback}
ENV_FILE=${FALLBACK_ENV:-$HOME/.apple-duo-watch/fallback.env}
STALE=${STALE_AFTER:-300}
VPS=${VPS_HOST:-root@188.213.0.80}
VPS_KEY=${VPS_KEY:-$HOME/.ssh/duo_tunnel}
PIDFILE=$DIR/watcher.pid
MODEFILE=$DIR/mode
LOG=$DIR/fallback.log
STATE=$DIR/state.json
# Tests substitute the watcher and the probe; nothing else changes for them.
WATCHER=(python3 -I "$HERE/watcher.py")
if [[ -n ${WATCHER_CMD:-} ]]; then read -r -a WATCHER <<<"$WATCHER_CMD"; fi

mkdir -p "$DIR"
say() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" | tee -a "$LOG"; }

# Two facts from outside: when the NanoPi last reached the VPS, and when the
# German server last polled Apple. Either unreadable means "not known", and
# not knowing never starts anything.
probe() {
  if [[ -n ${PROBE_CMD:-} ]]; then eval "$PROBE_CMD"; return; fi
  ssh -p 22 -i "$VPS_KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 \
      "$VPS" 'bash -s' <<'REMOTE'
stat -c %Y /run/duo-watch/heartbeat
docker exec -i apple-duo-watch-standby python3 -I - <<'PY'
import json
print(json.load(open("/data/state.json")).get("checked_at", ""))
PY
REMOTE
}

# "<nanopi seconds> <german seconds>", or "? ?" when either is unknown.
ages() {
  local out
  out=$(probe 2>/dev/null) || { echo "? ?"; return; }
  python3 -I - "$out" <<'PY'
import datetime as dt, sys
lines = [line.strip() for line in sys.argv[1].splitlines() if line.strip()]
now = dt.datetime.now(dt.timezone.utc)
def age(value, parse):
    try:
        return str(max(0, int((now - parse(value)).total_seconds())))
    except (ValueError, TypeError, OverflowError):
        return "?"
def epoch(value):
    return dt.datetime.fromtimestamp(int(value), dt.timezone.utc)
def iso(value):
    when = dt.datetime.fromisoformat(value)
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
first = lines[0] if lines else ""
second = lines[1] if len(lines) > 1 else ""
print(age(first, epoch), age(second, iso))
PY
}

decide() {   # nanopi age, german age -> off | pickup | full | unknown
  local nanopi=$1 german=$2
  if [[ $nanopi == "?" || $german == "?" ]]; then echo unknown
  elif (( nanopi <= STALE )); then echo off
  elif (( german <= STALE )); then echo pickup
  else echo full
  fi
}

running() { [[ -s $PIDFILE ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

mode_now() {
  if running; then cat "$MODEFILE" 2>/dev/null || echo unknown; else echo none; fi
}

start() {   # pickup | full
  local mode=$1
  if ! grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$ENV_FILE" 2>/dev/null; then
    say "not starting the $mode watcher: $ENV_FILE has no TELEGRAM_BOT_TOKEN"
    return 1
  fi
  rm -f "$STATE"        # a fresh baseline: nothing the NanoPi sent is sent again
  (
    # Values are taken literally, as docker's env_file takes them, not as shell.
    while IFS= read -r line; do
      [[ $line =~ ^[A-Z_][A-Z0-9_]*= ]] && export "$line"
    done <"$ENV_FILE"
    unset TELEGRAM_PROXY HEALTHCHECK_URL
    export STATE_FILE="$STATE"
    if [[ $mode == pickup ]]; then export ONLY_KINDS=PICKUP; else unset ONLY_KINDS; fi
    exec "${WATCHER[@]}"
  ) >>"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  echo "$mode" >"$MODEFILE"
  say "started the $mode watcher (pid $(cat "$PIDFILE"))"
}

stop() {
  if running; then
    local pid mode
    pid=$(cat "$PIDFILE")
    mode=$(cat "$MODEFILE" 2>/dev/null || echo unknown)
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null
    say "stopped the $mode watcher (pid $pid)"
  fi
  rm -f "$PIDFILE" "$MODEFILE"
}

tick() {
  local nanopi german want now
  read -r nanopi german < <(ages)
  want=$(decide "$nanopi" "$german")
  now=$(mode_now)
  case $want in
    unknown)
      say "cannot see both the NanoPi (${nanopi} s) and the German server (${german} s); leaving things as they are"
      ;;
    off)
      if [[ $now != none ]]; then
        say "the NanoPi answers again (${nanopi} s ago); standing down"
        stop
      fi
      ;;
    pickup | full)
      if [[ $now == "$want" ]]; then return 0; fi
      if [[ $now != none ]]; then say "switching $now -> $want"; stop; fi
      start "$want"
      ;;
  esac
}

status() {
  local nanopi german want
  read -r nanopi german < <(ages)
  want=$(decide "$nanopi" "$german")
  local shown_nanopi shown_german
  shown_nanopi=$([[ $nanopi == "?" ]] && echo "нет связи" || echo "${nanopi} с назад")
  shown_german=$([[ $german == "?" ]] && echo "нет связи" || echo "${german} с назад")
  echo "NanoPi (дом):        последний удачный опрос — ${shown_nanopi}"
  echo "Сервер в Германии:   последний удачный опрос — ${shown_german}"
  echo "порог молчания:      ${STALE} с"
  echo "по правилам сейчас:  ${want}   (off — ничего, pickup — только самовывоз, full — всё)"
  if running; then
    echo "этот Mac:            работает, режим $(mode_now), pid $(cat "$PIDFILE")"
  else
    echo "этот Mac:            не работает"
  fi
}

case ${1:-status} in
  status) status ;;
  tick)   tick ;;
  stop)   stop ;;
  *) echo "usage: $0 status|tick|stop" >&2; exit 2 ;;
esac
