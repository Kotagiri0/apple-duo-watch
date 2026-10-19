#!/usr/bin/env bash
# Sign the current commit and hand it to the VPS, from which the NanoPi pulls
# it on its own (deploy/duo-update.sh, every five minutes).
#
#   ./deploy/publish.sh [-p PORT] [-i IDENTITY] [-k SIGNING_KEY] user@vps
#
# A release is exactly a commit: the working tree must be clean, so what is
# signed is what is in git. The signing key never leaves this machine; the
# VPS only ever sees files and a signature it cannot forge.
set -euo pipefail

PORT=${SSH_PORT:-22}
IDENTITY=${SSH_IDENTITY:-}
KEY=${SIGNING_KEY:-$HOME/.ssh/duo_release}
RELEASES=/var/lib/duo-releases
LIVE=/var/lib/duo-release
KEEP=5

while getopts ":p:i:k:" opt; do
  case $opt in
    p) PORT=$OPTARG ;;
    i) IDENTITY=$OPTARG ;;
    k) KEY=$OPTARG ;;
    *) echo "usage: $0 [-p PORT] [-i IDENTITY] [-k SIGNING_KEY] user@vps" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
TARGET=${1:?usage: $0 [-p PORT] [-i IDENTITY] [-k SIGNING_KEY] user@vps}
HERE=$(cd "$(dirname "$0")/.." && pwd)

SSH_OPTS=(-p "$PORT" -o ConnectTimeout=20)
[[ -n $IDENTITY ]] && SSH_OPTS+=(-i "$IDENTITY" -o IdentitiesOnly=yes)

cd "$HERE"
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: uncommitted changes — commit first, so the release is exactly a commit" >&2
  exit 1
fi
[[ -f $KEY ]] || { echo "error: signing key $KEY is missing" >&2; exit 1; }

FILES=(watcher.py Dockerfile docker-compose.yml
       deploy/duo-update.sh deploy/duo-update.service deploy/duo-update.timer)
version=$(date +%s)
commit=$(git rev-parse --short HEAD)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

{
  echo "duo-release 1"
  echo "version $version"
  echo "commit $commit"
  for f in "${FILES[@]}"; do
    cp "$f" "$stage/"
    echo "sha256 $(shasum -a 256 "$f" | cut -d' ' -f1) $(basename "$f")"
  done
} > "$stage/MANIFEST"
ssh-keygen -q -Y sign -f "$KEY" -n duo-release "$stage/MANIFEST"
# Check our own signature against the very file the NanoPi trusts, so a
# release that the box would refuse is caught here rather than in 5 minutes.
ssh-keygen -Y verify -f deploy/release-signers -I duo-release -n duo-release \
  -s "$stage/MANIFEST.sig" < "$stage/MANIFEST" >/dev/null

echo "==> publishing $commit as release $version to $TARGET"
ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p '$RELEASES/$version.partial'"
# No --chmod here: the rsync macOS ships with does not support it.
rsync -a -e "ssh ${SSH_OPTS[*]}" "$stage/" "$TARGET:$RELEASES/$version.partial/"
# The flip is a rename of a symlink, which is atomic: the NanoPi sees either
# the old release or the new one, never half of one.
ssh "${SSH_OPTS[@]}" "$TARGET" "set -e
cd '$RELEASES'
chmod 755 '$version.partial' && chmod 644 '$version.partial'/*
mv '$version.partial' '$version'
ln -sfn '$RELEASES/$version' '$LIVE.tmp'
mv -T '$LIVE.tmp' '$LIVE'
ls -1d [0-9]* | sort -n | head -n -$KEEP | xargs -r rm -rf"
echo "==> live: $(ssh "${SSH_OPTS[@]}" "$TARGET" "readlink '$LIVE'")"
echo "The NanoPi picks it up within five minutes and rolls it back by itself"
echo "if the new build does not complete a check: journalctl -t duo-update"
