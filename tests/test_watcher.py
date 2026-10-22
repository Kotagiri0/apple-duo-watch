"""Regression tests for watcher.py. Standard library only, no network.

    python3 -I -m unittest discover -s tests -v

Telegram is replaced at the HTTP-call level only (Telegram._call), so the
real send/poll_commands/chat_id code runs. Fakes that bypass real code are
how a type mismatch once dropped every message the owner sent while every
test still passed.
"""
import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import watcher as w  # noqa: E402

logging.getLogger("duo").setLevel(logging.CRITICAL)

OWNER = "1423567839"
GUEST = 55500111
STRANGER = 99900222
PN = "MK244AH/A"
PN2 = "MK2C4AH/A"


class BotAPI:
    """Stands in for api.telegram.org: records sends, serves queued updates."""

    def __init__(self):
        self.sent = []          # (chat_id, text, params)
        self.updates = []       # (numeric chat id, username, text)
        self.blocked = set()

    def __call__(self, method, **kw):
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return {"ok": True, "result": [
                {"update_id": i, "message": {
                    "chat": {"id": c}, "from": {"username": u}, "text": t}}
                for i, (c, u, t) in enumerate(ups)]}
        if method == "sendMessage":
            target = str(kw["chat_id"])
            self.sent.append((target, kw["text"], kw))
            if target in self.blocked:
                return {"ok": False, "description": "bot was blocked"}
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        return {"ok": True, "result": True}

    def to(self, chat):
        return [text for c, text, _ in self.sent if c == str(chat)]


class Store:
    """A controllable Apple store: delivery per part, and pickup."""

    def __init__(self, parts):
        self.delivery = {pn: ("Ships Jan 22", "2027-01-22") for pn in parts}
        self.pickup = {}
        self.pickup_fails = False
        self.pickup_calls = 0

    def fetch_delivery(self, parts, today):
        return {pn: {"label": lab, "buyable": True, "reason": None, "date": d}
                for pn, (lab, d) in self.delivery.items() if pn in parts}

    def fetch_pickup(self, parts, city):
        self.pickup_calls += 1
        if self.pickup_fails:
            return None
        return {pn: list(v) for pn, v in self.pickup.items()}


class Case(unittest.TestCase):
    parts = {PN: ("256GB Star White", 8499), PN2: ("2TB Night Sky", 13599)}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state_file = os.path.join(self.dir, "state.json")
        self.store = Store(self.parts)
        for name, fn in (("fetch_delivery", self.store.fetch_delivery),
                         ("fetch_pickup", self.store.fetch_pickup)):
            p = mock.patch.object(w, name, fn)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for var in ("TELEGRAM_ADMINS", "HEALTHCHECK_URL", "HISTORY_FILE"):
            os.environ.pop(var, None)
        self.cfg = self.make_cfg()
        self.api = BotAPI()
        self.tg = w.Telegram("token", OWNER, None)
        self.tg._call = self.api
        self.rt = w.Runtime(self.cfg, [self.tg], w.Healthcheck(""), w.RingLog())

    def make_cfg(self, **kw):
        args = dict(interval=10, city="Dubai", state=self.state_file,
                    alert_before=None, only=None, pickup_every=6,
                    blind_after=600)
        args.update(kw)
        cfg = w.Config(argparse.Namespace(**args))
        cfg.parts = dict(self.parts)
        return cfg

    def state(self):
        return w.load_state(self.state_file)

    def check(self, **kw):
        events, current, pickup = w.check_once(self.cfg, [self.tg], **kw)
        self.rt.current, self.rt.pickup = current, pickup
        self.rt.checked_at = w.now_iso()
        return [e[0] for e in events]

    def say(self, chat, user, text):
        self.api.updates = [(chat, user, text)]
        before = len(self.api.sent)
        w.handle_commands(self.rt, self.tg)
        return self.api.sent[before:]


# ------------------------------------------------------------------ watchdog

class WatchdogTest(Case):
    def age(self, subject, minutes):
        s = self.state()
        s["health"][subject]["failing_since"] = (
            dt.datetime.now().astimezone() - dt.timedelta(minutes=minutes)
        ).isoformat(timespec="seconds")
        w.save_state(self.state_file, s)

    def test_one_alert_per_incident_and_one_recovery(self):
        wd = w.Watchdog(self.state_file, after=600)
        self.assertIsNone(wd.record("apple", False, "boom", [self.tg]))
        self.age("apple", 11)
        self.assertEqual(wd.record("apple", False, "boom", [self.tg])[0], "blind")
        self.assertIsNone(wd.record("apple", False, "boom", [self.tg]))
        self.assertEqual(wd.record("apple", True, notifiers=[self.tg])[0], "back")
        self.assertIsNone(wd.record("apple", True, notifiers=[self.tg]))
        self.assertEqual(len(self.api.to(OWNER)), 2)

    def test_undelivered_alert_is_retried(self):
        self.api.blocked.add(OWNER)
        wd = w.Watchdog(self.state_file, after=600)
        wd.record("apple", False, "boom", [self.tg])
        self.age("apple", 11)
        self.assertEqual(wd.record("apple", False, "x", [self.tg])[0], "blind")
        self.assertNotIn("alerted", self.state()["health"]["apple"])
        self.api.blocked.clear()
        self.assertEqual(wd.record("apple", False, "x", [self.tg])[0], "blind")
        self.assertTrue(self.state()["health"]["apple"]["alerted"])

    def test_restart_keeps_the_open_incident(self):
        wd = w.Watchdog(self.state_file, after=600)
        wd.record("apple", False, "boom", [self.tg])
        self.age("apple", 11)
        wd.record("apple", False, "boom", [self.tg])
        again = w.Watchdog(self.state_file, after=600)
        self.assertIsNone(again.record("apple", False, "boom", [self.tg]))

    def test_error_text_is_escaped_for_telegram_html(self):
        html = w.render_health_alert("blind", "apple", {
            "last_error": "<urlopen error [Errno -3]>", "fail_streak": 3,
            "failing_since": "x"}, 700)
        self.assertIn("&lt;urlopen error", html)
        self.assertNotIn("<urlopen", html)


# ------------------------------------------------------------------- access

class AccessTest(Case):
    def setUp(self):
        super().setUp()
        self.check()                                  # baseline

    def test_owner_from_env_text_matches_numeric_update(self):
        # .env gives "1423567839", getUpdates gives 1423567839
        self.assertTrue(self.say(int(OWNER), "owner", "/help"))

    def test_chat_id_is_normalised_on_every_assignment(self):
        for given in (OWNER, int(OWNER), f"  {OWNER}  "):
            self.assertEqual(w.Telegram("t", given, None).chat_id, OWNER)
        for empty in (None, "", "   "):
            self.assertIsNone(w.Telegram("t", empty, None).chat_id)

    def test_stranger_gets_nothing_and_changes_nothing(self):
        for cmd in ("/status", "/pause", "/addadmin @self", "/admins"):
            self.assertEqual(self.say(STRANGER, "rando", cmd), [])
        s = self.state()
        self.assertNotIn("admins", s)
        self.assertFalse(s.get("paused"))

    def test_default_is_owner_only(self):
        self.assertEqual(self.cfg.admin_seed, [])
        self.assertEqual(w.recipient_ids(self.state(), OWNER), [OWNER])

    def test_handle_invitation_is_claimed_once_and_pinned(self):
        os.environ["TELEGRAM_ADMINS"] = "@Example_Handle"
        cfg = self.make_cfg()
        s = self.state()
        w.seed_admins(cfg, s)
        w.save_state(self.state_file, s)
        out = self.say(GUEST, "example_handle", "/status")
        self.assertTrue(out)
        self.assertEqual(out[0][0], str(GUEST))      # reply to the asker
        self.assertEqual(self.state()["admins"][str(GUEST)]["username"],
                         "example_handle")
        # the handle is spent: somebody else using it later gets nothing
        self.assertEqual(self.say(STRANGER, "example_handle", "/status"), [])

    def test_only_admins_add_and_the_owner_cannot_be_removed(self):
        s = self.state()
        s["admins"] = {str(GUEST): {"username": "guest"}}
        w.save_state(self.state_file, s)
        self.say(GUEST, "guest", "/addadmin 777000333")
        self.assertIn("777000333", self.state()["admins"])
        self.assertTrue(any("изменил список" in t for t in self.api.to(OWNER)))
        out = self.say(GUEST, "guest", f"/deladmin {OWNER}")
        self.assertIn("нельзя", out[0][1])
        self.assertTrue(self.say(int(OWNER), "owner", "/status"))

    def test_check_by_another_admin_does_not_hijack_the_owner_chat(self):
        s = self.state()
        s["admins"] = {str(GUEST): {"username": "guest"}}
        w.save_state(self.state_file, s)
        self.say(GUEST, "guest", "/check")
        self.assertEqual(self.tg.chat_id, OWNER)
        self.assertEqual(str(self.state().get("chat_id")), OWNER)


# ---------------------------------------------------------------- findings

class FindingsTest(Case):
    def setUp(self):
        super().setUp()
        s = w.load_state(self.state_file)
        s["admins"] = {str(GUEST): {"username": "guest"}}
        w.save_state(self.state_file, s)
        self.check()                                  # baseline

    def earlier(self, date="2026-12-02", label="Ships Dec 02"):
        self.store.delivery[PN] = (label, date)

    def test_finding_reaches_owner_and_admins_with_a_button(self):
        self.earlier()
        self.assertEqual(self.check(), ["EARLIER"])
        for chat in (OWNER, str(GUEST)):
            self.assertTrue(self.api.to(chat))
        params = self.api.sent[-1][2]
        self.assertIn(PN, params["reply_markup"])

    def test_one_blocked_admin_does_not_cost_the_rest_the_news(self):
        self.api.blocked.add(str(GUEST))
        self.earlier()
        self.assertEqual(self.check(), ["EARLIER"])
        self.assertEqual(self.check(), [])            # marked seen
        self.assertEqual(len(w.load_history(self.cfg.history_file)), 1)

    def test_nobody_took_it_so_it_is_found_again(self):
        self.api.blocked.update({OWNER, str(GUEST)})
        self.earlier()
        self.assertEqual(self.check(), ["EARLIER"])
        self.assertEqual(self.check(), ["EARLIER"])   # retried
        self.assertEqual(w.load_history(self.cfg.history_file), [])

    def test_history_lives_outside_the_state_file(self):
        self.earlier()
        self.check()
        self.assertNotIn("history", self.state())
        hist = w.load_history(self.cfg.history_file)
        self.assertEqual((hist[0]["old_date"], hist[0]["new_date"]),
                         ("2027-01-22", "2026-12-02"))

    def test_pause_records_without_sending_and_resume_replays(self):
        self.say(int(OWNER), "owner", "/pause")
        before = len(self.api.sent)
        self.earlier()
        self.assertEqual(self.check(), ["EARLIER"])
        self.assertEqual(len(self.api.sent), before)  # nothing sent
        self.check()                                  # quiet cycle
        self.assertEqual(len(self.state()["paused_queue"]), 1)
        out = self.say(int(OWNER), "owner", "/resume")
        self.assertIn("накопилось находок: 1", out[0][1])
        self.assertFalse(self.state()["paused"])


# ------------------------------------------------------------------ pickup

class PickupTest(Case):
    def setUp(self):
        super().setUp()
        self.check(with_pickup=True)                  # baseline

    def test_pickup_alert_once_and_a_failed_lookup_does_not_repeat_it(self):
        self.store.pickup = {PN: ["Dubai Mall: Available Today"]}
        self.assertEqual(self.check(with_pickup=True), ["PICKUP"])
        self.assertEqual(self.check(with_pickup=True), [])
        self.store.pickup_fails = True
        self.assertEqual(self.check(with_pickup=True), [])
        self.assertEqual(self.state()["parts"][PN]["pickup"],
                         ["Dubai Mall: Available Today"])
        self.store.pickup_fails = False
        self.assertEqual(self.check(with_pickup=True), [])

    def test_pickup_is_looked_up_at_once_when_delivery_moves(self):
        calls = self.store.pickup_calls
        self.assertEqual(self.check(with_pickup=False), [])
        self.assertEqual(self.store.pickup_calls, calls)       # no extra call
        self.store.delivery[PN] = ("Ships Dec 02", "2026-12-02")
        self.store.pickup = {PN: ["Yas Mall: Available Today"]}
        self.assertEqual(sorted(self.check(with_pickup=False)),
                         ["EARLIER", "PICKUP"])
        self.assertEqual(self.store.pickup_calls, calls + 1)

    def test_parser_reads_apples_real_response_shape(self):
        resp = {"body": {"stores": [
            {"storeName": "Dubai Mall", "partsAvailability": {
                PN: {"pickupDisplay": "unavailable"},
                PN2: {"pickupDisplay": "available",
                      "pickupSearchQuote": "Available Today"}}},
            {"storeName": "Yas Mall", "partsAvailability": {
                PN2: {"pickupDisplay": "unavailable"}}}]}}
        real = WatcherSource.fetch_pickup
        with mock.patch.object(w, "_shop_json", lambda path, params: resp):
            self.assertEqual(real(self.parts, "Dubai"),
                             {PN2: ["Dubai Mall: Available Today"]})

    def test_failed_lookup_is_none_not_empty(self):
        def boom(path, params):
            raise urllib.error.URLError("down")
        with mock.patch.object(w, "_shop_json", boom):
            self.assertIsNone(WatcherSource.fetch_pickup(self.parts, "Dubai"))


class WatcherSource:
    """The real fetchers, captured before any test patches the module."""
    fetch_pickup = staticmethod(w.fetch_pickup)


# ----------------------------------------------------------------- urgency

class UrgencyTest(unittest.TestCase):
    def capture_ntfy(self, events, priority):
        sent = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def urlopen(req, timeout=None):
            sent.append(dict(req.header_items()))
            return Resp()
        with mock.patch.object(urllib.request, "urlopen", urlopen):
            w.Ntfy("http://ntfy.invalid/topic").send(
                "x", "x", w.alert_markup(events), priority=priority)
        return sent[0]

    def ev(self, kind):
        return (kind, PN2, "2TB Night Sky", 13599, "x", w.link_for(PN2))

    def test_what_is_loud_and_what_is_quiet(self):
        for kind in ("EARLIER", "BUYABLE", "TARGET", "PICKUP"):
            self.assertEqual(w.urgency([self.ev(kind)]), "urgent", kind)
        self.assertEqual(w.urgency([self.ev("CHANGED")]), "low")
        self.assertEqual(w.urgency([self.ev("CHANGED"), self.ev("EARLIER")]),
                         "urgent")

    def test_ntfy_headers(self):
        h = self.capture_ntfy([self.ev("EARLIER")], "urgent")
        self.assertEqual(h.get("Priority"), "5")
        self.assertEqual(h.get("Tags"), "rotating_light")
        self.assertTrue(h.get("Click", "").endswith(PN2))
        self.assertEqual(self.capture_ntfy([self.ev("CHANGED")], "low")
                         .get("Priority"), "2")

    def test_telegram_quiet_only_for_low(self):
        api = BotAPI()
        tg = w.Telegram("t", OWNER, None)
        tg._call = api
        tg.send("a", priority="low")
        tg.send("b", priority="urgent")
        tg.send("c")
        self.assertEqual([p.get("disable_notification") for _, _, p in api.sent],
                         ["true", None, None])


# -------------------------------------------------------------------- sink

class SinkTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "rel").mkdir()
        (self.dir / "rel" / "MANIFEST").write_text("manifest")
        (self.dir / "secret").write_text("outside")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        env = dict(os.environ, HEARTBEAT_FILE=str(self.dir / "hb"),
                   RELEASE_DIR=str(self.dir / "rel"),
                   LISTEN_PORT=str(self.port))
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(ROOT / "deploy" / "duo-heartbeat-sink.py")],
            env=env)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.terminate)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        self.fail("sink did not start")

    def get(self, path, data=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                        timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""

    def test_serves_release_files_and_nothing_else(self):
        self.assertEqual(self.get("/release/MANIFEST"), (200, b"manifest"))
        self.assertEqual(self.get("/release/.env")[0], 404)
        self.assertEqual(self.get("/release/Dockerfile")[0], 404)  # missing

    def test_path_traversal_leads_nowhere(self):
        for path in ("/release/../secret", "/release/%2e%2e%2fsecret"):
            with socket.create_connection(("127.0.0.1", self.port)) as s:
                s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                self.assertIn(b" 404 ", s.recv(200))

    def test_release_fetch_is_not_a_heartbeat(self):
        self.get("/release/MANIFEST")
        self.assertFalse((self.dir / "hb").exists())
        self.assertEqual(self.get("/beat", b"8 SKUs")[0], 204)
        self.assertEqual((self.dir / "hb").read_text(), "8 SKUs")



class OnlyKindsTest(Case):
    """The pickup-only fallback: read everything, announce its own kind."""

    def test_pickup_only_announces_pickup_and_drops_delivery(self):
        self.check()                                    # baseline
        with mock.patch.dict(os.environ, {"ONLY_KINDS": "pickup"}):
            self.cfg = self.make_cfg()
            self.store.delivery[PN] = ("Ships Dec 02", "2026-12-02")
            self.assertEqual(self.check(), [])           # delivery: not ours
            self.assertEqual(self.state()["parts"][PN]["date"], "2026-12-02")
            self.store.pickup = {PN: ["Dubai Mall: Available Today"]}
            self.assertEqual(self.check(with_pickup=True), ["PICKUP"])
        self.assertIn("🏬", self.api.to(OWNER)[-1])

    def test_unknown_kind_is_refused_at_start(self):
        with mock.patch.dict(os.environ, {"ONLY_KINDS": "PICKUPS"}):
            with self.assertRaises(SystemExit):
                self.make_cfg()

class PickupRulesTest(unittest.TestCase):
    """Apple's real wording: a future date ('Check back ... 23/10') is not stock."""

    def test_display_and_quote_combinations(self):
        cases = [
            ("default", "Check back for availability on 23/10", False),
            ("unavailable", "Unavailable for Pickup", False),
            ("default", "Available Today", True),
            ("available", "available", True),
            ("", "", False),
            ("default", "", False),
        ]
        for display, quote, want in cases:
            self.assertEqual(w.pickup_available(display, quote), want, (display, quote))

    def test_the_real_check_back_payload_yields_no_pickup(self):
        resp = {"body": {"stores": [{"storeName": "Dubai Mall", "partsAvailability": {
            "MK244AH/A": {
                "pickupDisplay": "default",
                "pickupSearchQuote": "Check back for availability on 23/10",
                "messageTypes": {"regular": {
                    "storePickupQuote": "Check back for availability on 23/10"}}}}}]}}
        with mock.patch.object(w, "_shop_json", lambda path, params: resp):
            got = WatcherSource.fetch_pickup({"MK244AH/A": ("x", 1)}, "Dubai")
        self.assertEqual(got, {})

    def test_an_available_store_still_counts(self):
        resp = {"body": {"stores": [{"storeName": "Yas Mall", "partsAvailability": {
            "MK244AH/A": {"pickupDisplay": "default",
                          "pickupSearchQuote": "Available Today"}}}]}}
        with mock.patch.object(w, "_shop_json", lambda path, params: resp):
            got = WatcherSource.fetch_pickup({"MK244AH/A": ("x", 1)}, "Dubai")
        self.assertEqual(got, {"MK244AH/A": ["Yas Mall: Available Today"]})


if __name__ == "__main__":
    unittest.main()
