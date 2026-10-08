#!/usr/bin/env python3
"""Serve the parent hop screen on localhost.

    python3 app.py            # recorded GLEIF responses, no network
    python3 app.py --live     # straight from api.gleif.org
    python3 app.py --port 8800 --open
"""
import argparse
import json
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import hop

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "static" / "index.html"
APPROVALS = ROOT / "approvals.jsonl"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def _hostname(value: str) -> str:
    """'http://127.0.0.1:8765' or '127.0.0.1:8765' -> '127.0.0.1'."""
    value = value.split("://", 1)[-1]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def request_is_local(headers) -> bool:
    """Only this machine's own page may approve. Host stops DNS rebinding; Origin and the
    JSON content type stop another site's page from posting at the screen (CSRF)."""
    if _hostname(headers.get("Host") or "") not in LOCAL_HOSTS:
        return False
    origin = headers.get("Origin")
    return origin is None or _hostname(origin) in LOCAL_HOSTS


def handler(mode):
    class H(BaseHTTPRequestHandler):
        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if _hostname(self.headers.get("Host") or "") not in LOCAL_HOSTS:
                return self.send(403, {"error": "local only"})
            u = urlparse(self.path)
            if u.path == "/":
                return self.send(200, INDEX.read_bytes(), "text/html")
            if u.path == "/api/cases":
                return self.send(200, {"mode": mode, "configured": list(hop.CONFIGURED), "cases": hop.CASES})
            if u.path == "/api/hop":
                q = parse_qs(u.query).get("q", [""])[0]
                try:
                    return self.send(200, hop.run(q, mode))
                except LookupError as e:
                    return self.send(404, {"error": str(e)})
                except OSError as e:  # GLEIF unreachable in live mode
                    return self.send(502, {"error": f"GLEIF did not answer: {e}"})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/approve":
                return self.send(404, {"error": "not found"})
            if not request_is_local(self.headers):
                return self.send(403, {"error": "local only"})
            if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
                return self.send(415, {"error": "send application/json"})
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 64_000:
                return self.send(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(n))
                entry = {"approved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "lei": str(body["lei"]),
                         "edited": bool(body.get("edited")), "draft": str(body["draft"])}
            except (ValueError, KeyError, TypeError):
                return self.send(400, {"error": "expected JSON with lei and draft"})
            # ponytail: a local JSONL file stands in for the case manager; nothing is sent anywhere.
            with APPROVALS.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.send(200, {"ok": True, "approved_at": entry["approved_at"], "saved_to": APPROVALS.name})

        def log_message(self, *args):
            pass

    return H


def serve(mode="fixtures", port=0):
    return ThreadingHTTPServer(("127.0.0.1", port), handler(mode))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="query api.gleif.org instead of the recorded fixtures")
    ap.add_argument("--port", type=int, default=0, help="default: any free port")
    ap.add_argument("--open", action="store_true", help="open the screen in the browser")
    a = ap.parse_args()
    srv = serve("live" if a.live else "fixtures", a.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Parent hop ({'live GLEIF' if a.live else 'recorded GLEIF data'}) on {url}  (Ctrl+C to stop)", flush=True)
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
