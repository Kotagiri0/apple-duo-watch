#!/usr/bin/env python3
"""Remember when the watcher last said it was alive.

Runs on the proxy VPS and listens on loopback only; the watcher reaches it
from the other machine through the same SSH tunnel that carries the Telegram
proxy. Every request rewrites the heartbeat file, so its mtime is the time of
the last successful check. duo-deadman.sh is what notices the file going
stale. Standard library only, like the watcher itself.

  HEARTBEAT_FILE  file to rewrite on every beat (default /run/duo-watch/heartbeat)
  LISTEN_PORT     loopback port to listen on (default 8899)
"""
import http.server
import os
import pathlib

PATH = pathlib.Path(os.environ.get("HEARTBEAT_FILE",
                                   "/run/duo-watch/heartbeat"))
PORT = int(os.environ.get("LISTEN_PORT") or 8899)
# The watcher sends a line like "8 SKUs, earliest 2027-01-22"; anything much
# longer is not a heartbeat and the body is ignored.
NOTE_LIMIT = 200


class Sink(http.server.BaseHTTPRequestHandler):
    server_version = "duo-heartbeat/1"
    protocol_version = "HTTP/1.1"

    def beat(self):
        length = int(self.headers.get("Content-Length") or 0)
        note = self.rfile.read(length) if 0 < length <= NOTE_LIMIT else b""
        PATH.write_bytes(note or b"ok")
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = beat

    def log_message(self, *args):
        """A line per beat would be a log entry every minute, forever."""


if __name__ == "__main__":
    PATH.parent.mkdir(parents=True, exist_ok=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Sink).serve_forever()
