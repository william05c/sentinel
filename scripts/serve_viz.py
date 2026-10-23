"""Static server for the visualisations.

Serves viz/ over HTTP so the page can fetch data.json (file:// is blocked by CORS),
with caching disabled so a regenerated data.json is picked up on the next poll rather
than after a hard refresh.

Usage:  python scripts/serve_viz.py [port]
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "viz"
DEFAULT_PORT = 8777


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self.path = "/crawler.html"
        super().do_GET()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    if not ROOT.exists():
        raise SystemExit("no viz directory at " + str(ROOT))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"serving {ROOT}", flush=True)
        print(f"  http://127.0.0.1:{port}/crawler.html", flush=True)
        print(f"  http://127.0.0.1:{port}/provenance.html", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
