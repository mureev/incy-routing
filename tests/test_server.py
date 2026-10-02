"""What the service answers, and when it builds."""

from __future__ import annotations

import http.client
import json
import threading
from datetime import UTC, datetime

import pytest

from incy_routing import build
from incy_routing.server import Service, serve

from conftest import SOURCE


@pytest.fixture
def service(data_dir, mirror):
    build.run(data_dir, source=SOURCE, mirror=mirror, geo_refresh=False, log=lambda m: None)
    return Service(data_dir, source=SOURCE, mirror=mirror, geo_refresh=False)


@pytest.fixture
def client(service):
    httpd = serve(service, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def request(method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    yield request
    httpd.shutdown()


def test_serves_each_profile_as_json(client):
    for slug, name in [("ru-direct", "RU direct"), ("blocked-only", "Blocked only"), ("sr-geo-literal", "SR geo (literal)")]:
        resp, body = client("GET", f"/{slug}.json")
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "application/json; charset=utf-8"
        assert resp.getheader("Cache-Control") == "no-cache"
        assert json.loads(body)["Name"] == name


def test_revalidation_answers_304(client):
    resp, _ = client("GET", "/ru-direct.json")
    resp2, body2 = client("GET", "/ru-direct.json", {"If-None-Match": resp.getheader("ETag")})
    assert resp2.status == 304 and body2 == b""


def test_head_has_no_body(client):
    resp, body = client("HEAD", "/ru-direct.json")
    assert resp.status == 200 and body == b"" and int(resp.getheader("Content-Length")) > 1000


@pytest.mark.parametrize("path", ["/", "/public/ru-direct.json", "/../status.json", "/.ru-direct.json.tmp",
                                  "/ru-direct.deeplink", "/status.json", "/geo/geosite.dat", "/nope.json"])
def test_nothing_else_is_served(client, path):
    resp, _ = client("GET", path)
    assert resp.status == 404


def test_only_reads_are_allowed(client):
    resp, _ = client("POST", "/ru-direct.json")
    assert resp.status == 405


def test_health_and_status(client):
    resp, body = client("GET", "/healthz")
    assert resp.status == 200 and body == b"ok\n"
    resp, body = client("GET", "/status")
    status = json.loads(body)
    assert status["stale"] is False and status["last_error"] is None
    assert set(status["profiles"]) == {"ru-direct", "blocked-only", "sr-geo-literal"}
    assert status["profiles"]["ru-direct"]["name"] == "RU direct"


def test_the_server_names_itself_and_nothing_more(client):
    resp, _ = client("GET", "/healthz")
    assert resp.getheader("Server") == "incy-routing"


class TestSchedule:
    def test_next_run_is_today_or_tomorrow_at_the_set_time(self, service):
        service.jitter_min = 0
        assert service.next_run(datetime(2026, 10, 2, 0, 30, tzinfo=UTC)) == datetime(2026, 10, 2, 1, 45, tzinfo=UTC)
        assert service.next_run(datetime(2026, 10, 2, 1, 45, tzinfo=UTC)) == datetime(2026, 10, 3, 1, 45, tzinfo=UTC)

    def test_a_fresh_build_is_not_rebuilt_on_start(self, service):
        assert service.needs_build_on_start() is False

    def test_an_empty_data_dir_builds_on_start(self, tmp_path):
        assert Service(tmp_path).needs_build_on_start() is True

    def test_a_day_old_success_builds_on_start(self, service, data_dir):
        status = json.loads((data_dir / "status.json").read_text())
        status["last_success"] = "2026-01-01T00:00:00Z"
        (data_dir / "status.json").write_text(json.dumps(status))
        assert service.needs_build_on_start() is True
