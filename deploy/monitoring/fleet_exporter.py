#!/usr/bin/env python3
"""Fleet exporter for the apple-duo-watch deployment, in Prometheus text format.

Standard library only. It reads what the watchers already leave on this host
and changes nothing: the heartbeat file, the deadman flag, the standby
container's metadata, its json log and its state file, all mounted read-only
(see compose.yml). It also probes a few ports from outside.

Only numbers leave this process. No chat ids, handles, tokens or message
texts are exported; the log is counted, never published.
"""
import datetime as dt
import http.server
import json
import os
import pathlib
import re
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

LISTEN = os.environ.get("LISTEN", "127.0.0.1:9877")
HEARTBEAT = pathlib.Path(os.environ.get("HEARTBEAT", "/host/duo-watch/heartbeat"))
DEADMAN_FLAG = pathlib.Path(os.environ.get("DEADMAN_FLAG", "/host/deadman/alerted"))
CONTAINERS = pathlib.Path(os.environ.get("CONTAINERS", "/host/containers"))
VOLUMES = pathlib.Path(os.environ.get("VOLUMES", "/host/volumes"))
STANDBY_NAME = "/apple-duo-watch-standby"
AMNEZIA_NAME = "/amnezia-wg-easy"

# The NanoPi and the home PC share one public address and differ by port.
TCP_PROBES = [("nanopi_ssh", "37.204.22.209", 2222),
              ("pc_ssh", "37.204.22.209", 2202)]
SERVICE_PROBES = [("sink", "127.0.0.1", 8899),
                  ("tinyproxy", "127.0.0.1", 8888),
                  ("sshd", "127.0.0.1", 22)]
TELEGRAM_URL = "https://api.telegram.org/"

# The same catalogue as watcher.py. Keep the two in step by hand.
SKU = {
    "MK244AH/A": ("256GB Star White", 8499),
    "MK254AH/A": ("256GB Night Sky", 8499),
    "MK264AH/A": ("512GB Star White", 9349),
    "MK274AH/A": ("512GB Night Sky", 9349),
    "MK284AH/A": ("1TB Star White", 11049),
    "MK294AH/A": ("1TB Night Sky", 11049),
    "MK2A4AH/A": ("2TB Star White", 13599),
    "MK2C4AH/A": ("2TB Night Sky", 13599),
}

# Same rule as watcher.py's pickup_available(), applied to the stored quote:
# "Check back for availability on 23/10" is a future date, not stock. Keep in step.
AVAILABLE_RE = re.compile(r"\bavailable\b", re.IGNORECASE)
NOT_NOW_RE = re.compile(r"unavailable|check back", re.IGNORECASE)
HEARTBEAT_RE = re.compile(r"(\d+)\s+SKUs?,\s*earliest\s+(\d{4}-\d{2}-\d{2})")
FINDING_RE = re.compile(r"\b(?:INFO|WARNING) (?:EARLIER|BUYABLE|TARGET|PICKUP|CHANGED) ")
CONFLICT_RE = re.compile(r"\b409\b|Conflict")
# The watcher logs "telegram <method> failed"; only a failed send is a send failure,
# a failed getUpdates is a poll failure.
SEND_FAIL_RE = re.compile(r"telegram sendMessage failed")
POLL_FAIL_RE = re.compile(r"telegram getUpdates failed")
EVENTS = ("ok_poll", "conflict", "takeover", "quiet", "finding", "command",
          "pickup_failed", "send_failed", "poll_failed", "error")
DEADMAN_SILENT_SECONDS = 900


def pickup_entry_available(entry):
    """A stored entry reads 'Store name: quote'. Count it only if the quote means stock now."""
    _store, _sep, quote = entry.partition(": ")
    quote = quote or entry
    return not NOT_NOW_RE.search(quote) and bool(AVAILABLE_RE.search(quote))


def ts_of_date(text):
    day = dt.date.fromisoformat(text)
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp()


def parse_iso(text):
    when = dt.datetime.fromisoformat(text)
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def read_heartbeat():
    """Age in seconds and what the heartbeat body says, or None if there is none."""
    try:
        age = max(0.0, time.time() - HEARTBEAT.stat().st_mtime)
        body = HEARTBEAT.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = HEARTBEAT_RE.search(body)
    return {
        "age": age,
        "skus": int(found.group(1)) if found else None,
        "earliest": ts_of_date(found.group(2)) if found else None,
    }


class ContainerIndex:
    """Finds a container's directory under /var/lib/docker/containers by name."""

    def __init__(self):
        self.dirs = {}
        self.scanned_at = 0.0

    def dir_for(self, name):
        # Rescan every 30 s, so a recreated container (new id) is found again.
        if time.time() - self.scanned_at > 30 or name not in self.dirs:
            self.scanned_at = time.time()
            self.dirs = {}
            if CONTAINERS.is_dir():
                for d in CONTAINERS.iterdir():
                    try:
                        cfg = json.loads((d / "config.v2.json").read_text())
                    except (OSError, ValueError):
                        continue
                    self.dirs[cfg.get("Name")] = d
        return self.dirs.get(name)

    def info(self, name):
        d = self.dir_for(name)
        if d is None:
            return None
        try:
            cfg = json.loads((d / "config.v2.json").read_text())
        except (OSError, ValueError):
            return None
        state = cfg.get("State") or {}
        try:
            started = dt.datetime.fromisoformat((state.get("StartedAt") or "")[:19])
            started = started.replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            started = None
        return {"dir": d, "cfg": cfg, "running": bool(state.get("Running")),
                "started": started}


def standby_state(index):
    """The standby's state file (numbers and dates only are used), plus its container info."""
    info = index.info(STANDBY_NAME)
    if info is None:
        return None, None
    try:
        volume = info["cfg"]["MountPoints"]["/data"]["Name"]
        state = json.loads((VOLUMES / volume / "_data" / "state.json").read_text())
    except (KeyError, TypeError, OSError, ValueError):
        state = None
    return state, info


def watcher_role(info):
    """'primary' or 'standby', as the container was started with; None if unknown.

    Only the ROLE entry is kept. The rest of the environment, tokens included,
    is not stored anywhere.
    """
    if info is None:
        return None
    env = (info["cfg"].get("Config") or {}).get("Env") or []
    for entry in env:
        if entry.startswith("ROLE="):
            role = entry[len("ROLE="):].strip().lower()
            return role if role in ("primary", "standby") else None
    return None


def primary_beat(role, checked_age, sink):
    """What the page judges the primary by.

    While the primary runs in this container, its last successful Apple poll is
    its heartbeat. Otherwise it is the heartbeat the sink wrote from a home machine.
    """
    if role == "primary" and checked_age is not None:
        return {"age": checked_age, "skus": None, "earliest": None}
    return sink


class LogTail:
    """Counts events in the standby's json log, incrementally, across rotations."""

    def __init__(self):
        self.dir = None
        self.path = None
        self.offset = 0
        self.inode = None
        self.totals = {event: 0 for event in EVENTS}
        self.mode = -1  # 1 taking over, 0 quiet, -1 no transition seen yet

    def follow(self, container_dir):
        if container_dir != self.dir:
            # A recreated container has a new id and a new log: count it from its start.
            # A watcher starts quiet, so the old container's last transition no longer holds.
            self.dir = container_dir
            self.path = container_dir / f"{container_dir.name}-json.log"
            self.offset = 0
            self.inode = None
            self.mode = 0

    def poll(self):
        if self.path is None:
            return
        try:
            st = self.path.stat()
        except OSError:
            return
        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset):
            self.offset = 0  # rotated or truncated: the new file starts from the top
        self.inode = st.st_ino
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            return  # only complete lines are counted
        self.offset += end + 1
        for raw in data[:end + 1].splitlines():
            try:
                text = json.loads(raw).get("log", "")
            except (ValueError, AttributeError):
                continue
            self.classify(text)

    def classify(self, text):
        t = self.totals
        if " ok — " in text:
            t["ok_poll"] += 1
        if CONFLICT_RE.search(text):
            t["conflict"] += 1
        if "taking over alerts" in text:
            t["takeover"] += 1
            self.mode = 1
        if "going quiet" in text:
            t["quiet"] += 1
            self.mode = 0
        if FINDING_RE.search(text):
            t["finding"] += 1
        if "command /" in text:
            t["command"] += 1
        if "pickup lookup failed" in text:
            t["pickup_failed"] += 1
        if SEND_FAIL_RE.search(text):
            t["send_failed"] += 1
        if POLL_FAIL_RE.search(text):
            t["poll_failed"] += 1
        if "unexpected error" in text:
            t["error"] += 1


def tcp_up(host, port, timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return 1
    except OSError:
        return 0


def telegram_up():
    try:
        with urllib.request.urlopen(TELEGRAM_URL, timeout=10) as r:
            return 1 if r.status < 500 else 0
    except urllib.error.HTTPError as e:
        return 1 if e.code < 500 else 0  # an answer, even a redirect, means reachable
    except (urllib.error.URLError, OSError, ValueError):
        return 0


class Probes:
    """Outside checks, refreshed in the background once a minute."""

    def __init__(self):
        self.tcp = None
        self.services = None
        self.telegram = None

    def run(self):
        while True:
            try:
                tcp = {name: tcp_up(host, port) for name, host, port in TCP_PROBES}
                services = {name: tcp_up(host, port, timeout=2)
                            for name, host, port in SERVICE_PROBES}
                telegram = telegram_up()
                self.tcp, self.services, self.telegram = tcp, services, telegram
            except Exception:
                traceback.print_exc(file=sys.stderr)
            time.sleep(60)


class Metrics:
    def __init__(self):
        self.families = {}

    def add(self, name, kind, help_text, value, labels=None):
        if value is None:
            return
        family = self.families.setdefault(name, (kind, help_text, []))
        family[2].append((labels or {}, value))

    def render(self):
        lines = []
        for name, (kind, help_text, samples) in self.families.items():
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in samples:
                if labels:
                    inside = ",".join(f'{k}="{v}"' for k, v in labels.items())
                    lines.append(f"{name}{{{inside}}} {value}")
                else:
                    lines.append(f"{name} {value}")
        return "\n".join(lines) + "\n"


def collect(app):
    m = Metrics()
    now = time.time()
    m.add("duo_fleet_last_update_timestamp_seconds", "gauge",
          "When the fleet exporter last collected.", now)

    state_doc, standby = standby_state(app.index)
    doc = state_doc or {}
    role = watcher_role(standby)
    checked_age = None
    if doc.get("checked_at"):
        checked_age = max(0.0, now - parse_iso(doc["checked_at"]).timestamp())

    hb = primary_beat(role, checked_age, read_heartbeat())
    m.add("duo_heartbeat_age_seconds", "gauge",
          "Seconds since the primary's last successful Apple poll; -1 if there is none. "
          "While the primary runs in Germany this is that container's poll, else the sink heartbeat.",
          hb["age"] if hb else -1)
    if hb:
        m.add("duo_heartbeat_skus", "gauge",
              "SKU count in the last heartbeat body.", hb["skus"])
        m.add("duo_heartbeat_earliest_timestamp_seconds", "gauge",
              "Earliest ship date in the last heartbeat body, as a timestamp.", hb["earliest"])
    m.add("duo_deadman_incident", "gauge",
          "1 while the deadman has an open 'watcher silent' incident.",
          1 if DEADMAN_FLAG.exists() else 0)
    m.add("duo_standby_container_primary", "gauge",
          "1 if the container in Germany runs with ROLE=primary, 0 if as standby.",
          None if role is None else int(role == "primary"))

    amnezia = app.index.info(AMNEZIA_NAME)
    for label, info in (("standby", standby), ("amnezia", amnezia)):
        m.add("duo_container_running", "gauge", "1 if the container is running.",
              1 if info and info["running"] else 0, {"name": label})
        if info and info["started"]:
            m.add("duo_container_started_timestamp_seconds", "gauge",
                  "When the container last started.", info["started"], {"name": label})

    m.add("duo_standby_checked_age_seconds", "gauge",
          "Seconds since the container's last successful Apple poll.", checked_age)
    for subject, rec in (doc.get("health") or {}).items():
        m.add("duo_standby_health_fail_streak", "gauge",
              "Failures in a row for this subject, as the standby sees it.",
              rec.get("fail_streak", 0), {"subject": subject})
    parts = doc.get("parts") or {}
    for pn, (name, price) in SKU.items():
        rec = parts.get(pn) or {}
        labels = {"pn": pn, "name": name}
        m.add("duo_sku_price_aed", "gauge", "Price in AED.", price, labels)
        if rec.get("date"):
            m.add("duo_sku_ship_timestamp_seconds", "gauge",
                  "Ship date for this SKU, as a timestamp.", ts_of_date(rec["date"]), labels)
        if "buyable" in rec:
            m.add("duo_sku_buyable", "gauge", "1 if the SKU can be ordered now.",
                  1 if rec.get("buyable") else 0, {"pn": pn})
        m.add("duo_sku_pickup_stores", "gauge",
              "Stores where the phone can be picked up now (future dates excluded).",
              sum(1 for entry in rec.get("pickup") or [] if pickup_entry_available(entry)),
              {"pn": pn})

    if standby is not None:
        app.tail.follow(standby["dir"])
        app.tail.poll()
    for event in EVENTS:
        m.add("duo_standby_log_events_total", "counter",
              "Events in the standby's current log file, counted from its first line "
              "or from the last rotation.",
              app.tail.totals[event], {"event": event})
    m.add("duo_standby_mode", "gauge",
          "1 while the standby is taking over alerts, 0 while quiet, -1 unknown.",
          app.tail.mode)

    probes = app.probes
    if probes.tcp is not None:
        for name, _host, _port in TCP_PROBES:
            m.add("duo_host_tcp_up", "gauge", "1 if the host's port accepts a connection.",
                  probes.tcp[name], {"name": name})
    if probes.services is not None:
        for name, _host, _port in SERVICE_PROBES:
            m.add("duo_service_up", "gauge",
                  "1 if the local service port accepts a connection.",
                  probes.services[name], {"name": name})
    if probes.telegram is not None:
        m.add("duo_telegram_reachable", "gauge",
              "1 if api.telegram.org answers from this server.", probes.telegram)
    return m


class App:
    def __init__(self, probes):
        self.probes = probes
        self.index = ContainerIndex()
        self.tail = LogTail()
        self.lock = threading.Lock()

    def render(self):
        with self.lock:
            return collect(self).render()


def make_handler(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_error(404)
                return
            try:
                body = app.render().encode("utf-8")
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            """Prometheus scrapes every 30 s; logging each one would be noise."""

    return Handler


def main():
    probes = Probes()
    threading.Thread(target=probes.run, daemon=True).start()
    host, port = LISTEN.rsplit(":", 1)
    server = http.server.ThreadingHTTPServer((host, int(port)), make_handler(App(probes)))
    server.serve_forever()


if __name__ == "__main__":
    main()
