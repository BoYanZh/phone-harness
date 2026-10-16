"""push and pull: the file routes, a storage link each way, and the phone's
url.open. No cloud: the API, the phone and the store are stand-ins."""
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from phone_harness import cloud, transfer  # noqa: E402


class Store(BaseHTTPRequestHandler):
    objects = {}

    def log_message(self, *a):
        pass

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        Store.objects[self.path.split("?")[0]] = (self.rfile.read(n), self.headers.get("Content-Type"))
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        body, _ = Store.objects.get(self.path.split("?")[0], (None, None))
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b""))); self.end_headers()
        if body:
            self.wfile.write(body)


class FakePhone:
    def __init__(self, ops=("url.open",)):
        self.ops, self.opened = set(ops), []

    def supports(self, op):
        return op in self.ops

    def send(self, op, **kw):
        self.opened.append((op, kw))
        return "com.apple.mobilesafari"


class Transfer(unittest.TestCase):
    def setUp(self):
        Store.objects = {}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Store)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close); self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.phone = FakePhone()
        self.calls = []
        sess = {"sid": "abc123def456", "link": {"kind": "control", "url": "https://api.test/iphone/c/tok", "token": "t"}}
        self.api_replies = {}

        def api(method, path, body=None, **kw):
            self.calls.append((method, path, body))
            return self.api_replies[(method, path)]
        self.p = [mock.patch.object(cloud, "attached", lambda kind=None: sess), mock.patch.object(cloud, "_api", api),
                  mock.patch.object(transfer, "Remote", lambda url, token: self.phone), mock.patch.object(transfer, "POLL_SECONDS", 0)]
        for patcher in self.p:
            patcher.start(); self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)

    def test_push_uploads_then_the_phone_opens_the_download_link(self):
        src = Path(self.tmp.name) / "cat photo.jpg"; src.write_bytes(b"\xff\xd8jpeg")
        self.api_replies[("POST", "/sessions/abc123def456/files")] = {
            "id": "0123456789abcdef", "filename": "cat_photo.jpg", "content_type": "image/jpeg", "expires_at": 1_800_000_000,
            "upload_url": self.base + "/t/file?sig=up", "download_url": self.base + "/t/file?sig=down"}
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(transfer.push([str(src)]), 0)
        self.assertEqual(self.calls, [("POST", "/sessions/abc123def456/files", {"filename": "cat photo.jpg", "content_type": "image/jpeg"})])
        self.assertEqual(Store.objects["/t/file"], (b"\xff\xd8jpeg", "image/jpeg"))
        self.assertEqual(self.phone.opened, [("url.open", {"url": self.base + "/t/file?sig=down"})])
        self.assertIn("cat_photo.jpg", out.getvalue())
        self.assertNotIn("sig=", out.getvalue())                     # links are credentials: never printed

    def test_pull_opens_the_inbox_page_waits_and_downloads_under_the_phone_s_name(self):
        self.api_replies[("POST", "/sessions/abc123def456/files/inbox")] = {
            "id": "0123456789abcdef", "page_url": self.base + "/t/inbox.html?sig=page", "expires_at": 1_800_000_000}
        states = iter([{"id": "0123456789abcdef", "state": "waiting"},
                       {"id": "0123456789abcdef", "state": "uploaded", "filename": "IMG_0001.jpeg", "size": 5,
                        "content_type": "image/jpeg", "download_url": self.base + "/t/file?sig=get", "expires_at": 1_800_000_000}])
        self.api_replies[("GET", "/sessions/abc123def456/files/0123456789abcdef")] = None
        real = cloud._api

        def api(method, path, body=None, **kw):
            if method == "GET":
                return next(states)
            return real(method, path, body)
        Store.objects["/t/file"] = (b"bytes", "image/jpeg")
        out = io.StringIO()
        with mock.patch.object(cloud, "_api", api), redirect_stdout(out):
            self.assertEqual(transfer.pull(["--out", self.tmp.name]), 0)
        self.assertEqual(self.phone.opened, [("url.open", {"url": self.base + "/t/inbox.html?sig=page"})])
        result = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual((result["path"], result["size"], result["sha256"]),
                         (str(Path(self.tmp.name) / "IMG_0001.jpeg"), 5, hashlib.sha256(b"bytes").hexdigest()))
        self.assertEqual(Path(result["path"]).read_bytes(), b"bytes")
        self.assertNotIn("sig=", out.getvalue())

    def test_a_phone_without_url_open_is_refused_before_anything_is_uploaded(self):
        self.phone.ops = set()
        src = Path(self.tmp.name) / "a.jpg"; src.write_bytes(b"x")
        with self.assertRaises(SystemExit) as e:
            transfer.push([str(src)])
        self.assertIn("url.open", str(e.exception))
        self.assertEqual(self.calls, [])

    def test_an_adb_session_is_refused(self):
        with mock.patch.object(cloud, "attached", lambda kind=None: {"sid": "x", "link": {"kind": "adb", "host": "h", "port": 1}}):
            with self.assertRaises(SystemExit) as e:
                transfer.pull([])
        self.assertIn("adb", str(e.exception))


if __name__ == "__main__":
    unittest.main()
