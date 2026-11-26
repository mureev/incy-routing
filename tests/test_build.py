"""A build publishes all of the profiles or none of them."""

from __future__ import annotations

import io
import json
import os
import time

import pytest

from incy_routing import build, publication
from incy_routing.profiles import PROFILES

import geodata
from conftest import SOURCE

pytestmark = pytest.mark.usefixtures("rule_validator")

SLUGS = [p["slug"] for p in PROFILES]


def go(data_dir, mirror):
    return build.run(data_dir, source=SOURCE, mirror=mirror, geo_refresh=False, log=lambda m: None)


def published(data_dir):
    return {s: json.loads((publication.published_dir(data_dir) / f"{s}.json").read_text()) for s in SLUGS}


def test_publishes_every_profile_from_the_upstream_configs(data_dir, mirror):
    status = go(data_dir, mirror)
    assert status["error"] is None
    assert status["results"] == dict.fromkeys(SLUGS, "updated")
    p = published(data_dir)
    assert {s: p[s]["Name"] for s in SLUGS} == {"ru-direct": "RU direct", "blocked-only": "Blocked only",
                                                 "sr-geo-literal": "SR geo (literal)"}
    ru = p["ru-direct"]
    assert ru["GlobalProxy"] == "true" and ru["RouteOrder"] == "block-proxy-direct"
    assert "geosite:category-ru" in ru["DirectSites"] and "geoip:ru" in ru["DirectIp"]
    assert "domain:alfabank.com" in ru["DirectSites"]  # a bank outside the Russian zones
    assert "domain:habr.com" in ru["ProxySites"] and "keyword:instagram" in ru["ProxySites"]
    blocked = p["blocked-only"]
    assert blocked["GlobalProxy"] == "false" and blocked["DomainStrategy"] == "AsIs"
    assert "geoip:telegram" in blocked["ProxyIp"] and "149.154.160.0/20" in blocked["ProxyIp"]
    literal = p["sr-geo-literal"]
    assert "domain:ru" not in literal["DirectSites"]  # skip-proxy is not a routing rule
    assert literal["ProxySites"] == ["domain:cp-rp16-normal-ycru.tiktokv.com", "domain:habr.com",
                                     "domain:p16-sign-sg.tiktokcdn.com", "domain:p16-sign-va.tiktokcdn.com",
                                     "domain:sf-ssp.tiktokcdn.com"]


def test_a_second_run_changes_nothing(data_dir, mirror):
    go(data_dir, mirror)
    before = {s: (publication.published_dir(data_dir) / f"{s}.json").stat().st_mtime_ns for s in SLUGS}
    time.sleep(1.1)
    status = go(data_dir, mirror)
    assert status["results"] == dict.fromkeys(SLUGS, "unchanged")
    assert {s: (publication.published_dir(data_dir) / f"{s}.json").stat().st_mtime_ns for s in SLUGS} == before


def test_an_upstream_change_republishes_only_what_it_touches(data_dir, mirror):
    go(data_dir, mirror)
    before = published(data_dir)
    community = mirror / "raw.githubusercontent.com/misha-tgshv/shadowrocket-configuration-file/main/rules/domains_community.list"
    community.write_text(community.read_text() + "DOMAIN-SUFFIX,newly-blocked.example\n")
    time.sleep(1.1)
    status = go(data_dir, mirror)
    assert status["results"] == {"ru-direct": "updated", "blocked-only": "unchanged", "sr-geo-literal": "unchanged"}
    after = published(data_dir)
    assert "domain:newly-blocked.example" in after["ru-direct"]["ProxySites"]
    assert int(after["ru-direct"]["LastUpdated"]) > int(before["ru-direct"]["LastUpdated"])
    assert after["blocked-only"] == before["blocked-only"]


def test_a_gutted_list_publishes_nothing(data_dir, mirror):
    go(data_dir, mirror)
    before = {s: (publication.published_dir(data_dir) / f"{s}.json").read_bytes() for s in SLUGS}
    community = mirror / "raw.githubusercontent.com/misha-tgshv/shadowrocket-configuration-file/main/rules/domains_community.list"
    community.write_text("")
    time.sleep(1.1)
    status = go(data_dir, mirror)
    assert "ru-direct: ProxySites has 5 entries, expected >= 100" in status["error"]
    assert {s: (publication.published_dir(data_dir) / f"{s}.json").read_bytes() for s in SLUGS} == before
    assert status["last_success"] != status["last_attempt"]


def test_a_missing_upstream_file_publishes_nothing(data_dir, mirror):
    go(data_dir, mirror)
    before = {s: (publication.published_dir(data_dir) / f"{s}.json").read_bytes() for s in SLUGS}
    (mirror / "github.com/toneywhitez/zkeen_rules/releases/latest/download/meta_ips.list").unlink()
    status = go(data_dir, mirror)
    assert "blocked-only: the converter exited 2" in status["error"]
    assert {s: (publication.published_dir(data_dir) / f"{s}.json").read_bytes() for s in SLUGS} == before


def test_the_first_build_failing_leaves_nothing_half_published(data_dir, mirror):
    (mirror / "raw.githubusercontent.com/misha-tgshv/shadowrocket-configuration-file/main/conf/sr_ru_basic.conf").unlink()
    status = go(data_dir, mirror)
    assert status["error"] and "last_success" not in status
    assert not list(publication.published_dir(data_dir).glob("*.json"))


def test_each_profile_is_checked_by_xray_before_publication(data_dir, mirror, rule_validator):
    assert go(data_dir, mirror)["error"] is None
    assert [p["Name"] for p in rule_validator] == ["RU direct", "Blocked only", "SR geo (literal)"]


def test_xray_rejection_keeps_the_previous_generation(data_dir, mirror, monkeypatch):
    original = go(data_dir, mirror)
    active = publication.published_dir(data_dir)

    def reject(profile, _geo):
        if profile["Name"] == "Blocked only":
            raise ValueError("invalid regexp")

    monkeypatch.setattr(build, "validate_profile", reject)
    status = go(data_dir, mirror)
    assert "blocked-only: Xray validation failed: invalid regexp" in status["error"]
    assert status["last_success"] == original["last_success"]
    assert publication.published_dir(data_dir) == active


def test_bad_geo_responses_never_replace_the_cache(data_dir, mirror, monkeypatch):
    geo = data_dir / "geo"
    cached = {file.name: file.read_bytes() for file in geo.iterdir()}
    for name in cached:
        os.utime(geo / name, (1, 1))
    monkeypatch.setattr(build.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"<html>error</html>"))
    status = build.run(data_dir, source=SOURCE, mirror=mirror, log=lambda _message: None)
    assert status["error"] is None
    assert {name: (geo / name).read_bytes() for name in cached} == cached
    assert all((geo / name).stat().st_mtime == 1 for name in cached)


def test_pointer_switch_failure_leaves_all_profiles_in_place(data_dir, mirror, monkeypatch):
    go(data_dir, mirror)
    old = publication.published_dir(data_dir)
    (old / "blocked-only.json").write_text("{}")
    (old / "ru-direct.json").write_text("{}")
    before = published(data_dir)
    replace = os.replace

    def fail_switch(path, target):
        if target.name == "current":
            raise OSError("simulated rename failure")
        return replace(path, target)

    monkeypatch.setattr(publication.os, "replace", fail_switch)
    assert "simulated rename failure" in go(data_dir, mirror)["error"]
    assert publication.published_dir(data_dir) == old
    assert published(data_dir) == before
    assert list((data_dir / "generations").iterdir()) == [old]


def test_upgrade_leaves_legacy_profiles_available_for_rollback(data_dir, mirror):
    go(data_dir, mirror)
    current = publication.published_dir(data_dir)
    legacy = data_dir / "public"
    current.rename(legacy)
    (data_dir / "current").unlink()
    before = published(data_dir)
    assert go(data_dir, mirror)["results"] == dict.fromkeys(SLUGS, "unchanged")
    assert publication.published_dir(data_dir) != legacy
    assert published(data_dir) == before
    assert {slug: json.loads((legacy / f"{slug}.json").read_text()) for slug in SLUGS} == before


def test_a_valid_geo_download_is_only_cached_after_profiles_pass(data_dir, mirror, monkeypatch):
    path = data_dir / "geo" / "geosite.dat"
    old = path.read_bytes()
    os.utime(path, (1, 1))
    new = geodata.geosite({**geodata.GEOSITE, "EXTRA": [("domain", "example.test")]})
    monkeypatch.setattr(build.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(new))

    def reject(profile, _geo):
        if profile.get("Name") == "RU direct":
            raise ValueError("invalid routing")

    monkeypatch.setattr(build, "validate_profile", reject)
    assert build.run(data_dir, source=SOURCE, mirror=mirror)["error"]
    assert path.read_bytes() == old and path.stat().st_mtime == 1
    monkeypatch.setattr(build, "validate_profile", lambda *_args: None)
    assert build.run(data_dir, source=SOURCE, mirror=mirror)["error"] is None
    assert path.read_bytes() == new and path.stat().st_mtime > 1
