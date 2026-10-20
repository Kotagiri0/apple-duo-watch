"""The Mac fallback: who may run, and what it is allowed to see.

    python3 -I -m unittest discover -s tests -v

The probe and the watcher are stubs, so nothing touches the network or the
VPS. What is checked: the decision, the process lifecycle, and the environment
the watcher is started with (no proxy, no heartbeat ping, the right kinds).
"""
import datetime as dt
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "fallback-mac.sh"


def probe(nanopi_age, german_age):
    """A PROBE_CMD that reports the two ages, as the VPS would."""
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    epoch = int(now - nanopi_age)
    iso = dt.datetime.fromtimestamp(now - german_age, dt.timezone.utc)
    return f"printf '%s\\n%s\\n' {epoch} {iso.isoformat(timespec='seconds')}"


class Fallback(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fdir = self.dir / "fallback"
        self.env_file = self.dir / "fallback.env"
        self.env_file.write_text(
            "TELEGRAM_BOT_TOKEN=000:test\nTELEGRAM_CHAT_ID=1\n"
            "TELEGRAM_PROXY=http://172.17.0.1:8888\n"
            "HEALTHCHECK_URL=http://172.17.0.1:8899/beat\nNTFY_URL=\n")
        self.dump = self.dir / "watcher.env"
        stub = self.dir / "stub.sh"
        stub.write_text('#!/bin/sh\nenv > "$DUMP"\nexec sleep 300\n')
        self.watcher = f"/bin/sh {stub}"
        self.addCleanup(self.run_script, "stop")

    def run_script(self, *args, probe=None):
        env = dict(os.environ)
        env.pop("PROBE_CMD", None)
        env.update({"FALLBACK_DIR": str(self.fdir),
                    "FALLBACK_ENV": str(self.env_file),
                    "WATCHER_CMD": self.watcher,
                    "DUMP": str(self.dump),
                    "STALE_AFTER": "300"})
        if probe is not None:
            env["PROBE_CMD"] = probe
        return subprocess.run(["bash", str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def mode(self):
        path = self.fdir / "mode"
        return path.read_text().strip() if path.exists() else None

    def log(self):
        path = self.fdir / "fallback.log"
        return path.read_text() if path.exists() else ""

    def watcher_env(self):
        for _ in range(50):
            if self.dump.exists() and self.dump.stat().st_size:
                break
            time.sleep(0.1)
        return dict(line.split("=", 1) for line in self.dump.read_text().splitlines()
                    if "=" in line)

    def test_nanopi_answering_means_nothing_runs(self):
        r = self.run_script("tick", probe=probe(10, 10))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self.mode())

    def test_nanopi_silent_german_answering_gives_pickup_only(self):
        self.run_script("tick", probe=probe(900, 10))
        self.assertEqual(self.mode(), "pickup")
        env = self.watcher_env()
        self.assertEqual(env.get("ONLY_KINDS"), "PICKUP")
        self.assertEqual(env.get("STATE_FILE"), str(self.fdir / "state.json"))
        self.assertNotIn("TELEGRAM_PROXY", env)      # this Mac reaches Telegram directly
        self.assertNotIn("HEALTHCHECK_URL", env)     # never pings the NanoPi's heartbeat
        self.assertEqual(env.get("TELEGRAM_BOT_TOKEN"), "000:test")

    def test_both_silent_gives_everything(self):
        self.run_script("tick", probe=probe(900, 900))
        self.assertEqual(self.mode(), "full")
        self.assertNotIn("ONLY_KINDS", self.watcher_env())

    def test_switching_restarts_from_a_fresh_baseline(self):
        self.run_script("tick", probe=probe(900, 10))
        (self.fdir / "state.json").write_text("{}")  # what the last run left behind
        self.run_script("tick", probe=probe(900, 900))
        self.assertEqual(self.mode(), "full")
        self.assertFalse((self.fdir / "state.json").exists())

    def test_nanopi_back_stops_the_mac(self):
        self.run_script("tick", probe=probe(900, 900))
        self.run_script("tick", probe=probe(10, 10))
        self.assertIsNone(self.mode())
        self.assertIn("standing down", self.log())

    def test_unknown_changes_nothing(self):
        self.run_script("tick", probe=probe(900, 900))
        pid = (self.fdir / "watcher.pid").read_text()
        r = self.run_script("tick", probe="exit 1")
        self.assertEqual(self.mode(), "full")
        self.assertEqual((self.fdir / "watcher.pid").read_text(), pid)
        self.assertIn("cannot see both", r.stdout)

    def test_refuses_to_start_without_a_token(self):
        self.env_file.write_text("TELEGRAM_CHAT_ID=1\n")
        self.run_script("tick", probe=probe(900, 900))
        self.assertIsNone(self.mode())
        self.assertIn("no TELEGRAM_BOT_TOKEN", self.log())

    def test_status_says_what_the_rules_want(self):
        r = self.run_script("status", probe=probe(900, 10))
        self.assertIn("pickup", r.stdout)
        self.assertIn("не работает", r.stdout)


if __name__ == "__main__":
    unittest.main()
