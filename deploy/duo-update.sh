#!/usr/bin/env bash
# Pull a signed release of the watcher from the VPS and roll it out.
#
# Runs on the NanoPi from duo-update.timer. Deploying used to mean SSHing into
# this box and pushing files — and on 19 Oct 2026 it stopped accepting every
# key at once, so a finished fix could not be shipped although the watcher
# itself kept running. Now the box fetches its own updates through the tunnel
# it already keeps open to the VPS, and needs no inbound access at all.
#
# The VPS only serves files. What gets installed is decided by a signature
# made with a key that lives on the MacBook alone, so a compromised VPS can at
# worst stop updates — it cannot push code here. Each release carries a
# version that only grows, so an old signed release cannot be replayed to
# roll the box back to a known bug.
#
# A new release has to complete one successful check of the Apple store
# within HEALTH_WAIT seconds, or the previous files are put back. A release
# that failed that way is not retried; publishing a newer one is.
set -euo pipefail

SRC=${SRC:-http://172.17.0.1:8899/release}
DIR=${DIR:-/opt/apple-duo-watch}
SIGNERS=${SIGNERS:-/etc/duo-update/allowed_signers}
STATE=${STATE:-/var/lib/duo-update}
DOCKER=${DOCKER:-docker}
CONTAINER=${CONTAINER:-apple-duo-watch}
HEALTH_WAIT=${HEALTH_WAIT:-120}
DRY_RUN=${DRY_RUN:-0}
# What this updater rolls out. It never replaces itself: a broken updater
# would end all future updates, so changes to it go through SSH deliberately.
APPLY=(watcher.py Dockerfile docker-compose.yml)
KNOWN='^(watcher\.py|Dockerfile|docker-compose\.yml|duo-update\.sh|duo-update\.service|duo-update\.timer)$'

log() { echo "duo-update: $*"; logger -t duo-update -- "$*" 2>/dev/null || true; }

# Only a rollback is worth a notification: a release going live is expected,
# a release that failed means the owner is running older code than intended.
notify() {
  local url
  url=$(grep -E '^NTFY_URL=' "$DIR/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)
  [[ -n $url ]] || return 0
  curl -fsS --max-time 20 -H "Title: apple-duo-watch update" \
       -H "Content-Type: text/plain; charset=utf-8" \
       --data-binary "$1" "$url" >/dev/null 2>&1 || true
}

mkdir -p "$STATE"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
fetch() { curl -fsS --max-time 30 -o "$stage/$1" "$SRC/$1"; }

# No release endpoint is not an error worth a failed unit every five minutes:
# the tunnel may be down, and the watchdog already reports that.
if ! fetch MANIFEST || ! fetch MANIFEST.sig; then
  log "release endpoint unreachable; nothing to do"
  exit 0
fi

# 1. Nothing in the manifest is believed until its signature checks out.
if ! ssh-keygen -Y verify -f "$SIGNERS" -I duo-release -n duo-release \
       -s "$stage/MANIFEST.sig" < "$stage/MANIFEST" >/dev/null 2>&1; then
  log "REFUSED: the manifest signature does not verify"
  exit 1
fi

version=$(awk '$1 == "version" { print $2 }' "$stage/MANIFEST")
commit=$(awk '$1 == "commit" { print $2 }' "$stage/MANIFEST")
if ! [[ $version =~ ^[0-9]+$ ]]; then
  log "REFUSED: the manifest carries no usable version"
  exit 1
fi
applied=$(cat "$STATE/applied" 2>/dev/null || echo 0)
failed=$(cat "$STATE/failed" 2>/dev/null || echo 0)
# 2. Only forward. This is also what makes a replayed old release harmless.
if (( version <= applied )); then
  exit 0
fi
if (( version == failed )); then
  exit 0
fi

# 3. Every file must be one we know and must hash to what the manifest says.
while read -r tag sum name; do
  [[ $tag == sha256 ]] || continue
  if ! [[ $name =~ $KNOWN ]]; then
    log "REFUSED: the manifest names an unexpected file: $name"
    exit 1
  fi
  fetch "$name"
  if ! echo "$sum  $stage/$name" | sha256sum -c --quiet - >/dev/null 2>&1; then
    log "REFUSED: $name does not match its hash in the signed manifest"
    exit 1
  fi
done < "$stage/MANIFEST"
for f in "${APPLY[@]}"; do
  if [[ ! -f $stage/$f ]]; then
    log "REFUSED: the release is missing $f"
    exit 1
  fi
done

if (( DRY_RUN )); then
  log "dry run: release $version ($commit) verified, not installed"
  exit 0
fi

checked_at() {
  "$DOCKER" exec "$CONTAINER" python3 -I -c \
    'import json; print(json.load(open("/data/state.json")).get("checked_at", ""))' \
    2>/dev/null || true
}

# 4. Swap in, keeping what was there for the rollback.
backup="$STATE/previous"
rm -rf "$backup"
mkdir -p "$backup"
for f in "${APPLY[@]}"; do
  [[ -f $DIR/$f ]] && cp -a "$DIR/$f" "$backup/$f"
done
before=$(checked_at)
for f in "${APPLY[@]}"; do
  install -m 644 "$stage/$f" "$DIR/$f.new"
  mv -f "$DIR/$f.new" "$DIR/$f"
done
log "installing release $version ($commit)"
(cd "$DIR" && "$DOCKER" compose up -d --build >/dev/null 2>&1) || true

# 5. Live only once it has actually looked at the store.
deadline=$(( $(date +%s) + HEALTH_WAIT ))
while (( $(date +%s) < deadline )); do
  now=$(checked_at)
  if [[ -n $now && $now != "$before" ]]; then
    echo "$version" > "$STATE/applied"
    log "release $version ($commit) is live; first check at $now"
    exit 0
  fi
  sleep 5
done

for f in "${APPLY[@]}"; do
  [[ -f $backup/$f ]] && cp -a "$backup/$f" "$DIR/$f"
done
(cd "$DIR" && "$DOCKER" compose up -d --build >/dev/null 2>&1) || true
echo "$version" > "$STATE/failed"
log "ROLLED BACK release $version ($commit): no successful check within ${HEALTH_WAIT}s"
notify "⚠️ Обновление $commit откатено: новая версия за ${HEALTH_WAIT} с не сделала ни одной удачной проверки магазина. Работает прежняя версия."
exit 1
