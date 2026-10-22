"""The monitoring stack: parsing, rotation, the verdict, and what the page may show.

    python3 -I -m unittest discover -s tests -v

The two modules under deploy/monitoring are loaded by path: they are scripts
that run inside containers, not an installable package.
"""
import importlib.util
import json
import os
import pathlib
import re
import shutil
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MON = ROOT / "deploy" / "monitoring"


def load(name):
    spec = importlib.util.spec_from_file_location(name, MON / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet = load("fleet_exporter")
render = load("render_status")


def log_line(text):
    return json.dumps({"log": text + "\n", "stream": "stdout", "time": "x"}) + "\n"


class LogTailTest(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.container = self.dir / "abc123"
        self.container.mkdir()
        self.path = self.container / "abc123-json.log"

    def write(self, *texts):
        with open(self.path, "a", encoding="utf-8") as f:
            for text in texts:
                f.write(log_line(text))

    def test_counts_each_kind_of_event(self):
        self.write("2026-10-22T10:00:00+0000 INFO ok — 8 SKUs, earliest ship date 2027-01-22",
                   "2026-10-22T10:00:01+0000 WARNING telegram getUpdates failed: HTTP Error 409: Conflict",
                   "2026-10-22T10:00:02+0000 INFO EARLIER 256GB Star White (8499 AED): x",
                   "2026-10-22T10:00:03+0000 INFO command /alerts from 1423567839 (@someone)",
                   "2026-10-22T10:00:04+0000 WARNING pickup lookup failed: HTTP Error 541",
                   "2026-10-22T10:00:05+0000 WARNING telegram sendMessage failed (1 in a row)")
        tail = fleet.LogTail()
        tail.follow(self.container)
        tail.poll()
        t = tail.totals
        self.assertEqual(t["ok_poll"], 1)
        self.assertEqual(t["conflict"], 1)
        self.assertEqual(t["finding"], 1)
        self.assertEqual(t["command"], 1)
        self.assertEqual(t["pickup_failed"], 1)
        self.assertEqual(t["send_failed"], 1)      # the sendMessage line only
        self.assertEqual(t["poll_failed"], 1)      # the getUpdates line is a poll failure

    def test_mode_follows_the_last_transition(self):
        self.write("WARNING primary went quiet (опрашивал 5 мин назад) — taking over alerts")
        tail = fleet.LogTail()
        tail.follow(self.container)
        tail.poll()
        self.assertEqual(tail.mode, 1)
        self.write("INFO primary is answering again (опрашивал 40 с назад) — going quiet")
        tail.poll()
        self.assertEqual(tail.mode, 0)
        self.assertEqual(tail.totals["takeover"], 1)
        self.assertEqual(tail.totals["quiet"], 1)

    def test_counts_only_new_lines_on_each_poll(self):
        self.write("INFO ok — 8 SKUs")
        tail = fleet.LogTail()
        tail.follow(self.container)
        tail.poll()
        tail.poll()
        self.assertEqual(tail.totals["ok_poll"], 1)
        self.write("INFO ok — 8 SKUs", "INFO ok — 8 SKUs")
        tail.poll()
        self.assertEqual(tail.totals["ok_poll"], 3)

    def test_incomplete_last_line_waits_for_its_newline(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(log_line("INFO ok — 8 SKUs").rstrip("\n"))
        tail = fleet.LogTail()
        tail.follow(self.container)
        tail.poll()
        self.assertEqual(tail.totals["ok_poll"], 0)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n")
        tail.poll()
        self.assertEqual(tail.totals["ok_poll"], 1)

    def test_rotation_keeps_totals_and_starts_the_new_file_from_the_top(self):
        self.write("INFO ok — 8 SKUs", "INFO ok — 8 SKUs")
        tail = fleet.LogTail()
        tail.follow(self.container)
        tail.poll()
        os.remove(self.path)
        self.write("INFO ok — 8 SKUs")
        tail.poll()
        self.assertEqual(tail.totals["ok_poll"], 3)

    def test_a_recreated_container_is_followed_without_losing_totals(self):
        self.write("INFO ok — 8 SKUs")
        tail = fleet.LogTail()
        tail.follow(self.container)
        tail.poll()
        newer = self.dir / "def456"
        newer.mkdir()
        (newer / "def456-json.log").write_text(log_line("INFO ok — 8 SKUs"), encoding="utf-8")
        tail.follow(newer)
        tail.poll()
        self.assertEqual(tail.totals["ok_poll"], 2)

    def test_a_recreated_container_starts_quiet_not_in_the_old_takeover(self):
        self.write("WARNING primary went quiet (опрашивал 5 мин назад) — taking over alerts")
        tail = fleet.LogTail()
        tail.follow(self.container)
        tail.poll()
        self.assertEqual(tail.mode, 1)
        newer = self.dir / "def456"
        newer.mkdir()
        (newer / "def456-json.log").write_text(log_line("INFO ok — 8 SKUs"), encoding="utf-8")
        tail.follow(newer)
        tail.poll()
        self.assertEqual(tail.mode, 0)
        self.assertEqual(tail.totals["takeover"], 1)  # the count is kept, the mode is not


class HeartbeatTest(unittest.TestCase):
    def test_reads_age_and_body(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        path = d / "heartbeat"
        path.write_text("8 SKUs, earliest 2027-01-22")
        old = time.time() - 42
        os.utime(path, (old, old))
        original = fleet.HEARTBEAT
        fleet.HEARTBEAT = path
        self.addCleanup(setattr, fleet, "HEARTBEAT", original)
        hb = fleet.read_heartbeat()
        self.assertAlmostEqual(hb["age"], 42, delta=3)
        self.assertEqual(hb["skus"], 8)
        self.assertEqual(hb["earliest"], fleet.ts_of_date("2027-01-22"))

    def test_missing_heartbeat_is_none(self):
        original = fleet.HEARTBEAT
        fleet.HEARTBEAT = pathlib.Path("/definitely/not/here")
        self.addCleanup(setattr, fleet, "HEARTBEAT", original)
        self.assertIsNone(fleet.read_heartbeat())


def fixture(**overrides):
    d = {
        "node_up": 1, "cpu": 12.0, "ram": 40.0, "disk": 30.0, "load1": 0.1,
        "cores": 1, "uptime": 400000, "hb_age": 20.0, "hb_skus": 8.0,
        "hb_earliest": 1798000000.0, "deadman": 0, "standby_running": 1,
        "standby_primary": 0.0,
        "amnezia_running": 1, "standby_mode": 0, "standby_checked_age": 30.0,
        "fail": {"apple": 0.0, "telegram": 0.0}, "tcp": {"nanopi_ssh": 0.0, "pc_ssh": 1.0},
        "services": {"sink": 1.0, "tinyproxy": 1.0, "sshd": 1.0}, "telegram": 1.0,
        "events": {"ok_poll": 100.0, "finding": 0.0},
        "skus": [{"pn": "MK244AH/A", "name": "256GB Star White", "price": 8499.0,
                  "ship": 1798000000.0, "buyable": 1.0, "pickup": 0.0}],
        "series": {"hb_age": [(0.0, 20.0), (300.0, 25.0)],
                   "cpu": [(0.0, 10.0), (300.0, 12.0)],
                   "ram": [(0.0, 40.0), (300.0, 41.0)],
                   "earliest": [(0.0, 1798000000.0), (3600.0, 1797900000.0)],
                   "polls": [(0.0, 120.0), (900.0, 118.0)]},
    }
    d.update(overrides)
    return d


class VerdictTest(unittest.TestCase):
    def test_all_green(self):
        everything_up = fixture(tcp={"nanopi_ssh": 1.0, "pc_ssh": 1.0})
        self.assertEqual(render.assess(everything_up), ("green", []))

    def test_primary_silent_and_nobody_delivering_is_red(self):
        level, reasons = render.assess(fixture(hb_age=2000.0, standby_mode=0))
        self.assertEqual(level, "red")
        self.assertTrue(any("никто" in r or "не ведёт" in r for r in reasons))

    def test_primary_silent_but_standby_delivering_is_yellow(self):
        level, _ = render.assess(fixture(hb_age=2000.0, standby_mode=1))
        self.assertEqual(level, "yellow")

    def test_host_port_down_is_yellow_not_red(self):
        level, reasons = render.assess(fixture(tcp={"nanopi_ssh": 0.0, "pc_ssh": 1.0}))
        self.assertEqual(level, "yellow")
        self.assertTrue(any("nanopi_ssh" in r for r in reasons))

    def test_missing_metrics_are_treated_as_silent(self):
        level, _ = render.assess(fixture(hb_age=None, standby_mode=1))
        self.assertEqual(level, "yellow")

    def test_load_over_limit_is_yellow(self):
        level, reasons = render.assess(fixture(cpu=97.0))
        self.assertEqual(level, "yellow")
        self.assertTrue(any("ЦП" in r for r in reasons))


class PageTest(unittest.TestCase):
    def test_page_renders_and_shows_only_numbers(self):
        html_text = render.page(fixture())
        self.assertIn("<html", html_text)
        self.assertIn("256GB Star White", html_text)
        self.assertIn("<svg", html_text)
        self.assertNotIn("1423567839", html_text)   # the owner's chat id never appears
        self.assertNotIn("Kotagiri0", html_text)    # nor a handle
        self.assertIsNone(re.search(r"\d{8,}:[A-Za-z0-9_-]{30,}", html_text))  # nor a token shape

    def test_empty_series_still_render(self):
        d = fixture(series={"hb_age": [], "cpu": [], "ram": [], "earliest": [], "polls": []})
        html_text = render.page(d)
        self.assertIn("данные копятся", html_text)

    def test_every_dynamic_value_is_escaped(self):
        d = fixture(skus=[{"pn": "<x>", "name": "<script>1</script>", "price": 1.0,
                           "ship": None, "buyable": None, "pickup": None}])
        html_text = render.page(d)
        self.assertNotIn("<script>1</script>", html_text)
        self.assertIn("&lt;script&gt;", html_text)

    def test_spark_handles_flat_series(self):
        out = render.spark([(0.0, 5.0), (60.0, 5.0)])
        self.assertIn("polyline", out)

    def test_uptime_counts_fresh_samples(self):
        self.assertEqual(render.uptime_percent([(0, 10), (1, 2000), (2, 20), (3, -1)]), 50.0)


class PickupCountTest(unittest.TestCase):
    """The page counts pickups by Apple's wording, so a future date is never stock."""

    def test_future_dates_are_not_pickup(self):
        self.assertFalse(fleet.pickup_entry_available(
            "Dubai Mall: Check back for availability on 23/10"))
        self.assertFalse(fleet.pickup_entry_available("Dubai Mall: Unavailable for Pickup"))

    def test_available_today_is_pickup(self):
        self.assertTrue(fleet.pickup_entry_available("Dubai Mall: Available Today"))


class ExporterOutputTest(unittest.TestCase):
    def test_metrics_text_is_prometheus_format(self):
        m = fleet.Metrics()
        m.add("duo_test", "gauge", "A test.", 1, {"name": "x"})
        m.add("duo_none", "gauge", "Skipped.", None)
        text = m.render()
        self.assertIn('# TYPE duo_test gauge', text)
        self.assertIn('duo_test{name="x"} 1', text)
        self.assertNotIn("duo_none", text)


class PrimaryInGermanyTest(unittest.TestCase):
    """While the primary is the container here, its own Apple poll is the heartbeat."""

    def test_the_primary_beat_is_the_last_poll_when_the_primary_is_here(self):
        sink = {"age": 2000.0, "skus": 8, "earliest": 1.0}
        beat = fleet.primary_beat("primary", 30.0, sink)
        self.assertEqual(beat["age"], 30.0)
        self.assertIsNone(beat["skus"])

    def test_the_primary_beat_is_the_sink_otherwise(self):
        sink = {"age": 2000.0, "skus": 8, "earliest": 1.0}
        self.assertIs(fleet.primary_beat("standby", 30.0, sink), sink)
        self.assertIs(fleet.primary_beat("primary", None, sink), sink)
        self.assertIsNone(fleet.primary_beat("standby", None, None))

    def test_only_the_role_is_read_from_the_environment(self):
        info = {"cfg": {"Config": {"Env": ["TELEGRAM_BOT_TOKEN=not-a-real-token", "ROLE=primary"]}}}
        self.assertEqual(fleet.watcher_role(info), "primary")
        self.assertIsNone(fleet.watcher_role({"cfg": {"Config": {"Env": ["ROLE=other"]}}}))
        self.assertIsNone(fleet.watcher_role(None))

    def test_a_silent_primary_in_germany_is_red(self):
        level, _ = render.assess(fixture(standby_primary=1.0, hb_age=2000.0, standby_mode=-1))
        self.assertEqual(level, "red")

    def test_a_healthy_primary_in_germany_is_green(self):
        everything_up = fixture(standby_primary=1.0, hb_age=20.0, standby_mode=-1,
                                tcp={"nanopi_ssh": 1.0, "pc_ssh": 1.0})
        self.assertEqual(render.assess(everything_up), ("green", []))

    def test_the_page_shows_the_container_as_primary(self):
        html_text = render.page(fixture(standby_primary=1.0, standby_mode=-1))
        self.assertIn("Контейнер в Германии", html_text)
        self.assertNotIn("режим: перехват", html_text)


if __name__ == "__main__":
    unittest.main()
