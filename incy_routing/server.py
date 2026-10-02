"""Serve the built profiles and rebuild them once a day.

    GET /<slug>.json   a published profile (ETag, Last-Modified, Cache-Control: no-cache)
    GET /status        JSON: build, last attempt and success, staleness, what is published
    GET /healthz       200 while the process is up; nothing more (a liveness probe should never claim more)

Everything else is 404, and nothing is listed. Put it behind a reverse proxy at a path of your choosing; the
service itself neither knows nor cares what that path is.

Configuration, all optional (environment):
    INCY_DATA_DIR      /data       state, cache, geo files and the published profiles
    INCY_PORT          8080
    INCY_BUILD_AT      01:45       daily build time, UTC (04:45 in Moscow, after misha's 03:00 MSK rebuild)
    INCY_JITTER_MIN    15          up to this many minutes are added at random each day
    INCY_STALE_HOURS   50          /status says "stale" when the last success is older than this
    INCY_SOURCE        misha-tgshv's repo on raw.githubusercontent.com
    INCY_MIRROR        read upstream from <dir>/<host>/<path> instead of the network (tests, offline)
    INCY_GEO_REFRESH   1           0 = never download geo files; they must already be in <data>/geo
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import signal
import sys
import threading
from datetime import UTC, datetime, timedelta
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import build
from .profiles import PROFILES

RETRIES = 5  # after a failed scheduled build, try again hourly this many times, then wait for the next day
SLUG = re.compile(r"^/([a-z0-9-]+)\.json$")


def log(message: str) -> None:
    print(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {message}", flush=True)


class Service:
    def __init__(self, data_dir: Path, *, build_at: str = "01:45", jitter_min: int = 15, stale_hours: float = 50,
                 source: str = build.SOURCE, mirror: Path | None = None, geo_refresh: bool = True):
        self.data_dir = data_dir
        self.public = data_dir / "public"
        self.hour, self.minute = (int(x) for x in build_at.split(":"))
        self.jitter_min = jitter_min
        self.stale_hours = stale_hours
        self.source, self.mirror, self.geo_refresh = source, mirror, geo_refresh
        self.slugs = {p["slug"] for p in PROFILES}
        self.lock = threading.Lock()
        self.stop = threading.Event()

    # ---------------------------------------------------------------- building

    def build_now(self) -> dict:
        with self.lock:
            return build.run(self.data_dir, source=self.source, mirror=self.mirror,
                             geo_refresh=self.geo_refresh, log=log)

    def status(self) -> dict:
        try:
            status = json.loads((self.data_dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
        last = status.get("last_success")
        age = None
        if last:
            age = (datetime.now(UTC) - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)).total_seconds()
        profiles = {}
        for slug in sorted(self.slugs):
            path = self.public / f"{slug}.json"
            if path.exists():
                body = path.read_bytes()
                data = json.loads(body)
                profiles[slug] = {"name": data.get("Name"), "last_updated": data.get("LastUpdated"),
                                  "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        return {
            "build": os.environ.get("APP_BUILD", "dev"),
            "built_at": os.environ.get("APP_BUILT_AT") or None,
            "last_attempt": status.get("last_attempt"),
            "last_success": last,
            "last_error": status.get("error"),
            "stale": age is None or age > self.stale_hours * 3600,
            "profiles": profiles,
        }

    def needs_build_on_start(self) -> bool:
        if any(not (self.public / f"{slug}.json").exists() for slug in self.slugs):
            return True
        last = self.status()["last_success"]
        if not last:
            return True
        age = datetime.now(UTC) - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        return age > timedelta(hours=24)

    def next_run(self, after: datetime) -> datetime:
        run = after.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if run <= after:
            run += timedelta(days=1)
        return run + timedelta(seconds=random.randint(0, self.jitter_min * 60))

    def schedule(self) -> None:
        if self.needs_build_on_start():
            log("start-up build: nothing published yet, or the last success is over a day old")
            failures = 0 if self.build_now().get("error") is None else 1
        else:
            failures = 0
        while not self.stop.is_set():
            now = datetime.now(UTC)
            if 0 < failures <= RETRIES:
                when = now + timedelta(hours=1)
            else:
                failures = 0
                when = self.next_run(now)
            log(f"next build at {when:%Y-%m-%d %H:%M}Z")
            if self.stop.wait((when - now).total_seconds()):
                return
            failures = 0 if self.build_now().get("error") is None else failures + 1


# -------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    service: Service  # set on the subclass made in serve()
    server_version = "incy-routing"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # no access log: the reverse proxy keeps one
        pass

    def version_string(self) -> str:
        return self.server_version

    def _send(self, code: int, body: bytes = b"", headers: dict | None = None) -> None:
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if code != 304:  # a 304 has no body, and its length is not this one
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._send(200, b"ok\n", {"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store"})
        if path == "/status":
            body = (json.dumps(self.service.status(), indent=2) + "\n").encode()
            return self._send(200, body, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"})
        m = SLUG.match(path)
        if m and m.group(1) in self.service.slugs:
            file = self.service.public / f"{m.group(1)}.json"
            try:
                body = file.read_bytes()
                mtime = file.stat().st_mtime
            except OSError:
                return self._send(503, b"not built yet\n", {"Content-Type": "text/plain; charset=utf-8", "Retry-After": "120"})
            etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
            headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache",
                       "ETag": etag, "Last-Modified": formatdate(mtime, usegmt=True)}
            if self.headers.get("If-None-Match") == etag:
                return self._send(304, b"", {k: v for k, v in headers.items() if k != "Content-Type"})
            return self._send(200, body, headers)
        return self._send(404, b"", {"Content-Type": "text/plain; charset=utf-8"})

    do_HEAD = do_GET

    def _refuse(self):
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"", {"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse


def serve(service: Service, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> int:
    env = os.environ.get
    service = Service(
        Path(env("INCY_DATA_DIR", "/data")),
        build_at=env("INCY_BUILD_AT", "01:45"),
        jitter_min=int(env("INCY_JITTER_MIN", "15")),
        stale_hours=float(env("INCY_STALE_HOURS", "50")),
        source=env("INCY_SOURCE", build.SOURCE),
        mirror=Path(env("INCY_MIRROR")) if env("INCY_MIRROR") else None,
        geo_refresh=env("INCY_GEO_REFRESH", "1") != "0",
    )
    port = int(env("INCY_PORT", "8080"))
    httpd = serve(service, port)
    log(f"incy-routing {env('APP_BUILD', 'dev')} on :{port}, daily build at {service.hour:02d}:{service.minute:02d}Z "
        f"(+ up to {service.jitter_min} min), profiles: {', '.join(sorted(service.slugs))}")

    def shutdown(signum, frame):
        service.stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=service.schedule, name="scheduler", daemon=True).start()
    httpd.serve_forever()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
