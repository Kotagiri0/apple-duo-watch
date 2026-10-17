# apple-duo-watch

Watches the Apple **United Arab Emirates** store for the iPhone Duo and tells you
on Telegram the moment anything moves — an earlier ship date, a configuration
becoming orderable again, or in-store pickup appearing.

It does **not** buy anything. It sends you a link that lands on the exact
configuration; you place and pay for the order yourself.

## Why it exists

iPhone Duo pre-orders opened on 16 October 2026 and the launch-day stock went in
minutes. In the UAE store all eight configurations stayed orderable, but with a
ship date in late January. When someone cancels, freed stock shows up as an
earlier ship date — usually for a few minutes. This service notices that within
one polling interval.

## What it watches

All eight UAE SKUs, via two public store endpoints:

| Part | Configuration | Price |
|---|---|---|
| MK244AH/A | 256GB Star White | 8 499 AED |
| MK254AH/A | 256GB Night Sky | 8 499 AED |
| MK264AH/A | 512GB Star White | 9 349 AED |
| MK274AH/A | 512GB Night Sky | 9 349 AED |
| MK284AH/A | 1TB Star White | 11 049 AED |
| MK294AH/A | 1TB Night Sky | 11 049 AED |
| MK2A4AH/A | 2TB Star White | 13 599 AED |
| MK2C4AH/A | 2TB Night Sky | 13 599 AED |

- `GET /ae/shop/delivery-message` — ship date and buyability per part
- `GET /ae/shop/retail/pickup-message` — pickup across the five UAE stores
  (Dubai Mall, Mall of the Emirates, Yas Mall, Al Jimi Mall, Al Maryah Island)

The last seen state is kept in a JSON file, so each run reports only what
changed. Findings are classified: `EARLIER`, `BUYABLE`, `TARGET`, `PICKUP`,
`CHANGED`.

## Telegram bot

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the
   prompts, copy the token it gives you.
2. Put the token in `.env` as `TELEGRAM_BOT_TOKEN`. Leave `TELEGRAM_CHAT_ID`
   empty.
3. Start the service, then send `/start` to your bot. The first chat to write in
   is adopted and saved to the state file — no need to look up a chat id.

Commands: `/status` (current ship dates for every configuration), `/help`.

The token is a credential: it lives in `.env`, which is gitignored. Do not
commit it, and rotate it via `/revoke` in BotFather if it leaks.

## Deployment layout

Two machines. The watcher runs at home, where Telegram is blocked, so the Bot
API calls are tunnelled through a VPS that can reach it. Apple is always
fetched directly from the watcher — only Telegram takes the detour.

```
  MacBook (dev)                     watcher host                    proxy host
  ~/pupazalupa                      NanoPi R4S                      VPS
                                    37.204.22.209                   188.213.0.80
  ┌──────────────┐                 ┌─────────────────────┐        ┌──────────────────┐
  │ git, deploy  │   ssh :2222     │ docker              │        │ tinyproxy        │
  │ script, .env │ ──────────────▶ │  apple-duo-watch    │        │ 127.0.0.1:8888   │
  └──────────────┘   duo_deploy    │                     │        └────────▲─────────┘
                                   │ telegram-tunnel     │  ssh :22        │
                                   │  172.17.0.1:8888 ───┼────────────────▶┘
                                   └──────┬──────────────┘  tunnel_to_vps
                                          │
                        apple.com/ae ◀────┤ direct, no proxy
                        ntfy.sh     ◀─────┘ direct, fallback channel
```

### Watcher host — NanoPi R4S

| | |
|---|---|
| Hostname | `nanopi-r4s` |
| OS | Armbian 26.8.1 (Ubuntu 26.04 "resolute"), aarch64 |
| Resources | 3.8 GB RAM, 55 GB free of 57 GB |
| Network | `37.204.22.209`, LAN `192.168.0.111` behind `192.168.0.1` on `end0` |
| SSH | port **2222**, `root`, key `~/.ssh/duo_deploy` |
| Docker | `docker.io` 29.1.3, `docker-compose-v2` 2.40.3, both enabled at boot |
| Service | container `apple-duo-watch`, `restart: unless-stopped` |
| State | volume `apple-duo-watch_duo-state` mounted at `/data/state.json` |
| Tunnel | `telegram-tunnel.service`, enabled, key `/root/.ssh/tunnel_to_vps` |
| Files | `/opt/apple-duo-watch/` — `watcher.py`, `Dockerfile`, `docker-compose.yml`, `.env` |

Docker came from the Ubuntu archive rather than `get.docker.com`: that script
aborts here because the Armbian mirror (`apt.armbian.com`) times out, which
makes its `apt-get update` fail.

### Proxy host — VPS

| | |
|---|---|
| Hostname | `qwertjklk.com` |
| OS | Ubuntu 24.04 LTS, x86_64 |
| Resources | 14.7 GB disk |
| Network | `188.213.0.80` |
| SSH | port **22**, `root`, key `~/.ssh/duo_tunnel` |
| Service | `tinyproxy` on `127.0.0.1:8888`, enabled, loopback only |
| Purpose | reaches `api.telegram.org`, which the watcher host cannot |

Its `authorized_keys` holds three entries: the admin key from the MacBook, the
MacBook's own default key, and the watcher's key restricted to the single
forward it needs.

Nothing on this host listens publicly except SSH — tinyproxy is bound to
loopback, so the proxy is reachable only from inside the tunnel.

### Keys

| Key | From | To | Scope |
|---|---|---|---|
| `~/.ssh/duo_deploy` | MacBook | NanoPi :2222 | full root, deploys |
| `~/.ssh/duo_tunnel` | MacBook | VPS :22 | full root, admin |
| `~/.ssh/id_ed25519` | MacBook | VPS :22 | full root, your own login |
| `/root/.ssh/tunnel_to_vps` | NanoPi | VPS :22 | `permitopen` 127.0.0.1:8888, no shell |

## Run it

### Docker (recommended on a server)

```sh
cp .env.example .env     # then edit: add the bot token
docker compose up -d --build
docker compose logs -f
```

State survives restarts in the `duo-state` volume.

### On a server over SSH

From this checkout, with `.env` filled in:

```sh
./deploy/remote-install.sh -p 2222 -i ~/.ssh/duo_deploy root@your-host
```

`-p` is the SSH port (default 22) and `-i` the key to authenticate with. The
script copies `watcher.py`, the Dockerfile, the compose file and `.env` to
`/opt/apple-duo-watch`, installs Docker if the host has none, and runs
`docker compose up -d --build`. Re-run the same command to deploy an update.
Nothing is cloned on the server, so it needs no GitHub credentials.

The key has to be authorized on the host first — once, from your machine,
typing the server password yourself:

```sh
ssh-copy-id -p 2222 -i ~/.ssh/duo_deploy.pub root@your-host
```

### systemd (no Docker)

```sh
sudo useradd --system --home /opt/apple-duo-watch duo
sudo mkdir -p /opt/apple-duo-watch
sudo cp watcher.py .env /opt/apple-duo-watch/
sudo cp deploy/apple-duo-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now apple-duo-watch
journalctl -u apple-duo-watch -f
```

### Locally on macOS

`deploy/com.appleduo.watch.plist` is a launchd agent — it keeps running when the
Claude app is closed, unlike a scheduled task in the app.

```sh
cp deploy/com.appleduo.watch.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.appleduo.watch.plist
```

### One-off checks

```sh
python3 -I watcher.py --show     # table of all eight SKUs right now
python3 -I watcher.py --once     # one pass, report changes, exit
```

## Configuration

Every setting is an environment variable, documented in `.env.example`:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_PROXY`, `NTFY_URL`,
`NTFY_TOKEN`, `INTERVAL_SECONDS`, `PICKUP_EVERY`, `CITY`, `ALERT_BEFORE`,
`ONLY_PARTS`, `STATE_FILE`. CLI flags of the same name win over the
environment.

## When Telegram is blocked on the watcher's network

The deployment this repo was written for sits on a Russian home ISP, where
`api.telegram.org`, `core.telegram.org` and `t.me` do not accept TCP at all
(DNS resolves fine, so the filtering is by IP), while Apple, GitHub and
everything else is reachable. Two pieces handle that:

- `TELEGRAM_PROXY` sends **only** the Bot API calls through an HTTP proxy.
  Apple is always fetched directly, so nothing else rides the tunnel.
- `deploy/telegram-tunnel.service` keeps an SSH tunnel to a VPS outside that
  network, exposing the VPS's loopback-only tinyproxy on the docker bridge
  (`172.17.0.1:8888`) where the container can reach it. The transport is SSH,
  so a DPI box sees a connection to the VPS and nothing about Telegram.

On the VPS, tinyproxy listens on `127.0.0.1:8888` only, and the tunnel key is
restricted in `authorized_keys` to that one forward with no shell:

```
restrict,port-forwarding,permitopen="127.0.0.1:8888",command="/bin/false" ssh-ed25519 …
```

`NTFY_URL` adds a second channel for when the tunnel itself is down. A change
that reaches no channel is not marked as seen, so the next check reports it
again rather than losing it — but a second channel still gets the alert out
sooner.

## Polling rate

The deployment runs at `INTERVAL_SECONDS=10`. That is deliberately aggressive,
and the trade-off is worth stating plainly: a freed pre-order slot can be gone
within a minute, so a short interval is the point — but Apple rate-limits by
source IP, and a block makes the watcher blind, which is strictly worse than a
slower poll. Four things keep that risk down:

- Each cycle subtracts its own request time from the wait, so the period is the
  interval itself rather than the interval plus however long Apple took.
- Up to 15% random jitter per tick, so the traffic does not look like a
  metronome.
- The in-store pickup lookup is a second request, so it runs every Nth cycle —
  about once a minute at any interval, via `PICKUP_EVERY`. At 10 seconds that
  is one request per cycle instead of two.
- Failures back off exponentially to 15 minutes instead of hammering through a
  block, and the service warns at startup whenever the interval is under 30
  seconds.

One of Apple's availability endpoints already answers `541` to plain scripted
requests, so the limits are real rather than theoretical. If the logs start
showing `apple unreachable` in runs, raise the interval.

## Scope

No purchase, no checkout, no card details, no account actions — the service only
reads public availability endpoints and sends you a link.
