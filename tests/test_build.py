"""A build publishes all of the profiles or none of them."""

from __future__ import annotations

import json
import time

from incy_routing import build
from incy_routing.profiles import PROFILES

from conftest import SOURCE

SLUGS = [p["slug"] for p in PROFILES]


def go(data_dir, mirror):
    return build.run(data_dir, source=SOURCE, mirror=mirror, geo_refresh=False, log=lambda m: None)


def published(data_dir):
    return {s: json.loads((data_dir / "public" / f"{s}.json").read_text()) for s in SLUGS}


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
    before = {s: (data_dir / "public" / f"{s}.json").stat().st_mtime_ns for s in SLUGS}
    time.sleep(1.1)
    status = go(data_dir, mirror)
    assert status["results"] == dict.fromkeys(SLUGS, "unchanged")
    assert {s: (data_dir / "public" / f"{s}.json").stat().st_mtime_ns for s in SLUGS} == before


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
    before = {s: (data_dir / "public" / f"{s}.json").read_bytes() for s in SLUGS}
    community = mirror / "raw.githubusercontent.com/misha-tgshv/shadowrocket-configuration-file/main/rules/domains_community.list"
    community.write_text("")
    time.sleep(1.1)
    status = go(data_dir, mirror)
    assert "ru-direct: ProxySites has 5 entries, expected >= 100" in status["error"]
    assert {s: (data_dir / "public" / f"{s}.json").read_bytes() for s in SLUGS} == before
    assert status["last_success"] != status["last_attempt"]


def test_a_missing_upstream_file_publishes_nothing(data_dir, mirror):
    go(data_dir, mirror)
    before = {s: (data_dir / "public" / f"{s}.json").read_bytes() for s in SLUGS}
    (mirror / "github.com/toneywhitez/zkeen_rules/releases/latest/download/meta_ips.list").unlink()
    status = go(data_dir, mirror)
    assert "blocked-only: the converter exited 2" in status["error"]
    assert {s: (data_dir / "public" / f"{s}.json").read_bytes() for s in SLUGS} == before


def test_the_first_build_failing_leaves_nothing_half_published(data_dir, mirror):
    (mirror / "raw.githubusercontent.com/misha-tgshv/shadowrocket-configuration-file/main/conf/sr_ru_basic.conf").unlink()
    status = go(data_dir, mirror)
    assert status["error"] and "last_success" not in status
    assert not list((data_dir / "public").glob("*.json"))
