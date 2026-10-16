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

## Run it

### Docker (recommended on a server)

```sh
cp .env.example .env     # then edit: add the bot token
docker compose up -d --build
docker compose logs -f
```

State survives restarts in the `duo-state` volume.

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
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `INTERVAL_SECONDS`, `CITY`,
`ALERT_BEFORE`, `ONLY_PARTS`, `STATE_FILE`. CLI flags of the same name win over
the environment.

## Polling rate

`INTERVAL_SECONDS=60` is the intended cadence. Two things to know:

- Apple rate-limits aggressive clients; one of its availability endpoints
  already answers `541` to plain scripted requests. Each tick adds up to 15%
  random jitter so the traffic does not look like a metronome, and failures back
  off exponentially up to 15 minutes rather than hammering through a block.
- Going below ~30 seconds raises the risk of the source IP being blocked, which
  makes the watcher blind. The service warns at startup if you do it anyway.

## Scope

No purchase, no checkout, no card details, no account actions — the service only
reads public availability endpoints and sends you a link.
