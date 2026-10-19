#!/usr/bin/env bash
# One-time repair of the NanoPi, run by its owner with the root password:
#
#   ssh -p 2222 root@37.204.22.209 'bash -s' < deploy/nanopi-bootstrap.sh
#
# On 19 Oct 2026 the box stopped accepting every SSH key at once — the deploy
# key and the VPS's restricted key alike — while the watcher kept running. A
# fix was ready and could not be shipped. This script, in order:
#
#   1. shows why: ownership and permissions on the way to authorized_keys
#      (sshd silently ignores the file if any of them is too open), the
#      file's contents, sshd's effective settings and its recent complaints;
#   2. puts the deploy key back, with permissions sshd accepts;
#   3. installs the self-updater from the signed release on the VPS, so the
#      next fix ships without anyone needing to log in here at all;
#   4. runs the first update, which also ships the pending fixes.
#
# It changes nothing else: not the watcher's .env or state, not sshd's
# configuration, not anybody else's key. Re-running it is harmless.
set -euo pipefail

DEPLOY_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPilYpYgTwVWZXqmXsjsSVyjQUUC4NNONeXktQkyIzTC apple-duo-watch deploy key (claude)'
RELEASE_SIGNER='duo-release namespaces="duo-release" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIF404TGKRw6dzz1iY3V1Axjq7Yicp9fqNXoukuwZ7VbP'
SRC=${SRC:-http://172.17.0.1:8899/release}
AK=/root/.ssh/authorized_keys

echo "== 1. почему ключи перестали приниматься"
echo "   (резерв на VPS последний раз достучался 2026-10-19 14:42:48 UTC,"
echo "    с 14:43:51 UTC одни отказы: ищи, что менялось в эту минуту)"
for p in / /root /root/.ssh "$AK"; do
  stat -c '   %A %U:%G  изменён %y  %n' "$p" 2>/dev/null || echo "   (нет) $p"
done
for p in / /root /root/.ssh; do
  if [ -d "$p" ] && [ -n "$(find "$p" -maxdepth 0 -perm /022 2>/dev/null)" ]; then
    echo "   !! $p доступен на запись группе или всем — sshd из-за этого игнорирует ключи"
  fi
done
if [ -f "$AK" ]; then
  if [ -n "$(find "$AK" -maxdepth 0 -perm /022 2>/dev/null)" ]; then
    echo "   !! authorized_keys доступен на запись группе или всем — sshd его игнорирует"
  fi
  echo "   ключей в authorized_keys: $(grep -cE '^[^#]*ssh-' "$AK" || true)"
  grep -oE '(ssh-[a-z0-9-]+ [A-Za-z0-9+/=]{12})[^ ]* *.*$' "$AK" 2>/dev/null \
    | sed -E 's/(ssh-[a-z0-9-]+ [A-Za-z0-9+\/=]{12})[A-Za-z0-9+\/=]*/\1…/; s/^/     /' || true
else
  echo "   !! authorized_keys отсутствует"
fi
echo "   действующие настройки sshd:"
sshd -T 2>/dev/null | grep -Ei '^(permitrootlogin|pubkeyauthentication|authorizedkeysfile|strictmodes|passwordauthentication) ' | sed 's/^/     /' || true
echo "   последние жалобы sshd:"
journalctl -u ssh -u sshd --since "-4 days" --no-pager 2>/dev/null \
  | grep -Ei 'bad ownership|bad modes|authentication refused|could not open|no such file' \
  | tail -5 | sed 's/^/     /' || true
echo "   что происходило в системе в ту минуту:"
journalctl --since "2026-10-19 14:40:00 UTC" --until "2026-10-19 14:46:00 UTC" \
  --no-pager -o short-iso 2>/dev/null | grep -vE 'CRON|session (opened|closed)' \
  | tail -15 | sed 's/^/     /' || true

echo "== 2. возвращаю деплой-ключ"
install -d -m 700 -o root -g root /root/.ssh
touch "$AK"
chown root:root /root /root/.ssh "$AK"
chmod go-w / /root
chmod 700 /root/.ssh
chmod 600 "$AK"
key_body=$(echo "$DEPLOY_KEY" | awk '{print $2}')
if grep -qF "$key_body" "$AK"; then
  echo "   ключ уже на месте; права исправлены"
else
  echo "$DEPLOY_KEY" >> "$AK"
  echo "   ключ добавлен"
fi
if ! grep -q 'vps->nanopi read primary state' "$AK"; then
  echo "   (ограниченного ключа VPS в файле нет. Его я не возвращаю: это выдача"
  echo "    доступа, решать владельцу. Как вернуть, если решишь, — в README.)"
fi

echo "== 3. ставлю самообновление из подписанного релиза"
command -v ssh-keygen >/dev/null && command -v sha256sum >/dev/null && command -v curl >/dev/null
install -d -m 755 /etc/duo-update /var/lib/duo-update
printf '%s\n' "$RELEASE_SIGNER" > /etc/duo-update/allowed_signers
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
curl -fsS --max-time 30 -o "$stage/MANIFEST" "$SRC/MANIFEST"
curl -fsS --max-time 30 -o "$stage/MANIFEST.sig" "$SRC/MANIFEST.sig"
ssh-keygen -Y verify -f /etc/duo-update/allowed_signers -I duo-release -n duo-release \
  -s "$stage/MANIFEST.sig" < "$stage/MANIFEST" >/dev/null
echo "   подпись релиза $(awk '$1=="commit"{print $2}' "$stage/MANIFEST") проверена"
for name in duo-update.sh duo-update.service duo-update.timer; do
  sum=$(awk -v n="$name" '$1=="sha256" && $3==n {print $2}' "$stage/MANIFEST")
  [ -n "$sum" ] || { echo "   !! $name нет в релизе"; exit 1; }
  curl -fsS --max-time 30 -o "$stage/$name" "$SRC/$name"
  echo "$sum  $stage/$name" | sha256sum -c --quiet -
done
install -m 755 "$stage/duo-update.sh" /usr/local/sbin/duo-update
install -m 644 "$stage/duo-update.service" "$stage/duo-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now duo-update.timer >/dev/null 2>&1
echo "   таймер: $(systemctl is-active duo-update.timer)"

echo "== 4. первое обновление прямо сейчас"
/usr/local/sbin/duo-update </dev/null || true
echo "   релиз в работе: $(cat /var/lib/duo-update/applied 2>/dev/null || echo '—')"

echo
echo "Готово. Проверка с MacBook: ssh -p 2222 -i ~/.ssh/duo_deploy root@37.204.22.209 hostname"
