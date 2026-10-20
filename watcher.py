#!/usr/bin/env python3
"""Watch the Apple UAE store for iPhone Duo availability and earlier ship dates.

Runs as a one-shot check (--once) or as a long-running service that polls on a
fixed interval and reports to Telegram. Standard library only.

Configuration comes from the environment (see .env.example):

  TELEGRAM_BOT_TOKEN   bot token from @BotFather; without it alerts only go to stdout
  TELEGRAM_CHAT_ID     chat to notify; if unset, the first /start sender is adopted
  TELEGRAM_PROXY       http(s) proxy for Telegram only, e.g. http://host:3128;
                       needed on networks where api.telegram.org is blocked
  NTFY_URL             ntfy topic URL to also post alerts to, e.g.
                       https://ntfy.sh/some-private-topic
  NTFY_TOKEN           bearer token, if the ntfy server requires one
  INTERVAL_SECONDS     seconds between checks (default 60)
  CITY                 city for in-store pickup lookups (default Dubai, "" to skip)
  ALERT_BEFORE         YYYY-MM-DD; always alert while any SKU ships on or before it
  STATE_FILE           where to keep the last seen state
  ONLY_PARTS           comma-separated part numbers to narrow the watch
  PICKUP_EVERY         check in-store pickup every Nth cycle instead of all of
                       them; defaults to roughly once a minute, which keeps a
                       short INTERVAL_SECONDS from doubling the request rate
  BLIND_AFTER_SECONDS  warn that the watcher went blind after this many seconds
                       of unbroken failure (default 600); one alert per
                       incident, plus one when it clears
  HEALTHCHECK_URL      outside watchdog pinged after every successful cycle,
                       so that something notices if this process dies or the
                       machine goes off, which no in-process alarm can
  HEALTHCHECK_EVERY_SECONDS
                       at most one healthcheck ping per this many seconds
                       (default 60), so a 10s interval does not hammer it
  HISTORY_FILE         where to keep the findings log that /alerts reads;
                       defaults to the state file's name plus "-history"
  TELEGRAM_ADMINS      comma-separated ids and/or @handles allowed to command
                       the bot besides TELEGRAM_CHAT_ID; handles are pinned to
                       an id the first time that person writes
  ONLY_KINDS           comma-separated finding kinds to announce here (EARLIER,
                       BUYABLE, TARGET, PICKUP, CHANGED); the rest are read and
                       dropped. Only the Mac fallback sets it, for pickup alone
"""
import argparse
import collections
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

# Our urgency words onto ntfy's 1..5 scale. Telegram has only loud or silent.
NTFY_PRIORITY = {"urgent": "5", "low": "2"}

# Telegram rejects anything longer, so /log and /alerts clip to fit.
TELEGRAM_LIMIT = 4096
# Findings kept in the state file, and log lines kept in memory. Both are
# ring buffers: the file must not grow without bound and neither must the
# resident size of a process meant to run for weeks.
HISTORY_LIMIT = 200
LOG_RING = 300

log = logging.getLogger("duo")


class AppleUnreachable(RuntimeError):
    """Apple did not answer, or answered with something we cannot parse."""


# ---------------------------------------------------------------- helpers

def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(text):
    """A stamp written by now_iso(), or None. Always timezone-aware."""
    try:
        when = dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return when.astimezone() if when.tzinfo is None else when


def ago(iso, now=None):
    """'2026-10-17T21:03:11+04:00' -> '3 мин назад'."""
    when = parse_iso(iso)
    if when is None:
        return "никогда"
    secs = max(0, int(((now or dt.datetime.now().astimezone())
                       - when).total_seconds()))
    if secs < 90:
        return f"{secs} с назад"
    if secs < 90 * 60:
        return f"{secs // 60} мин назад"
    if secs < 48 * 3600:
        return f"{secs // 3600} ч назад"
    return f"{secs // 86400} дн назад"


def esc(text):
    """Telegram's HTML mode rejects stray angle brackets, and the error strings
    we quote back are full of them ("<urlopen error [Errno -3] ...>")."""
    return (str(text).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


class RingLog(logging.Handler):
    """Keep the last few hundred log lines in memory, for /log.

    A container cannot read its own docker log, and the alternative — mounting
    the host log or shelling out to the daemon — would hand the watcher far
    more than it needs just to quote itself. A deque costs a few kilobytes.
    """

    def __init__(self, capacity=LOG_RING):
        super().__init__()
        self.lines = collections.deque(maxlen=capacity)

    def emit(self, record):
        try:
            self.lines.append((record.levelno, self.format(record)))
        except Exception:                       # logging must never raise
            self.handleError(record)

    def tail(self, count, min_level=0):
        picked = [text for level, text in self.lines if level >= min_level]
        return picked[-count:]


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
    """Stores where a SKU can be picked up today. Never fatal: pickup is a bonus.

    Returns {} for "asked, nothing available" and None for "could not ask".
    The two must not be confused: an empty answer is news, a failed request is
    not, and recording a failure as "pickup gone" makes the next successful
    lookup report the very same pickup all over again.
    """
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
        return None

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


def save_json(path, data):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def save_state(path, state):
    save_json(path, state)


def load_history(path):
    """The findings log. Its own file, see append_history."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def append_history(path, entries):
    """Add findings to the history, keeping only the last HISTORY_LIMIT.

    Kept apart from the state file on purpose: that one is rewritten every
    cycle, and carrying a few hundred records through every write would
    multiply this service's disk traffic — on the deployed NanoPi, SD-card
    wear — for data that changes a handful of times a day.
    """
    history = (load_history(path) + entries)[-HISTORY_LIMIT:]
    save_json(path, history)
    return history


# ----------------------------------------------------------------- telegram

class Telegram:
    """Minimal Bot API client: push alerts out, answer a few commands."""

    def __init__(self, token, chat_id=None, proxy=None):
        self.token = token
        self.chat_id = chat_id
        self.offset = None
        self.proxy = proxy
        self._fail_streak = 0
        # The watchdog reads these: getUpdates runs every cycle, so the last
        # call's outcome is a live check of the proxy and the tunnel.
        self.last_ok = None
        self.last_error = None
        # Everyone a finding goes to: the owner and the admins. Refreshed
        # from the state file, so admitting someone takes effect at once.
        self.recipients = []
        # A proxy here must not touch the Apple requests, so Telegram gets its
        # own opener instead of the process-wide default.
        handlers = [urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy} if proxy else {})]
        self.opener = urllib.request.build_opener(*handlers)

    @property
    def enabled(self):
        return bool(self.token)

    # Telegram sends chat ids as numbers, the environment hands them over as
    # text, and the state file keeps whichever was stored last. Normalising on
    # every assignment is the whole fix for a comparison that silently dropped
    # every message the owner sent: 1423567839 != "1423567839".
    @property
    def chat_id(self):
        return self._chat_id

    @chat_id.setter
    def chat_id(self, value):
        text = "" if value is None else str(value).strip()
        self._chat_id = text or None

    def _call(self, method, **params):
        if not self.token:
            return None
        url = TELEGRAM_API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}).encode()
        try:
            req = urllib.request.Request(url, data=data)
            with self.opener.open(req, timeout=60) as r:
                out = json.loads(r.read().decode("utf-8"))
            self._fail_streak = 0
            self.last_ok = now_iso()
            self.last_error = None
            return out
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError) as e:
            # On a network that blocks api.telegram.org every call fails, so
            # the first failure and then every 30th are logged, not all of them.
            self._fail_streak += 1
            self.last_error = str(e)
            if self._fail_streak == 1 or self._fail_streak % 30 == 0:
                log.warning("telegram %s failed (%d in a row): %s",
                            method, self._fail_streak, e)
            return None

    @property
    def fail_streak(self):
        return self._fail_streak

    def send(self, html, text=None, markup=None, to=None, priority=None):
        """Send to `to`, or to every recipient when it is not given.

        A reply names its chat and goes only there; a finding names none and
        reaches the owner and every admin. True if at least one of them took
        it, which is what decides whether a finding counts as delivered: one
        admin having blocked the bot must not make the rest lose the news.
        """
        if to is not None:
            targets = [to]
        else:
            targets = self.recipients or ([self.chat_id] if self.chat_id else [])
        if not targets:
            log.warning("no TELEGRAM_CHAT_ID yet; send /start to the bot")
            return False
        delivered = 0
        for target in targets:
            res = self._call("sendMessage", chat_id=target, text=html,
                             parse_mode="HTML",
                             disable_web_page_preview="true",
                             disable_notification=(
                                 "true" if priority == "low" else None),
                             reply_markup=json.dumps(markup) if markup else None)
            if res and res.get("ok"):
                delivered += 1
            else:
                # Blocked the bot, deleted the chat, whatever: say which one,
                # and carry on to the others.
                log.warning("telegram: chat %s did not take the message",
                            target)
        return delivered > 0

    def set_commands(self, commands):
        """Fill the bot's menu in Telegram, so the commands are discoverable."""
        res = self._call("setMyCommands", commands=json.dumps(
            [{"command": c, "description": d} for c, d in commands]))
        return bool(res and res.get("ok"))

    def poll_commands(self, timeout=0):
        """Return [(chat_id, username, text)]. Long-polls when timeout>0."""
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
            # The handle is how a person is let in before the bot has ever
            # seen them; the id is what access is pinned to afterwards.
            who = (msg.get("from") or {}).get("username")
            if chat is not None and text:
                out.append((str(chat), who, text))
        return out


class Ntfy:
    """Post alerts to an ntfy topic. Plain HTTP, no account, no SDK."""

    def __init__(self, url, token=None):
        self.url = (url or "").strip().rstrip("/")
        self.token = (token or "").strip()

    @property
    def enabled(self):
        return bool(self.url)

    def send(self, html, text=None, markup=None, to=None, priority=None):
        if not self.url:
            return False
        body = (text if text is not None else html).encode("utf-8")
        # HTTP header values are latin-1, so the title stays ASCII; the
        # body carries the UTF-8 text.
        headers = {"Title": "iPhone Duo - Apple UAE",
                   "Content-Type": "text/plain; charset=utf-8"}
        # The same link the Telegram button carries, as the notification's tap
        # target. ntfy has no inline keyboard, but it does have this.
        first_url = next((b["url"] for row in (markup or {}).get(
            "inline_keyboard", []) for b in row if b.get("url")), None)
        if first_url:
            headers["Click"] = first_url
        # 5 is ntfy's max: on Android a long vibration burst and a pop-over.
        # The phone can also be told to let this level through Do Not Disturb.
        level = NTFY_PRIORITY.get(priority)
        if level:
            headers["Priority"] = level
            if priority == "urgent":
                headers["Tags"] = "rotating_light"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            req = urllib.request.Request(self.url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return 200 <= r.status < 300
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError) as e:
            log.warning("ntfy post failed: %s", e)
            return False


class Healthcheck:
    """Ping an outside watchdog after every successful cycle.

    Everything else here runs inside this process, so none of it can speak up
    when the process itself is gone, the container is down or the NanoPi is
    off. A dead man's switch outside the house can: it alerts when the pings
    stop. Posted to directly, never through TELEGRAM_PROXY, because the
    network that blocks Telegram reaches ntfy and the healthcheck services.
    """

    def __init__(self, url, every=60):
        self.url = (url or "").strip()
        self.every = max(0, int(every))
        self.last_sent = None      # monotonic, for the rate limit
        self.last_ok = None        # wall clock, for /health
        self.fail_streak = 0

    @property
    def enabled(self):
        return bool(self.url)

    @property
    def host(self):
        """Only the host: a ping URL carries a UUID or an ntfy topic name."""
        return urllib.parse.urlsplit(self.url).netloc or "?"

    def ping(self, note=""):
        """One ping, at most one per `every` seconds. True if it landed."""
        if not self.url:
            return False
        now = time.monotonic()
        if self.last_sent is not None and now - self.last_sent < self.every:
            return False
        self.last_sent = now
        headers = {"Content-Type": "text/plain; charset=utf-8",
                   "Title": "apple-duo-watch heartbeat"}
        try:
            req = urllib.request.Request(
                self.url, data=(note or "ok").encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                ok = 200 <= r.status < 300
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                TimeoutError) as e:
            self.fail_streak += 1
            # Same throttle as Telegram: a healthcheck host that has gone away
            # would otherwise fill the log with one line per cycle.
            if self.fail_streak == 1 or self.fail_streak % 30 == 0:
                log.warning("healthcheck ping failed (%d in a row): %s",
                            self.fail_streak, e)
            return False
        if ok:
            self.fail_streak = 0
            self.last_ok = now_iso()
        return ok


def announce(notifiers, html, text, markup=None, priority=None):
    """Fan an alert out to every configured channel. Returns how many took it."""
    sent = 0
    for n in notifiers:
        if n.enabled and n.send(html, text, markup, priority=priority):
            sent += 1
    return sent


# ----------------------------------------------------------------- watchdog

# "Nothing changed" and "nothing is seen any more" look identical from the
# outside: both are silence. Everything below is about telling them apart.

HEALTH_SUBJECTS = {
    "apple": ("магазин Apple", "🙈", "👀"),
    "telegram": ("Telegram", "🙉", "📨"),
}


def render_health_alert(kind, subject, entry, blind_for, html=True):
    name, blind_icon, back_icon = HEALTH_SUBJECTS.get(
        subject, (subject, "🚨", "✅"))
    if kind == "back":
        since = entry.get("failing_since")
        return (f"{back_icon} Снова вижу {name}."
                + (f"\nНе отвечал с {since}." if since else ""))
    error = entry.get("last_error") or "без описания"
    head = f"<b>Не вижу {name}</b>" if html else f"Не вижу {name}"
    hint = ("Пока это так, об изменениях в магазине я не узнаю: проверь "
            "контейнер и не забанил ли Apple исходящий IP."
            if subject == "apple" else
            "Находки пока доходят только во второй канал: проверь "
            "telegram-tunnel.service и прокси.")
    return (f"{blind_icon} {head}\n"
            f"Неудач подряд: {entry.get('fail_streak', 0)}, "
            f"с {entry.get('failing_since')} (≈{int(blind_for // 60)} мин).\n"
            f"Ошибка: {esc(error) if html else error}\n{hint}")


class Watchdog:
    """Say it once when something stays broken, and once when it recovers.

    The trigger is `after` seconds of unbroken failure rather than a number of
    cycles, so the backoff cannot stretch it. The streak lives in the state
    file, so a restart neither forgets an open incident nor opens a second one.
    An alert no channel accepted is not marked as sent, the same way a finding
    nobody took is not marked as seen.
    """

    # A healthy cycle changes nothing but last_ok, and at a 10s interval
    # saving that every time would triple the writes this service makes to an
    # SD card for no gain. Quiet cycles are written at most this often.
    QUIET_WRITE_EVERY = 60

    def __init__(self, state_file, after):
        self.state_file = state_file
        self.after = max(1, int(after))
        self.written = {}

    def record(self, subject, ok, reason=None, notifiers=()):
        state = load_state(self.state_file)
        entry = state.setdefault("health", {}).setdefault(subject, {})
        was_failing = bool(entry.get("fail_streak") or entry.get("failing_since"))
        now = dt.datetime.now().astimezone()
        alert = None

        if ok:
            entry["last_ok"] = now.isoformat(timespec="seconds")
            entry["fail_streak"] = 0
            if entry.get("alerted"):
                # Keep failing_since and last_error until the "ok again"
                # message is actually delivered: it quotes them.
                alert = ("back", 0.0)
            else:
                entry.pop("failing_since", None)
                entry.pop("last_error", None)
        else:
            entry["fail_streak"] = entry.get("fail_streak", 0) + 1
            if reason is not None:
                entry["last_error"] = str(reason)[:300]
            since = entry.get("failing_since")
            if not since:
                entry["failing_since"] = since = now.isoformat(timespec="seconds")
            blind_for = max(0.0, (now - (parse_iso(since) or now)).total_seconds())
            if blind_for >= self.after and not entry.get("alerted"):
                alert = ("blind", blind_for)

        if alert:
            kind, blind_for = alert
            delivered = announce(
                notifiers,
                render_health_alert(kind, subject, entry, blind_for),
                render_health_alert(kind, subject, entry, blind_for, html=False),
                priority="urgent" if kind == "blind" else None)
            if delivered:
                log.warning("watchdog: %s is %s, told %d channel(s)",
                            subject, kind, delivered)
                if kind == "blind":
                    entry["alerted"] = True
                else:
                    entry.pop("alerted", None)
                    entry.pop("failing_since", None)
                    entry.pop("last_error", None)
            else:
                log.warning("watchdog: %s is %s but no channel took it; "
                            "retrying next cycle", subject, kind)

        quiet = ok and alert is None and not was_failing
        age = time.monotonic() - self.written.get(subject, float("-inf"))
        if not quiet or age >= self.QUIET_WRITE_EVERY:
            save_state(self.state_file, state)
            self.written[subject] = time.monotonic()
        return alert


def status_footer(rt, state, html=True):
    """The two lines of health that belong next to the prices, and no more."""
    earliest = min((p["date"] for p in rt.current.values() if p.get("date")),
                   default=None)
    lines = []
    if earliest:
        lines.append(f"Самый ранний срок: {earliest}")
    lines.append(f"Последний удачный опрос: {ago(state.get('checked_at'))}")
    if state.get("paused"):
        queued = len(state.get("paused_queue") or [])
        lines.append(f"⏸ Уведомления на паузе, накопилось {queued}. "
                     f"/resume — включить")
    return "\n".join(lines)


def render_health(cfg, tg, hc, rt=None, html=True):
    """Answer for /health: when the store was last seen, and by which path."""
    state = load_state(cfg.state_file)
    health = state.get("health") or {}
    apple = health.get("apple") or {}
    checked_at = state.get("checked_at")
    lines = ["<b>Самочувствие наблюдателя</b>" if html
             else "Самочувствие наблюдателя"]

    lines.append(f"🛒 Магазин Apple: удачная проверка {ago(checked_at)}"
                 + (f" ({checked_at})" if checked_at else ""))
    streak = apple.get("fail_streak") or 0
    if streak:
        err = apple.get("last_error") or "без описания"
        lines.append(f"⚠️ Неудач подряд: {streak}, с "
                     f"{apple.get('failing_since')} — "
                     f"{esc(err) if html else err}")
    else:
        lines.append("✅ Неудач подряд: 0")
    lines.append(f"⏱ Порог тревоги: {cfg.blind_after} с непрерывных неудач")

    if tg and tg.enabled:
        tgh = health.get("telegram") or {}
        route = f"через прокси {tg.proxy}" if tg.proxy else "напрямую"
        tstreak = tgh.get("fail_streak") or 0
        lines.append(f"{'✅' if not tstreak else '⚠️'} Telegram {route}: "
                     f"ответ {ago(tgh.get('last_ok'))}, неудач подряд "
                     f"{tstreak}")
        # This very message came through the tunnel, so it is up right now.
        lines.append("    (раз это сообщение дошло — туннель жив)")

    if hc and hc.enabled:
        lines.append(f"{'✅' if not hc.fail_streak else '⚠️'} Внешний "
                     f"healthcheck {hc.host}: пинг {ago(hc.last_ok)}, "
                     f"неудач подряд {hc.fail_streak}")
    else:
        lines.append("➖ Внешнего healthcheck нет (HEALTHCHECK_URL пуст): "
                     "если процесс умрёт целиком, сказать об этом будет некому")

    pickup_note = ("" if cfg.pickup_every == 1
                   else f", самовывоз раз в {cfg.pickup_every} циклов")
    lines.append(f"🔁 Интервал: {cfg.interval} с{pickup_note}")
    if rt is not None:
        lines.append(f"♻️ Циклов с запуска: {rt.cycles} "
                     f"(запущен {ago(rt.started)})")
    history = load_history(cfg.history_file)
    lines.append(f"🗂 Находок в истории: {len(history)}"
                 + (f", последняя {ago(history[-1].get('at'))}"
                    if history else ""))
    if state.get("paused"):
        queued = len(state.get("paused_queue") or [])
        lines.append(f"⏸ Уведомления на паузе, накопилось {queued}")
    return "\n".join(lines)


# ------------------------------------------------------------------ service

def render_status(current, parts, pickup, checked_at, html=True, footer=None):
    if html:
        lines = [f"<b>iPhone Duo — Apple UAE</b>  <i>{checked_at}</i>"]
    else:
        lines = [f"iPhone Duo — Apple UAE  ({checked_at})"]
    for pn, (name, price) in parts.items():
        cur = current.get(pn)
        if not cur:
            lines.append(f"• {name} — нет данных")
            continue
        mark = "✅" if cur["buyable"] else "⛔️"
        pk = pickup.get(pn)
        tail = f"  📦 {'; '.join(pk)}" if pk else ""
        if html:
            lines.append(f'{mark} <a href="{link_for(pn)}">{name}</a> — '
                         f'{cur["label"]}, {price} AED{tail}')
        else:
            lines.append(f'{mark} {name} — {cur["label"]}, {price} AED{tail}'
                         f'\n   {link_for(pn)}')
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


ALERT_ICONS = {"EARLIER": "⏩", "BUYABLE": "🟢", "TARGET": "🎯",
               "PICKUP": "🏬", "CHANGED": "ℹ️"}

# A freed slot lives minutes. A notification read twenty minutes later is
# worth nothing, so these wake the phone up; a plain label change does not.
URGENT_KINDS = {"EARLIER", "BUYABLE", "TARGET", "PICKUP"}


def urgency(events):
    """'urgent' if any finding is worth waking up for, else 'low'."""
    return "urgent" if any(e[0] in URGENT_KINDS for e in events) else "low"


def render_alert(events, html=True):
    head = ("🔔 <b>iPhone Duo — изменение в Apple UAE</b>" if html
            else "🔔 iPhone Duo — изменение в Apple UAE")
    body = []
    for kind, pn, name, price, detail, link in events:
        icon = ALERT_ICONS.get(kind, "•")
        if html:
            body.append(f'{icon} <a href="{link}">{name}</a> — {detail}'
                        f'\n    {price} AED')
        else:
            body.append(f'{icon} {name} — {detail}\n    {price} AED\n    {link}')
    return (head + "\n" + "\n".join(body)
            + "\n\nОформление и оплату подтверждаешь сам.")


def alert_markup(events):
    """One link button per finding — plain Bot API, nothing to install."""
    rows, seen = [], set()
    for _kind, pn, name, _price, _detail, link in events:
        if pn in seen:
            continue
        seen.add(pn)
        rows.append([{"text": f"Открыть {name}", "url": link}])
    return {"inline_keyboard": rows[:8]} if rows else None


def history_entries(events, previous, current, at):
    """Findings as records for the state file, so /alerts can look back."""
    out = []
    for kind, pn, name, price, detail, _link in events:
        old = previous.get(pn) or {}
        cur = current.get(pn) or {}
        out.append({"at": at, "kind": kind, "part": pn, "name": name,
                    "price": price, "detail": detail,
                    "old_date": old.get("date"), "new_date": cur.get("date"),
                    "old_label": old.get("label"), "new_label": cur.get("label")})
    return out


def clip_tail(text, budget):
    """Keep the end: in a log and in a history, the tail is the news."""
    if len(text) <= budget:
        return text, False
    return text[-budget:], True


def render_alerts(history, count, html=True, title=None):
    """Answer for /alerts: the findings that were made, newest first."""
    if not history:
        return ("Находок ещё не было ни одной. Сервис сообщает только об "
                "изменениях, так что само по себе это не поломка: значит, с "
                "запуска ничего не менялось.")
    shown = history[-count:]
    head = title or f"Последние находки — {len(shown)} из {len(history)}"
    lines = [f"<b>{head}</b>" if html else head]
    for e in reversed(shown):
        icon = ALERT_ICONS.get(e.get("kind"), "•")
        when = (e.get("at") or "")[:16].replace("T", " ")
        name = e.get("name") or e.get("part") or "?"
        move = ""
        if e.get("old_date") or e.get("new_date"):
            move = f"  {e.get('old_date') or '—'} → {e.get('new_date') or '—'}"
        detail = e.get("detail") or ""
        price = f", {e['price']} AED" if e.get("price") else ""
        if html:
            link = link_for(e["part"]) if e.get("part") else None
            shown_name = (f'<a href="{link}">{esc(name)}</a>' if link
                          else esc(name))
            lines.append(f"{icon} <i>{when}</i> {shown_name}{esc(price)}"
                         f"{esc(move)}\n    {esc(detail)}")
        else:
            lines.append(f"{icon} {when} {name}{price}{move}\n    {detail}")
    body = "\n".join(lines)
    body, cut = clip_tail(body, TELEGRAM_LIMIT - 80)
    return ("…\n" + body) if cut else body


def render_log(logs, count, errors_only, html=True):
    """Answer for /log: the tail of this process's own log."""
    lines = logs.tail(count, logging.WARNING if errors_only else 0)
    if not lines:
        return ("Ни одной строки WARNING и выше — пока всё спокойно."
                if errors_only else "Лог пока пуст.")
    head = (f"Лог, последние {len(lines)} стр."
            + (" (WARNING и выше)" if errors_only else ""))
    body = esc("\n".join(lines)) if html else "\n".join(lines)
    # The head, the <pre> wrapper and the "tail only" note all have to fit.
    body, cut = clip_tail(body, TELEGRAM_LIMIT - len(head) - 120)
    note = "\n(показан только хвост — целиком не влезает)" if cut else ""
    if html:
        return f"<b>{head}</b>{note}\n<pre>{body}</pre>"
    return f"{head}{note}\n{body}"


def check_once(cfg, notifiers, notify=True, with_pickup=True):
    """One pass. Returns (events, current, pickup). Raises AppleUnreachable."""
    tg = next((n for n in notifiers if isinstance(n, Telegram)), None)
    today = dt.date.today()
    current = fetch_delivery(cfg.parts, today)

    state = load_state(cfg.state_file)
    previous = state.get("parts", {})
    paused = bool(state.get("paused"))
    if tg:
        tg.recipients = recipient_ids(state, tg.chat_id)

    pickup = fetch_pickup(cfg.parts, cfg.city) if with_pickup else None
    if pickup is None:
        # Either this cycle skips the lookup, or the lookup failed. Carry the
        # last known pickup forward: not having asked cannot be allowed to
        # read as "pickup disappeared".
        pickup = {pn: list(p.get("pickup") or [])
                  for pn, p in previous.items() if p.get("pickup")}
    events = diff(previous, current, pickup, cfg.parts, cfg.threshold)
    if previous and events and not with_pickup and cfg.city:
        # The delivery side just moved, and a restock tends to move pickup in
        # the same wave. This cycle was not due a pickup lookup, but waiting
        # up to a minute for the next one is exactly the delay that costs a
        # slot. One extra request, and only when something actually changed.
        fresh = fetch_pickup(cfg.parts, cfg.city)
        if fresh is not None:
            log.info("delivery moved, so pickup was looked up at once")
            pickup = fresh
            events = diff(previous, current, pickup, cfg.parts, cfg.threshold)

    if cfg.only_kinds:
        # Read everything, announce only what this instance was asked to. The
        # dropped findings are not held back: whoever covers them has them,
        # and holding them here would re-announce them the moment this stops.
        events = [e for e in events if e[0] in cfg.only_kinds]

    checked_at = now_iso()
    state["checked_at"] = checked_at
    if tg and tg.chat_id:
        state["chat_id"] = tg.chat_id

    def persist():
        state["parts"] = current
        save_state(cfg.state_file, state)

    if not previous:
        best = min((p["date"] for p in current.values() if p.get("date")),
                   default=None)
        log.info("baseline recorded for %d SKUs, earliest ship date %s",
                 len(current), best)
        persist()
        if notify and not paused:
            tail = (f"\n\nПроверка каждые {cfg.interval} с. "
                    "Напишу, как только отправка станет раньше.")
            announce(
                notifiers,
                "👀 Слежу за iPhone Duo в Apple UAE.\n"
                + render_status(current, cfg.parts, pickup, checked_at) + tail,
                "👀 Слежу за iPhone Duo в Apple UAE.\n"
                + render_status(current, cfg.parts, pickup, checked_at,
                                html=False) + tail)
        return [], current, pickup

    for ev in events:
        log.info("%s", format_event(ev))

    if events:
        entries = history_entries(events, previous, current, checked_at)
        if notify and paused:
            # Paused means "say nothing", not "notice nothing". The findings
            # are kept twice — in the history and in the queue /resume reports
            # — and the state does move on. Holding it back instead would
            # rediscover the same change every cycle and fill the history with
            # copies of it; nothing is lost, because /resume replays the queue.
            append_history(cfg.history_file, entries)
            state["paused_queue"] = (
                (state.get("paused_queue") or []) + entries)[-HISTORY_LIMIT:]
            log.info("paused: %d finding(s) recorded but not sent", len(events))
            persist()
            return events, current, pickup
        if notify:
            delivered = announce(notifiers, render_alert(events),
                                 render_alert(events, html=False),
                                 alert_markup(events),
                                 priority=urgency(events))
            if not delivered:
                # Persisting now would make this change "already seen" and it
                # would never be reported again, so the old state stays and
                # the next check rediscovers it.
                log.warning("found %d change(s) but no channel accepted them; "
                            "keeping the previous state so the next check "
                            "retries", len(events))
                # The new dates stay unrecorded, but the fact that Apple
                # answered is saved: checked_at is what says we are not blind.
                save_state(cfg.state_file, state)
                return events, current, pickup
        append_history(cfg.history_file, entries)

    persist()
    return events, current, pickup


# -------------------------------------------------------------------- access

def norm_handle(name):
    """'@Example_Handle' -> 'example_handle'. Handles are case-insensitive."""
    return (name or "").strip().lstrip("@").lower()


def seed_admins(cfg, state):
    """Fold TELEGRAM_ADMINS into the state. Idempotent, so a restart is free."""
    admins = state.setdefault("admins", {})
    waiting = state.setdefault("admin_usernames", [])
    changed = False
    known = {rec.get("username") for rec in admins.values() if rec.get("username")}
    for entry in cfg.admin_seed:
        token = entry.lstrip("@")
        if token.isdigit():
            if token not in admins:
                admins[token] = {"username": None, "at": now_iso(),
                                 "added_by": "TELEGRAM_ADMINS"}
                changed = True
        else:
            handle = norm_handle(token)
            if handle and handle not in known and handle not in waiting:
                waiting.append(handle)
                changed = True
    return changed


def authorize(state, owner, chat_id, username):
    """May this chat command the bot? Returns (allowed, state_changed).

    A handle on the waiting list is claimed on first contact and pinned to the
    numeric id, because a Telegram handle can be given up and taken over by
    somebody else, while an id cannot. After that the handle is only a label.
    """
    chat_id = str(chat_id)
    if owner and chat_id == str(owner):
        return True, False
    admins = state.setdefault("admins", {})
    if chat_id in admins:
        return True, False
    handle = norm_handle(username)
    waiting = state.get("admin_usernames") or []
    if handle and handle in [norm_handle(w) for w in waiting]:
        admins[chat_id] = {"username": handle, "at": now_iso(),
                           "added_by": "список ожидания"}
        state["admin_usernames"] = [w for w in waiting
                                    if norm_handle(w) != handle]
        log.info("admin @%s claimed and pinned to chat %s", handle, chat_id)
        return True, True
    return False, False


def recipient_ids(state, owner):
    """Everyone a finding should reach: the owner first, then the admins."""
    out = [str(owner)] if owner else []
    for cid in sorted((state.get("admins") or {}).keys()):
        if cid not in out:
            out.append(cid)
    return out


def render_admins(state, owner, html=True):
    """Answer for /admins: who may command the bot, and who is still awaited."""
    lines = ["<b>Кто управляет ботом</b>" if html else "Кто управляет ботом"]
    lines.append(f"👑 {owner} — владелец, снять нельзя")
    for cid, rec in sorted((state.get("admins") or {}).items()):
        if owner and cid == str(owner):
            continue
        who = f"@{rec['username']}" if rec.get("username") else f"id {cid}"
        lines.append(f"• {who} — id {cid}, добавил {rec.get('added_by') or '?'}"
                     + (f", {rec['at'][:16].replace('T', ' ')}"
                        if rec.get("at") else ""))
    for handle in state.get("admin_usernames") or []:
        lines.append(f"⏳ @{handle} — впущен, но ещё не писал боту; "
                     f"id закрепится за ним при первом сообщении")
    if len(lines) == 2:
        lines.append("Больше никого. /addadmin @имя — впустить.")
    lines.append("")
    lines.append(f"Уведомления о находках приходят всем из этого списка "
                 f"({len(recipient_ids(state, owner))} чат(а)); тем, кто ждёт "
                 f"первого сообщения, — пока нет.")
    lines.append("Добавлять и убирать может только тот, кто уже в этом списке.")
    return "\n".join(lines)


def add_admin(state, target, by):
    """/addadmin. Numbers are pinned at once, handles wait for first contact."""
    token = target.lstrip("@")
    if token.isdigit():
        admins = state.setdefault("admins", {})
        if token in admins:
            return f"id {token} уже в списке."
        admins[token] = {"username": None, "at": now_iso(), "added_by": by}
        return f"✅ id {token} теперь админ."
    handle = norm_handle(token)
    if not handle or not re.fullmatch(r"[a-z0-9_]{4,32}", handle):
        return ("Это не похоже на имя пользователя Telegram. "
                "Напиши /addadmin @имя или /addadmin 123456789.")
    for cid, rec in (state.get("admins") or {}).items():
        if rec.get("username") == handle:
            return f"@{handle} уже админ (id {cid})."
    waiting = state.setdefault("admin_usernames", [])
    if handle in [norm_handle(w) for w in waiting]:
        return f"@{handle} уже в списке ожидания."
    waiting.append(handle)
    return (f"✅ @{handle} впущен. Доступ откроется, когда он сам напишет "
            f"боту: тогда за ним закрепится его id.")


def remove_admin(state, owner, target):
    """/deladmin. The owner cannot be removed — that would lock everyone out."""
    token = target.lstrip("@")
    if owner and token == str(owner):
        return "Владельца снять нельзя."
    admins = state.setdefault("admins", {})
    if token.isdigit() and token in admins:
        del admins[token]
        return f"🚫 id {token} больше не админ."
    handle = norm_handle(token)
    for cid, rec in list(admins.items()):
        if rec.get("username") == handle:
            del admins[cid]
            return f"🚫 @{handle} (id {cid}) больше не админ."
    waiting = state.get("admin_usernames") or []
    if handle in [norm_handle(w) for w in waiting]:
        state["admin_usernames"] = [w for w in waiting
                                    if norm_handle(w) != handle]
        return f"🚫 @{handle} убран из списка ожидания."
    return f"{target} и так не в списке."


class Runtime:
    """What the command handlers need to know about the running service."""

    def __init__(self, cfg, notifiers, hc, logs):
        self.cfg = cfg
        self.notifiers = notifiers
        self.hc = hc
        self.logs = logs
        self.current = {}
        self.pickup = {}
        self.checked_at = ""
        self.cycles = 0
        self.started = now_iso()


COMMANDS = [
    ("status", "даты отправки по всем конфигурациям"),
    ("admins", "кто управляет ботом"),
    ("alerts", "последние находки, например /alerts 25"),
    ("check", "проверить прямо сейчас, не дожидаясь интервала"),
    ("health", "самочувствие наблюдателя"),
    ("log", "хвост лога, например /log 50 или /log errors"),
    ("pause", "замолчать, не прекращая следить"),
    ("resume", "снова присылать уведомления"),
    ("addadmin", "впустить: /addadmin @имя"),
    ("deladmin", "убрать: /deladmin @имя"),
    ("help", "что умеет бот"),
]


def parse_count(text, default, cap):
    """'/alerts 25' -> 25, clamped. Anything else keeps the default."""
    for word in text.split()[1:]:
        if word.isdigit():
            return max(1, min(int(word), cap))
    return default


def render_help(html=True):
    lines = ["<b>Что умеет бот</b>" if html else "Что умеет бот"]
    lines += [f"/{cmd} — {about}" for cmd, about in COMMANDS]
    lines.append("")
    lines.append("Сам я ничего не покупаю: присылаю ссылку на нужную "
                 "конфигурацию, оформление и оплату подтверждаешь ты.")
    return "\n".join(lines)


def run_check_now(rt, tg, to=None):
    """/check: a pass right now, instead of waiting out the interval."""
    try:
        events, current, pickup = check_once(rt.cfg, rt.notifiers)
    except AppleUnreachable as e:
        tg.send(f"Apple не ответил: {esc(e)}", to=to)
        return
    rt.current, rt.pickup, rt.checked_at = current, pickup, now_iso()
    rt.cycles += 1
    state = load_state(rt.cfg.state_file)
    if not events:
        tg.send("Изменений нет.\n" + render_status(
            current, rt.cfg.parts, pickup, rt.checked_at,
            footer=status_footer(rt, state)), to=to)
    elif state.get("paused"):
        tg.send(f"Нашёл изменений: {len(events)}. Уведомления на паузе, "
                f"так что записал в историю — /resume покажет.", to=to)
    else:
        # check_once announced them to the owner's chat, not necessarily here.
        tg.send(f"Нашёл изменений: {len(events)} — уведомление ушло в чат "
                f"владельца.", to=to)


def handle_commands(rt, tg):
    """Answer the admins' commands; everyone else gets silence."""
    cfg = rt.cfg
    for chat_id, username, text in tg.poll_commands():
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        if tg.chat_id is None:
            tg.chat_id = chat_id
            state = load_state(cfg.state_file)
            state["chat_id"] = chat_id
            save_state(cfg.state_file, state)
            log.info("adopted chat_id %s", chat_id)

        state = load_state(cfg.state_file)
        allowed, claimed = authorize(state, tg.chat_id, chat_id, username)
        if claimed:
            save_state(cfg.state_file, state)
        if not allowed:
            # A stranger is told nothing at all — not even that the command
            # exists. The handle is logged because that is what you need to
            # decide whether it was someone you meant to let in.
            log.warning("ignoring /%s from chat %s (@%s)", cmd, chat_id,
                        username or "?")
            continue
        # Logged so that /log alone shows whether a command ever arrived and
        # was accepted — the symptom of the chat_id mismatch was silence, and
        # silence is the one thing a log should never leave ambiguous.
        log.info("command /%s from %s (@%s)", cmd, chat_id, username or "?")

        dispatch(rt, tg, state, cmd, text, chat_id, username)


def dispatch(rt, tg, state, cmd, text, chat_id, username):
    """Run one command. `chat_id` is whoever asked; tg.chat_id stays the owner."""
    cfg = rt.cfg
    owner = tg.chat_id

    def reply(body, markup=None):
        return tg.send(body, None, markup, to=chat_id)

    if cmd in ("start", "status"):
        if rt.current:
            reply(render_status(rt.current, cfg.parts, rt.pickup, rt.checked_at,
                                footer=status_footer(rt, state)))
        else:
            reply("Ещё не делал первую проверку, подожди минуту.")
    elif cmd == "alerts":
        reply(render_alerts(load_history(cfg.history_file),
                            parse_count(text, 10, 50)))
    elif cmd == "health":
        reply(render_health(cfg, tg, rt.hc, rt))
    elif cmd == "log":
        reply(render_log(rt.logs, parse_count(text, 20, 100),
                         "errors" in text.lower()))
    elif cmd == "check":
        run_check_now(rt, tg, chat_id)
    elif cmd == "pause":
        state["paused"] = True
        save_state(cfg.state_file, state)
        reply("⏸ Уведомления выключены. Следить продолжаю: находки пишу в "
              "историю и в лог, /resume вернёт их и снова включит сообщения.")
    elif cmd == "resume":
        queued = state.get("paused_queue") or []
        state["paused"] = False
        state["paused_queue"] = []
        save_state(cfg.state_file, state)
        if queued:
            reply(render_alerts(queued, len(queued),
                                title=f"▶️ Снова на связи. За паузу "
                                      f"накопилось находок: {len(queued)}"))
        else:
            reply("▶️ Снова на связи. За паузу ничего не нашлось.")
    elif cmd == "admins":
        reply(render_admins(state, owner))
    elif cmd in ("addadmin", "deladmin"):
        parts = text.split()
        if len(parts) < 2:
            reply(f"Кого? Напиши /{cmd} @имя или /{cmd} 123456789.\n\n"
                  + render_admins(state, owner))
        else:
            by = f"@{username}" if username else str(chat_id)
            if cmd == "addadmin":
                answer = add_admin(state, parts[1], by)
            else:
                answer = remove_admin(state, owner, parts[1])
            save_state(cfg.state_file, state)
            tg.recipients = recipient_ids(state, owner)
            log.info("/%s %s by %s -> %s (now %d recipient(s))", cmd,
                     parts[1], by, answer, len(tg.recipients))
            reply(answer)
            # The owner is told whenever the guest list changes, even when
            # somebody else changed it: this is the one thing that decides
            # who can silence their alerts.
            if str(chat_id) != str(owner):
                tg.send(f"ℹ️ {by} изменил список доступа: {answer}", to=owner)
    elif cmd == "help":
        reply(render_help())


def serve(cfg, notifiers, wd, hc, logs):
    tg = next((n for n in notifiers if isinstance(n, Telegram)), None)
    rt = Runtime(cfg, notifiers, hc, logs)
    stop = {"now": False}

    def _stop(signum, _frame):
        log.info("signal %s, shutting down", signum)
        stop["now"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    channels = ", ".join(
        f"{type(n).__name__.lower()}{'+proxy' if getattr(n, 'proxy', None) else ''}"
        for n in notifiers if n.enabled) or "none (stdout only)"
    pickup_note = (cfg.city if not cfg.city or cfg.pickup_every == 1
                   else f"{cfg.city} every {cfg.pickup_every} cycles")
    log.info("watching %d SKUs every %ds (pickup=%s, channels=%s)",
             len(cfg.parts), cfg.interval, pickup_note or "off", channels)
    log.info("watchdog: alerting after %ds of unbroken failure; heartbeat=%s",
             cfg.blind_after,
             f"{hc.host} every {hc.every}s" if hc.enabled else "off")
    if cfg.only_kinds:
        log.warning("announcing only %s here; other findings are read and "
                    "dropped, because another watcher covers them",
                    ", ".join(sorted(cfg.only_kinds)))
    seed_state = load_state(cfg.state_file)
    if seed_admins(cfg, seed_state):
        save_state(cfg.state_file, seed_state)
    if tg:
        tg.recipients = recipient_ids(seed_state, tg.chat_id)
    extra = len(seed_state.get("admins") or {}) + len(
        seed_state.get("admin_usernames") or [])
    if extra:
        log.info("%d admin(s) besides the owner may command the bot; "
                 "findings go to %d chat(s)", extra,
                 len(tg.recipients) if tg else 0)
    if load_state(cfg.state_file).get("paused"):
        log.warning("notifications are paused (/resume to turn them back on); "
                    "findings are still recorded")
    if tg and tg.enabled:
        # Puts the commands in Telegram's own menu, so they are discoverable
        # without reading the README.
        tg.set_commands(COMMANDS)

    backoff = 0
    cycle = 0
    while not stop["now"]:
        started = time.monotonic()
        try:
            events, current, pickup = check_once(
                cfg, notifiers, with_pickup=(cycle % cfg.pickup_every == 0))
            cycle += 1
            rt.current, rt.pickup, rt.checked_at = current, pickup, now_iso()
            rt.cycles = cycle
            backoff = 0
            earliest = min((p["date"] for p in current.values()
                            if p.get("date")), default=None)
            if not events:
                log.info("ok — %d SKUs, earliest ship date %s",
                         len(current), earliest)
            wd.record("apple", True, notifiers=notifiers)
            # Proof of life for something outside this process, which is the
            # only thing that can speak up once this process is gone.
            hc.ping(f"{len(current)} SKUs, earliest {earliest}")
        except AppleUnreachable as e:
            backoff = min(backoff * 2 or cfg.interval, 900)
            log.warning("apple unreachable (%s); backing off %ds", e, backoff)
            wd.record("apple", False, e, notifiers=notifiers)
        except Exception as e:                         # keep the service alive
            log.exception("unexpected error in check loop")
            backoff = min(backoff * 2 or cfg.interval, 900)
            wd.record("apple", False, e, notifiers=notifiers)

        if tg and tg.enabled:
            try:
                handle_commands(rt, tg)
            except Exception:
                log.exception("command handling failed")
            # getUpdates ran just now, so its outcome is a live check of the
            # proxy and the tunnel. Nothing else here would notice them going
            # away: without a finding to deliver, Telegram is never called.
            wd.record("telegram", tg.fail_streak == 0, tg.last_error,
                      notifiers=notifiers)

        # Subtract the work already done, so the cycle period is the interval
        # rather than interval plus however long Apple took to answer.
        elapsed = time.monotonic() - started
        base = backoff or max(0.0, cfg.interval - elapsed)
        # Jitter keeps a fixed interval from looking like a metronome to Apple.
        wait = base + random.uniform(0, cfg.interval * 0.15)
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
        self.history_file = (os.environ.get("HISTORY_FILE")
                             or f"{os.path.splitext(self.state_file)[0]}-history.json")
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
        # One delivery request per cycle is unavoidable; the pickup lookup is a
        # second one, so at short intervals it runs about once a minute instead.
        every = args.pickup_every or os.environ.get("PICKUP_EVERY")
        self.pickup_every = max(1, int(every)) if every else max(
            1, round(60 / self.interval))
        self.admin_seed = [a.strip() for a in
                           (os.environ.get("TELEGRAM_ADMINS") or "").split(",")
                           if a.strip()]
        kinds = (os.environ.get("ONLY_KINDS") or "").strip()
        self.only_kinds = {k.strip().upper() for k in kinds.split(",") if k.strip()}
        unknown = self.only_kinds - set(ALERT_ICONS)
        if unknown:
            raise SystemExit("unknown finding kinds in ONLY_KINDS: "
                             + ", ".join(sorted(unknown)))
        self.blind_after = int(args.blind_after
                               or os.environ.get("BLIND_AFTER_SECONDS") or 600)
        self.healthcheck_url = (os.environ.get("HEALTHCHECK_URL") or "").strip()
        self.healthcheck_every = int(
            os.environ.get("HEALTHCHECK_EVERY_SECONDS") or 60)
        if self.interval < 30:
            log.warning("interval %ds is aggressive; Apple may rate-limit this "
                        "IP, which would make the watcher blind", self.interval)
        if not self.healthcheck_url:
            log.warning("HEALTHCHECK_URL is unset: an in-process watchdog "
                        "cannot report that the process itself died")


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
    ap.add_argument("--pickup-every", type=int, default=None, metavar="N",
                    help="look up in-store pickup every Nth cycle")
    ap.add_argument("--blind-after", type=int, default=None, metavar="SECONDS",
                    help="warn after this long without a successful check")
    args = ap.parse_args(argv)

    fmt = "%(asctime)s %(levelname)s %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S%z"
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format=fmt, datefmt=datefmt)
    # Attached before anything else runs, so the startup warnings are in /log.
    logs = RingLog()
    logs.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
    logging.getLogger().addHandler(logs)

    cfg = Config(args)
    state = load_state(cfg.state_file)
    tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
                  os.environ.get("TELEGRAM_CHAT_ID", "").strip()
                  or state.get("chat_id"),
                  os.environ.get("TELEGRAM_PROXY", "").strip() or None)
    ntfy = Ntfy(os.environ.get("NTFY_URL", ""),
                os.environ.get("NTFY_TOKEN", ""))
    notifiers = [tg, ntfy]
    wd = Watchdog(cfg.state_file, cfg.blind_after)
    hc = Healthcheck(cfg.healthcheck_url, cfg.healthcheck_every)

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
            events, current, _ = check_once(cfg, notifiers)
        except AppleUnreachable as e:
            wd.record("apple", False, e, notifiers=notifiers)
            print(f"apple unreachable: {e}", file=sys.stderr)
            return 1
        wd.record("apple", True, notifiers=notifiers)
        hc.ping(f"{len(current)} SKUs")
        if not events:
            log.info("no change")
        return 0

    serve(cfg, notifiers, wd, hc, logs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
