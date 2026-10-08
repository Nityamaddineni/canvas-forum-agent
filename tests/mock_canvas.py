"""A small in-process mock of the Canvas endpoints the agent may touch."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "1234~SECRETTOKENVALUEabcdefghijklmnop"
COURSE, TOPIC, ME = "11", "22", 100
BASE = f"/api/v1/courses/{COURSE}/discussion_topics/{TOPIC}"
PAGE = 2


class MockCanvas:
    def __init__(self):
        self.topic_message = "<p>COURSE-TEAM CONTROL: RUNNING</p><p>Discussion topic.</p>"
        self.entries = []        # top-level and replies, in creation order
        self.next_id = 1000
        self.log = []            # (method, path)
        self.on_post = None      # callback(path, message) run just before a POST is applied
        self.get_failures = []   # statuses returned for the next GETs
        self.post_status = None  # force a status for POSTs (nothing applied)
        self.index_lag = 0       # listings (first page of /entries) that hide the newest POST; set per POST
        self.lag_next_post = 0   # index_lag assigned to the next applied POST
        self.lag_fresh = False
        self.hidden_ids = set()  # ids not yet "indexed"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def add(self, user_id, name, message, parent_id=None):
        self.next_id += 1
        e = {"id": self.next_id, "user_id": user_id, "user_name": name, "message": message,
             "created_at": f"2026-10-07T12:{self.next_id // 60 % 60:02d}:{self.next_id % 60:02d}Z", "parent_id": parent_id}
        self.entries.append(e)
        return e

    def posts(self, method="POST"):
        return [p for m, p in self.log if m == method]

    # --- http ---
    def _visible(self):
        return [e for e in self.entries if e["id"] not in self.hidden_ids]

    def _top(self, e):
        replies = [r for r in self._visible() if r["parent_id"] == e["id"]]
        out = {k: v for k, v in e.items() if k != "parent_id"}
        out["recent_replies"] = replies[:3]
        out["has_more_replies"] = len(replies) > 3
        return out

    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body, headers=None):
                data = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _page(self, rows, url):
                q = parse_qs(urlparse(self.path).query)
                page = int(q.get("page", ["1"])[0])
                chunk = rows[(page - 1) * PAGE: page * PAGE]
                hdr = {}
                if page * PAGE < len(rows):
                    hdr["Link"] = f'<{mock.url}{urlparse(self.path).path}?page={page + 1}>; rel="next"'
                self._send(200, chunk, hdr)

            def do_GET(self):
                path = urlparse(self.path).path
                mock.log.append(("GET", path))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"errors": "unauthorized"})
                if mock.get_failures:
                    return self._send(mock.get_failures.pop(0), {"errors": "boom"})
                if path == "/api/v1/users/self":
                    return self._send(200, {"id": ME, "name": "Me"})
                if path == BASE:
                    return self._send(200, {"id": int(TOPIC), "message": mock.topic_message})
                if path == BASE + "/entries":
                    if "page" not in parse_qs(urlparse(self.path).query):  # start of a listing pass
                        if mock.lag_fresh:  # the first pass after the POST is always hidden
                            mock.lag_fresh = False
                        elif mock.index_lag > 0:
                            mock.index_lag -= 1
                            if mock.index_lag == 0:
                                mock.hidden_ids.clear()
                    tops = [mock._top(e) for e in mock._visible() if e["parent_id"] is None]
                    return self._page(tops, path)
                m = re.fullmatch(BASE + r"/entries/(\d+)/replies", path)
                if m:
                    rows = [r for r in mock._visible() if r["parent_id"] == int(m.group(1))]
                    return self._page(rows, path)
                self._send(404, {"errors": "not found"})

            def do_POST(self):
                path = urlparse(self.path).path
                mock.log.append(("POST", path))
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                message = parse_qs(body).get("message", [""])[0]
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {})
                if mock.post_status:
                    return self._send(mock.post_status, {"errors": "forced"})
                if mock.on_post:
                    mock.on_post(path, message)
                parent = None
                if path != BASE + "/entries":
                    m = re.fullmatch(BASE + r"/entries/(\d+)/replies", path)
                    if not m:
                        return self._send(404, {})
                    parent = int(m.group(1))
                e = mock.add(ME, "Me", f"<p>{message}</p>", parent)
                if mock.lag_next_post:
                    mock.hidden_ids.add(e["id"])
                    mock.index_lag, mock.lag_next_post = mock.lag_next_post, 0
                    mock.lag_fresh = True
                self._send(200, {"id": e["id"]})

            def do_DELETE(self):
                mock.log.append(("DELETE", self.path))
                self._send(200, {})

        return H
