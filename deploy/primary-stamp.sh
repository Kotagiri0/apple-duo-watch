#!/bin/sh
# For the proxy VPS, as /usr/local/bin/primary-stamp.sh (chmod 755). Then point
# the standby at it in /etc/duo-standby.env:
#
#   PRIMARY_SSH=/usr/local/bin/primary-stamp.sh
#
# The standby used to read the NanoPi over SSH. The NanoPi has refused every
# key since 19 Oct 14:43 UTC, so the standby has been taking over alerts since
# 14:47 UTC and polling getUpdates against the NanoPi. Its heartbeat already
# reaches this VPS after every successful Apple poll, so the file's mtime is
# the same signal, read locally, with no SSH and no key to lose.
exec date -u -d "@$(stat -c %Y /run/duo-watch/heartbeat)" +%Y-%m-%dT%H:%M:%S+00:00
