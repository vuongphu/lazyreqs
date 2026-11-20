"""A small threaded HTTP server used by the compatibility test suite.

The server speaks HTTP/1.1 with ``Content-Length`` framing so the Rust core's
keep-alive pooling is exercised, and offers just enough routes to cover the
``pyureq``/``requests`` surface the tests exercise.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

_JSON = {"hello": "world", "nested": {"a": [1, 2, 3]}, "unicode": "héllo ☃"}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lazyreqs-test/1.0"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, *args):  # silence test output
        pass

    def _send(self, status=200, body=b"", headers=None, content_type=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, headers, "application/json; charset=utf-8")

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path

        if path == "/get":
            args = {key: values for key, values in parse_qs(parts.query).items()}
            self._send_json(
                {
                    "method": self.command,
                    "path": path,
                    "args": args,
                    "headers": dict(self.headers.items()),
                }
            )
        elif path == "/json":
            self._send_json(_JSON)
        elif path == "/text":
            self._send(200, "hello lazyreqs", None, "text/plain; charset=utf-8")
        elif path == "/latin1":
            self._send(
                200, "héllo".encode("latin-1"), None, "text/plain; charset=iso-8859-1"
            )
        elif path == "/binary":
            self._send(
                200, bytes(range(256)), None, "application/octet-stream"
            )
        elif path == "/status":
            code = int(path.rsplit("/", 1)[-1]) if path.count("/") > 1 else 200
            self._send_json({"code": code}, status=code)
        elif path.startswith("/status/"):
            code = int(path.rsplit("/", 1)[-1])
            self._send_json({"code": code}, status=code)
        elif path.startswith("/redirect/"):
            remaining = int(path.rsplit("/", 1)[-1])
            if remaining > 0:
                self._send(
                    302,
                    b"",
                    {"Location": f"/redirect/{remaining - 1}"},
                )
            else:
                self._send_json({"method": self.command, "path": "/get"})
        elif path.startswith("/delay/"):
            seconds = float(path.rsplit("/", 1)[-1])
            time.sleep(seconds)
            self._send_json({"delay": seconds})
        elif path == "/setcookie":
            self._send_json(
                {"set": True},
                headers={
                    "Set-Cookie": "sess=abc123; Path=/; HttpOnly",
                    "X-Extra": "yes",
                },
            )
        elif path == "/setcookies":
            # Two cookies in one response, as separate headers.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "a=1; Path=/")
            self.send_header("Set-Cookie", "b=2; Path=/")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        elif path == "/cookies":
            self._send_json({"cookie": self.headers.get("Cookie")})
        elif path == "/auth":
            self._send_json({"authorization": self.headers.get("Authorization")})
        elif path == "/multiheader":
            self._send_json({"multi": self.headers.get_all("X-Multi")})
        elif path == "/links":
            self._send(
                200,
                b"",
                {
                    "Link": '<https://example.com/next>; rel="next", '
                    '<https://example.com/last>; rel="last"'
                },
            )
        elif path == "/slow":
            # Used by streaming tests.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "11")
            self.end_headers()
            for chunk in (b"hello ", b"lazyreqs"):
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(0.05)
        else:
            self._send_json({"error": "not found", "path": path}, status=404)

    def do_HEAD(self):
        parts = urlsplit(self.path)
        if parts.path == "/get":
            self._send(200, b"", None, "application/json; charset=utf-8")
        elif parts.path.startswith("/status/"):
            code = int(parts.path.rsplit("/", 1)[-1])
            self._send(code, b"", None, "application/json")
        else:
            self._send(404, b"")

    def _echo(self):
        body = self._read_body()
        content_type = self.headers.get("Content-Type") or ""
        parsed = {
            "method": self.command,
            "content_type": content_type,
            "body_text": body.decode("utf-8", errors="replace"),
            "body_len": len(body),
            "headers": dict(self.headers.items()),
        }
        if content_type.startswith("multipart/form-data"):
            boundary = ""
            for part in content_type.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[len("boundary="):].strip('"')
            chunks = body.split(b"--" + boundary.encode("latin-1"))
            fields = []
            for chunk in chunks:
                if not chunk.strip(b"-\r\n"):
                    continue
                head, _, data = chunk.partition(b"\r\n\r\n")
                if not _:
                    continue
                disposition = ""
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-disposition:"):
                        disposition = line.decode("latin-1")
                name = filename = None
                for bit in disposition.split(";"):
                    bit = bit.strip()
                    if bit.startswith("name="):
                        name = bit[len("name="):].strip('"')
                    elif bit.startswith("filename="):
                        filename = bit[len("filename="):].strip('"')
                fields.append(
                    {
                        "name": name,
                        "filename": filename,
                        "data": data.rstrip(b"\r\n").decode("utf-8", errors="replace"),
                    }
                )
            parsed["multipart"] = fields
        self._send_json(parsed)

    do_POST = _echo
    do_PUT = _echo
    do_PATCH = _echo
    do_DELETE = _echo
    do_OPTIONS = _echo


class _ThreadingHTTPServer(ThreadingHTTPServer):
    # Allow bursts of concurrent clients without tripping the accept backlog.
    request_queue_size = 128
    daemon_threads = True


class _TestServer:
    """Context manager owning a threaded HTTP server on ``127.0.0.1``."""

    def __init__(self):
        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        host, port = self._server.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def url(self, path):
        return self.base_url + path
