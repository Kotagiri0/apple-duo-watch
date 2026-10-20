#!/usr/bin/env python3
"""Remember when the watcher last said it was alive, and hand out releases.

Runs on the proxy VPS and listens on loopback only; the watcher reaches it
from the other machine through the same SSH tunnel that carries the Telegram
proxy. Two jobs on one port, so the tunnel needs no second forward:

  POST /beat (or any other path)  rewrite the heartbeat file; its mtime is
                                  the time of the last successful check, and
                                  duo-deadman.sh notices it going stale
  GET  /release/<name>            serve a file of the current signed release,
                                  which the NanoPi pulls (duo-update.sh)

Fetching a release is deliberately not a heartbeat: the updater asking for
files must never be mistaken for the watcher being alive. Standard library
only, like the watcher itself.

  HEARTBEAT_FILE  file to rewrite on every beat (default /run/duo-watch/heartbeat)
  RELEASE_DIR     directory of the live release (default /var/lib/duo-release)
  LISTEN_PORT     loopback port to listen on (default 8899)
"""
import http.server
import os
import pathlib

PATH = pathlib.Path(os.environ.get("HEARTBEAT_FILE",
                                   "/run/duo-watch/heartbeat"))
RELEASE_DIR = pathlib.Path(os.environ.get("RELEASE_DIR",
                                          "/var/lib/duo-release"))
PORT = int(os.environ.get("LISTEN_PORT") or 8899)
# The watcher sends a line like "8 SKUs, earliest 2027-01-22"; anything much
# longer is not a heartbeat and the body is ignored.
NOTE_LIMIT = 200
# The only names that are ever served. The requested path never reaches the
# filesystem as typed, so "../" and friends lead nowhere.
RELEASE_FILES = {"MANIFEST", "MANIFEST.sig", "watcher.py", "Dockerfile",
                 "docker-compose.yml", "duo-update.sh", "duo-update.service",
                 "duo-update.timer"}


class Sink(http.server.BaseHTTPRequestHandler):
    server_version = "duo-heartbeat/2"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        # One request per connection: an unread oversized body can then
        # never be parsed as a second request.
        self.close_connection = True

    def reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def beat(self):
        length = int(self.headers.get("Content-Length") or 0)
        note = self.rfile.read(length) if 0 < length <= NOTE_LIMIT else b""
        PATH.write_bytes(note or b"ok")
        self.reply(204)

    def release(self, name):
        if name not in RELEASE_FILES:
            return self.reply(404)
        try:
            body = (RELEASE_DIR / name).read_bytes()
        except OSError:
            return self.reply(404)
        self.reply(200, body, "application/octet-stream")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/release/"):
            return self.release(path[len("/release/"):])
        return self.beat()

    do_POST = do_PUT = beat

    def log_message(self, *args):
        """A line per beat would be a log entry every minute, forever."""


if __name__ == "__main__":
    PATH.parent.mkdir(parents=True, exist_ok=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Sink).serve_forever()
