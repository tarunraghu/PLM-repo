"""A mock Windchill-style REST / OData API over the workspace state.

Use it to test pull-based ingestion: paging, a modified-since watermark, authentication, rate
limiting and transient errors. It serves the current state, so run `advance` and the API shows the
new data (the state is reloaded whenever it changes on disk).

  GET /health
  GET /odata/ProdMgmt/Parts?$filter=ModifyStamp gt 2026-11-01T00:00:00Z&$top=100
  GET /odata/ProdMgmt/ChangeNotices, /CadDocuments       (same options)
  GET /odata/ProdMgmt/PartRevisions, /AffectedItems, /BomDeltas, /Supersessions   (no watermark)

Responses follow OData: {"value": [...], "@odata.nextLink": "..."}. Page through with nextLink.
"""

import json
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .db import Database
from .engine import MANIFEST
from .outputs import json_row
from .schema import TABLES

ENTITIES = {
    "Parts": ("windchill_part", True), "ChangeNotices": ("windchill_change_notice", True),
    "CadDocuments": ("windchill_cad_document", True), "PartRevisions": ("windchill_part_revision", False),
    "AffectedItems": ("windchill_change_affected_item", False), "BomDeltas": ("windchill_change_bom_delta", False),
    "Supersessions": ("windchill_part_supersession", False),
}
FILTER = re.compile(r"^\s*(ModifyStamp|modified_at)\s+(gt|ge)\s+(\S+)\s*$", re.IGNORECASE)


class StateSource:
    """Loads the state and reloads it when the manifest changes."""

    def __init__(self, workspace):
        self.manifest = Path(workspace) / "state" / MANIFEST
        self.state_dir = self.manifest.parent
        self._stamp, self._db, self._lock = None, None, threading.Lock()

    def db(self):
        with self._lock:
            stamp = self.manifest.stat().st_mtime_ns
            if stamp != self._stamp:
                self._db, self._stamp = Database.load(self.state_dir), stamp
            return self._db


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        if self.server.verbose:
            super().log_message(*args)

    def _send(self, status, body, headers=None):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        server = self.server
        with server.lock:
            server.requests += 1
            n = server.requests
        url = urlparse(self.path)
        if url.path == "/health":
            return self._send(200, {"status": "ok", "requests": n})
        if server.token and self.headers.get("Authorization") != f"Bearer {server.token}":
            return self._send(401, {"error": {"code": "Unauthorized", "message": "Missing or invalid bearer token"}})
        if server.latency_ms:
            time.sleep(server.latency_ms / 1000)
        if server.throttle_every and n % server.throttle_every == 0:
            return self._send(429, {"error": {"code": "TooManyRequests", "message": "Rate limit exceeded"}},
                              {"Retry-After": "1"})
        if server.error_every and n % server.error_every == 0:
            return self._send(503, {"error": {"code": "ServiceUnavailable", "message": "Try again"}})

        match = re.fullmatch(r"/odata/ProdMgmt/(\w+)", url.path)
        if not match or match.group(1) not in ENTITIES:
            return self._send(404, {"error": {"code": "NotFound", "message": url.path}})
        table, watermark = ENTITIES[match.group(1)]
        query = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        spec = TABLES[table]
        rows = server.source.db().rows(table)
        if "$filter" in query:
            f = FILTER.match(query["$filter"])
            if not f or not watermark:
                return self._send(400, {"error": {"code": "BadRequest", "message": "Unsupported $filter"}})
            try:
                since = datetime.fromisoformat(f.group(3).rstrip("Z"))
            except ValueError:
                return self._send(400, {"error": {"code": "BadRequest", "message": "Bad timestamp"}})
            rows = [r for r in rows if (r["modified_at"] > since if f.group(2).lower() == "gt" else r["modified_at"] >= since)]
        if watermark:
            rows.sort(key=lambda r: (r["modified_at"], tuple(r[c] for c in spec.pk)))
        top = min(int(query.get("$top", server.page_size)), 1000)
        skip = int(query.get("$skiptoken", 0))
        page = rows[skip:skip + top]
        body = {"@odata.context": f"/odata/ProdMgmt/$metadata#{match.group(1)}",
                "value": [json_row(table, r) for r in page]}
        if skip + top < len(rows):
            params = {**{k: v for k, v in query.items() if k != "$skiptoken"}, "$top": str(top),
                      "$skiptoken": str(skip + top)}
            body["@odata.nextLink"] = f"{url.path}?" + "&".join(f"{k}={quote(v, safe='$ :')}" for k, v in params.items())
        self._send(200, body)


def make_server(workspace, host="127.0.0.1", port=8080, token=None, throttle_every=0, error_every=0,
                latency_ms=0, page_size=100, verbose=False):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.source = StateSource(workspace)
    server.token, server.throttle_every, server.error_every = token, throttle_every, error_every
    server.latency_ms, server.page_size, server.verbose = latency_ms, page_size, verbose
    server.requests, server.lock = 0, threading.Lock()
    return server
