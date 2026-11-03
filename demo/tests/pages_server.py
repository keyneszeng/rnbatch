"""Serve Cloudflare Pages' generated dist/ with real _headers during CI.

SimpleHTTPServer alone ignores Pages' _headers file, so browser tests
otherwise miss Content-Security-Policy and worker-src regressions.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "dist"
HEADERS = {}
for line in (ROOT / "_headers").read_text(encoding="utf-8").splitlines():
    if line.startswith("  ") and ":" in line:
        key, value = line.strip().split(":", 1)
        HEADERS[key.strip()] = value.strip()


class PagesHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        for key, value in HEADERS.items():
            self.send_header(key, value)
        super().end_headers()


if __name__ == "__main__":
    handler = partial(PagesHandler, directory=str(ROOT))
    with ThreadingHTTPServer(("127.0.0.1", 4173), handler) as server:
        server.serve_forever()
