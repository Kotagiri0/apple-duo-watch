#!/usr/bin/env python3
"""Watch the Apple UAE store for iPhone Duo availability and earlier ship dates.

Runs as a one-shot check (--once) or as a long-running service that polls on a
fixed interval and reports to Telegram. Standard library only.

Configuration comes from the environment (see .env.example):

  TELEGRAM_BOT_TOKEN   bot token from @BotFather; without it alerts only go to stdout
  TELEGRAM_CHAT_ID     chat to notify; if unset, the first /start sender is adopted
  INTERVAL_SECONDS     seconds between checks (default 60)
  CITY                 city for in-store pickup lookups (default Dubai, "" to skip)
  ALERT_BEFORE         YYYY-MM-DD; always alert while any SKU ships on or before it
  STATE_FILE           where to keep the last seen state
  ONLY_PARTS           comma-separated part numbers to narrow the watch
"""
import argparse
import datetime as dt
import json
import logging
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STORE = "ae"
BASE = f"https://www.apple.com/{STORE}/shop"
CONFIG_URL = f"{BASE}/buy-iphone/iphone-duo"

# iPhone Duo SKUs in the UAE store, cheapest first (prices in AED, Oct 2026).
ALL_PARTS = {
    "MK244AH/A": ("256GB Star White", 8499),
    "MK254AH/A": ("256GB Night Sky", 8499),
    "MK264AH/A": ("512GB Star White", 9349),
    "MK274AH/A": ("512GB Night Sky", 9349),
    "MK284AH/A": ("1TB Star White", 11049),
    "MK294AH/A": ("1TB Night Sky", 11049),
    "MK2A4AH/A": ("2TB Star White", 13599),
    "MK2C4AH/A": ("2TB Night Sky", 13599),
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

log = logging.getLogger("duo")


class AppleUnreachable(RuntimeError):
    """Apple did not answer, or answered with something we cannot parse."""


# ---------------------------------------------------------------- Apple store

def _get_json(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _shop_json(path, params):
    qs = urllib.parse.urlencode(params, safe="/.")
    return _get_json(f"{BASE}/{path}?{qs}", headers={
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "en-AE,en;q=0.9",
        "Referer": CONFIG_URL,
    })


def parse_date(text, today):
    """'Ships Jan 22' / 'Delivers Oct 23' -> date. The year is inferred forward."""
    m = re.search(r"\b([A-Z][a-z]{2})\s+(\d{1,2})\b", text or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1))
    if not mon:
        return None
    year = today.year if mon >= today.month else today.year + 1
    try:
        return dt.date(year, mon, int(m.group(2)))
    except ValueError:
        return None


def fetch_delivery(parts, today):
    params = {"little": "false"}
    for i, pn in enumerate(parts):
        params[f"parts.{i}"] = pn
    try:
        data = _shop_json("delivery-message", params)
        msgs = data["body"]["content"]["deliveryMessage"]
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            KeyError, TimeoutError) as e:
        raise AppleUnreachable(str(e)) from e

    out = {}
    for pn in parts:
        node = msgs.get(pn)
        if not isinstance(node, dict) or "regular" not in node:
            continue
        r = node["regular"]
        label = " / ".join(
            m.get("displayName", "") for m in r.get("deliveryOptionMessages", [])
            if m.get("displayName")) or "(no message)"
        date = parse_date(label, today)
        out[pn] = {
            "label": label,
            "buyable": bool(r.get("isBuyable")),
            "reason": r.get("buyability", {}).get("reason"),
            "date": date.isoformat() if date else None,
        }
    if not out:
        raise AppleUnreachable("no SKU data in the delivery-message response")
    return out


def fetch_pickup(parts, city):
    """Stores where a SKU can be picked up today. Never fatal: pickup is a bonus."""
    if not city:
        return {}
    params = {"location": city}
    for i, pn in enumerate(parts):
        params[f"parts.{i}"] = pn
    try:
        data = _shop_json("retail/pickup-message", params)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError) as e:
        log.warning("pickup lookup failed: %s", e)
        return {}

    out = {}
    for st in data.get("body", {}).get("stores") or []:
        name = st.get("storeName") or st.get("storeNumber")
        for pn, info in (st.get("partsAvailability") or {}).items():
            if pn not in parts:
                continue
            status = (info.get("pickupDisplay") or "").lower()
            if status and status != "unavailable":
                quote = (info.get("pickupSearchQuote")
                         or info.get("messageTypes", {})
                            .get("regular", {}).get("storePickupQuote")
                         or status)
                out.setdefault(pn, []).append(f"{name}: {quote}")
    return out


# ------------------------------------------------------------------ diffing

def link_for(pn):
    """Redirects straight to this exact capacity/colour configuration."""
    return f"{BASE}/product/{pn}"


def diff(previous, current, pickup, parts, threshold):
    """Return the list of findings worth waking someone up for."""
    events = []
    for pn, (name, price) in parts.items():
        cur = current.get(pn)
        if not cur:
            continue
        old = previous.get(pn) or {}
        link = link_for(pn)
        old_date = dt.date.fromisoformat(old["date"]) if old.get("date") else None
        new_date = dt.date.fromisoformat(cur["date"]) if cur.get("date") else None

        if old:
            if new_date and (old_date is None or new_date < old_date):
                events.append(("EARLIER", pn, name, price,
                               f"{old.get('label')} -> {cur['label']}", link))
            elif cur["buyable"] and not old.get("buyable", True):
                events.append(("BUYABLE", pn, name, price, cur["label"], link))
            elif cur["label"] != old.get("label"):
                events.append(("CHANGED", pn, name, price,
                               f"{old.get('label')} -> {cur['label']}", link))

        if threshold and new_date and new_date <= threshold and cur["buyable"]:
            events.append(("TARGET", pn, name, price,
                           f"{cur['label']} (on or before {threshold})", link))

        new_pickup = sorted(pickup.get(pn, []))
        cur["pickup"] = new_pickup
        if old and new_pickup and new_pickup != sorted(old.get("pickup", [])):
            events.append(("PICKUP", pn, name, price, "; ".join(new_pickup), link))
    return events


def format_event(ev):
    kind, _pn, name, price, detail, link = ev
    return f"{kind} {name} ({price} AED): {detail}  {link}"


# -------------------------------------------------------------------- state

def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# ----------------------------------------------------------------- telegram

class Telegram:
    """Minimal Bot API client: push alerts out, answer a few commands."""

    def __init__(self, token, chat_id=None):
        self.token = token
        self.chat_id = chat_id
        self.offset = None

    @property
    def enabled(self):
        return bool(self.token)

    def _call(self, method, **params):
        if not self.token:
            return None
        url = TELEGRAM_API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}).encode()
        try:
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError) as e:
            log.warning("telegram %s failed: %s", method, e)
            return None

    def send(self, text):
        if not self.chat_id:
            log.warning("no TELEGRAM_CHAT_ID yet; send /start to the bot")
            return False
        res = self._call("sendMessage", chat_id=self.chat_id, text=text,
                         parse_mode="HTML", disable_web_page_preview="true")
        return bool(res and res.get("ok"))

    def poll_commands(self, timeout=0):
        """Return [(chat_id, text)] for new messages. Long-polls when timeout>0."""
        res = self._call("getUpdates", offset=self.offset, timeout=timeout,
                         allowed_updates=json.dumps(["message"]))
        if not res or not res.get("ok"):
            return []
        out = []
        for upd in res.get("result", []):
            self.offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            text = (msg.get("text") or "").strip()
            if chat and text:
                out.append((chat, text))
        return out


# ------------------------------------------------------------------ service

def render_status(current, parts, pickup, checked_at):
    lines = [f"<b>iPhone Duo — Apple UAE</b>  <i>{checked_at}</i>"]
    for pn, (name, price) in parts.items():
        cur = current.get(pn)
        if not cur:
            lines.append(f"• {name} — нет данных")
            continue
        mark = "✅" if cur["buyable"] else "⛔️"
        pk = pickup.get(pn)
        tail = f"  📦 {'; '.join(pk)}" if pk else ""
        lines.append(f'{mark} <a href="{link_for(pn)}">{name}</a> — '
                     f'{cur["label"]}, {price} AED{tail}')
    return "\n".join(lines)


def check_once(cfg, tg, announce=True):
    """One pass. Returns (events, current, pickup). Raises AppleUnreachable."""
    today = dt.date.today()
    current = fetch_delivery(cfg.parts, today)
    pickup = fetch_pickup(cfg.parts, cfg.city)

    state = load_state(cfg.state_file)
    previous = state.get("parts", {})
    events = diff(previous, current, pickup, cfg.parts, cfg.threshold)

    checked_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    state["parts"] = current
    state["checked_at"] = checked_at
    if tg.chat_id:
        state["chat_id"] = tg.chat_id
    save_state(cfg.state_file, state)

    if not previous:
        best = min((p["date"] for p in current.values() if p.get("date")),
                   default=None)
        log.info("baseline recorded for %d SKUs, earliest ship date %s",
                 len(current), best)
        if announce and tg.enabled:
            tg.send("👀 Слежу за iPhone Duo в Apple UAE.\n"
                    + render_status(current, cfg.parts, pickup, checked_at)
                    + f"\n\nПроверка каждые {cfg.interval} с. "
                      "Напишу, как только отправка станет раньше.")
        return [], current, pickup

    for ev in events:
        log.info("%s", format_event(ev))

    if events and announce and tg.enabled:
        head = "🔔 <b>iPhone Duo — изменение в Apple UAE</b>"
        body = []
        for kind, pn, name, price, detail, link in events:
            icon = {"EARLIER": "⏩", "BUYABLE": "🟢", "TARGET": "🎯",
                    "PICKUP": "🏬", "CHANGED": "ℹ️"}.get(kind, "•")
            body.append(f'{icon} <a href="{link}">{name}</a> — {detail}'
                        f'\n    {price} AED')
        tg.send(head + "\n" + "\n".join(body)
                + "\n\nОформление и оплату подтверждаешь сам.")
    return events, current, pickup


def handle_commands(cfg, tg, last):
    """Answer /start, /status, /help. Adopts the first chat if none configured."""
    for chat_id, text in tg.poll_commands():
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        if tg.chat_id is None:
            tg.chat_id = chat_id
            state = load_state(cfg.state_file)
            state["chat_id"] = chat_id
            save_state(cfg.state_file, state)
            log.info("adopted chat_id %s", chat_id)
        if chat_id != tg.chat_id:
            continue
        if cmd in ("start", "status"):
            current, pickup, checked_at = last
            if current:
                tg.send(render_status(current, cfg.parts, pickup, checked_at))
            else:
                tg.send("Ещё не делал первую проверку, подожди минуту.")
        elif cmd == "help":
            tg.send("/status — текущие даты отправки по всем конфигурациям\n"
                    "/help — это сообщение\n\n"
                    "Сам я ничего не покупаю: присылаю ссылку, "
                    "оплату подтверждаешь ты.")


def serve(cfg, tg):
    stop = {"now": False}

    def _stop(signum, _frame):
        log.info("signal %s, shutting down", signum)
        stop["now"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("watching %d SKUs every %ds (city=%s, telegram=%s)",
             len(cfg.parts), cfg.interval, cfg.city or "off",
             "on" if tg.enabled else "off")

    last = ({}, {}, "")
    backoff = 0
    while not stop["now"]:
        try:
            events, current, pickup = check_once(cfg, tg)
            last = (current, pickup,
                    dt.datetime.now().astimezone().isoformat(timespec="seconds"))
            backoff = 0
            if not events:
                earliest = min((p["date"] for p in current.values()
                                if p.get("date")), default=None)
                log.info("ok — %d SKUs, earliest ship date %s",
                         len(current), earliest)
        except AppleUnreachable as e:
            backoff = min(backoff * 2 or cfg.interval, 900)
            log.warning("apple unreachable (%s); backing off %ds", e, backoff)
        except Exception:                              # keep the service alive
            log.exception("unexpected error in check loop")
            backoff = min(backoff * 2 or cfg.interval, 900)

        if tg.enabled:
            try:
                handle_commands(cfg, tg, last)
            except Exception:
                log.exception("command handling failed")

        # Jitter keeps a fixed interval from looking like a metronome to Apple.
        wait = (backoff or cfg.interval) + random.uniform(0, cfg.interval * 0.15)
        deadline = time.monotonic() + wait
        while not stop["now"] and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    log.info("stopped")


# -------------------------------------------------------------------- config

class Config:
    def __init__(self, args):
        self.interval = int(args.interval or os.environ.get("INTERVAL_SECONDS", 60))
        self.city = (args.city if args.city is not None
                     else os.environ.get("CITY", "Dubai"))
        self.state_file = (args.state or os.environ.get("STATE_FILE")
                           or os.path.expanduser("~/.apple-duo-watch/state.json"))
        before = args.alert_before or os.environ.get("ALERT_BEFORE") or ""
        self.threshold = dt.date.fromisoformat(before) if before else None
        only = (args.only or os.environ.get("ONLY_PARTS") or "").strip()
        if only:
            wanted = {p.strip().upper() for p in only.split(",") if p.strip()}
            unknown = wanted - set(ALL_PARTS)
            if unknown:
                raise SystemExit(f"unknown part numbers: {', '.join(sorted(unknown))}")
            self.parts = {k: v for k, v in ALL_PARTS.items() if k in wanted}
        else:
            self.parts = dict(ALL_PARTS)
        if self.interval < 30:
            log.warning("interval %ds is aggressive; Apple may rate-limit this IP",
                        self.interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true",
                    help="run a single check and exit")
    ap.add_argument("--show", action="store_true",
                    help="print the current state of every SKU and exit")
    ap.add_argument("--interval", type=int, default=None)
    ap.add_argument("--city", default=None)
    ap.add_argument("--state", default=None)
    ap.add_argument("--alert-before", default=None, metavar="YYYY-MM-DD")
    ap.add_argument("--only", default=None,
                    help="comma-separated part numbers to watch")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z")

    cfg = Config(args)
    state = load_state(cfg.state_file)
    tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
                  os.environ.get("TELEGRAM_CHAT_ID", "").strip()
                  or state.get("chat_id"))

    if args.show:
        try:
            current = fetch_delivery(cfg.parts, dt.date.today())
        except AppleUnreachable as e:
            print(f"apple unreachable: {e}", file=sys.stderr)
            return 1
        pickup = fetch_pickup(cfg.parts, cfg.city)
        for pn, (name, price) in cfg.parts.items():
            cur = current.get(pn)
            if not cur:
                print(f"{pn:11} {name:18} — no data")
                continue
            pk = "; ".join(pickup.get(pn, [])) or "no pickup"
            print(f"{pn:11} {name:18} {price:>6} AED  {cur['label']:<22}"
                  f" buyable={cur['buyable']!s:<5} [{pk}]")
        return 0

    if args.once:
        try:
            events, _, _ = check_once(cfg, tg)
        except AppleUnreachable as e:
            print(f"apple unreachable: {e}", file=sys.stderr)
            return 1
        if not events:
            log.info("no change")
        return 0

    serve(cfg, tg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
