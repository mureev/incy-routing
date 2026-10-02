"""The Shadowrocket → INCY translation, rule by rule."""

from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path

import pytest

from incy_routing import convert


def run(tmp_path: Path, conf: str, *extra: str, slug: str = "p", **kw) -> tuple[int, dict | None, str]:
    path = tmp_path / "in.conf"
    path.write_text(conf, encoding="utf-8")
    out = io.StringIO()
    code = convert.main([str(path), "--name", "Test", "--slug", slug, "--out-dir", str(tmp_path / "out"), *extra],
                        stdout=out)
    result = tmp_path / "out" / f"{slug}.json"
    return code, (json.loads(result.read_text(encoding="utf-8")) if result.exists() else None), out.getvalue()


CONF = """\
[General]
skip-proxy = 192.168.0.0/16, localhost, *.local, captive.apple.com, *.ru, *.рф
[Rule]
DOMAIN-SUFFIX,example.com,PROXY
DOMAIN,exact.example.org,PROXY
DOMAIN-KEYWORD,instagram,PROXY
DOMAIN-SUFFIX,мвд.рф,PROXY
DOMAIN-WILDCARD,*.cdn?.example.net,PROXY
IP-CIDR,149.154.160.0/20,PROXY,no-resolve
IP-CIDR6,2001:67c:4e8::/48,PROXY,no-resolve
IP-CIDR,127.0.0.1/32,PROXY,no-resolve
DST-PORT,3478,PROXY
USER-AGENT,*YouTube*,PROXY
DOMAIN-SUFFIX,ads.example,REJECT
DOMAIN-SUFFIX,bank.example,DIRECT
FINAL,DIRECT
"""


class TestRules:
    def test_each_rule_type_lands_in_its_bucket(self, tmp_path):
        code, p, _ = run(tmp_path, CONF)
        assert code == 0
        assert p["ProxySites"] == ["domain:example.com", "full:exact.example.org", "keyword:instagram",
                                   "domain:xn--b1aew.xn--p1ai", r"regexp:^[^.]*\.cdn.\.example\.net$"]
        assert p["ProxyIp"] == ["149.154.160.0/20", "2001:67c:4e8::/48"]
        assert p["BlockSites"] == ["domain:ads.example"]
        assert "domain:bank.example" in p["DirectSites"]

    def test_final_sets_global_proxy_and_the_first_policy_sets_route_order(self, tmp_path):
        _, p, _ = run(tmp_path, CONF)
        assert p["GlobalProxy"] == "false"
        assert p["RouteOrder"] == "block-proxy-direct"
        _, p, _ = run(tmp_path, "[Rule]\nGEOIP,RU,DIRECT\nDOMAIN-SUFFIX,x.com,PROXY\nFINAL,PROXY\n", slug="q")
        assert p["GlobalProxy"] == "true"
        assert p["RouteOrder"] == "block-direct-proxy"

    def test_cyrillic_names_become_punycode(self, tmp_path):
        _, p, _ = run(tmp_path, "[Rule]\nDOMAIN-SUFFIX,РФ,DIRECT\nDOMAIN,Мвд.РФ,PROXY\nFINAL,PROXY\n")
        assert "domain:xn--p1ai" in p["DirectSites"]
        assert p["ProxySites"] == ["full:xn--b1aew.xn--p1ai"]

    def test_unsupported_rules_are_dropped_and_reported(self, tmp_path):
        _, _, report = run(tmp_path, CONF)
        assert "DST-PORT rules (no INCY profile equivalent)" in report
        assert "USER-AGENT rules" in report

    def test_a_loopback_address_is_never_sent_to_the_proxy(self, tmp_path):
        _, p, report = run(tmp_path, CONF)
        assert "127.0.0.1/32" not in p["ProxyIp"]
        assert "private/loopback IP sent to PROXY" in report

    def test_private_networks_always_stay_local(self, tmp_path):
        _, p, _ = run(tmp_path, "[Rule]\nFINAL,PROXY\n")
        assert p["DirectSites"][0] == "geosite:private"
        assert {"geoip:private", "192.168.0.0/16", "10.0.0.0/8"} <= set(p["DirectIp"])


class TestSkipProxy:
    def test_is_not_a_routing_rule_by_default(self, tmp_path):
        _, p, report = run(tmp_path, CONF)
        assert "domain:ru" not in p["DirectSites"]
        assert "skip-proxy names not routed" in report

    def test_can_be_treated_as_direct(self, tmp_path):
        _, p, _ = run(tmp_path, CONF, "--skip-proxy-direct")
        assert {"domain:ru", "domain:xn--p1ai", "full:captive.apple.com", "domain:local"} <= set(p["DirectSites"])


class TestDomainStrategy:
    def test_no_resolve_everywhere_means_as_is(self, tmp_path):
        _, p, _ = run(tmp_path, CONF)
        assert p["DomainStrategy"] == "AsIs"

    def test_a_resolving_ip_rule_means_ip_if_non_match(self, tmp_path):
        _, p, _ = run(tmp_path, "[Rule]\nGEOIP,RU,DIRECT\nFINAL,PROXY\n")
        assert p["DomainStrategy"] == "IPIfNonMatch"
        assert "geoip:ru" in p["DirectIp"]


class TestLists:
    def test_rule_set_lines_with_and_without_types(self, tmp_path):
        (tmp_path / "l.list").write_text("# c\nDOMAIN-SUFFIX,a.com\n1.2.3.4\n.b.org\nIP-ASN,123,no-resolve\n")
        _, p, report = run(tmp_path, f"[Rule]\nRULE-SET,{tmp_path / 'l.list'},PROXY\nFINAL,DIRECT\n")
        assert p["ProxySites"] == ["domain:a.com", "domain:b.org"]
        assert p["ProxyIp"] == ["1.2.3.4/32"]
        assert "IP-ASN rules" in report

    def test_include_is_followed_relative_to_the_config(self, tmp_path):
        (tmp_path / "extra.conf").write_text("[Rule]\nDOMAIN-SUFFIX,included.example,PROXY\n")
        _, p, _ = run(tmp_path, "[General]\ninclude = extra.conf\n[Rule]\nFINAL,DIRECT\n")
        assert "domain:included.example" in p["ProxySites"]


class TestStrict:
    CONF = "[Rule]\nRULE-SET,/nonexistent/x.list,PROXY\nFINAL,DIRECT\n"

    def test_a_missing_list_writes_nothing(self, tmp_path):
        code, p, _ = run(tmp_path, self.CONF, "--strict")
        assert code == 2
        assert p is None

    def test_without_strict_it_still_fails_the_exit_code(self, tmp_path):
        code, p, report = run(tmp_path, self.CONF)
        assert code == 2
        assert p is not None and "FAILED" in report


class TestLastUpdated:
    def test_stays_put_while_the_rules_do(self, tmp_path):
        _, first, _ = run(tmp_path, CONF)
        time.sleep(1.1)
        _, second, _ = run(tmp_path, CONF)
        assert second["LastUpdated"] == first["LastUpdated"]

    def test_moves_when_a_rule_changes(self, tmp_path):
        _, first, _ = run(tmp_path, CONF)
        time.sleep(1.1)
        _, second, _ = run(tmp_path, CONF.replace("example.com", "example.net"))
        assert int(second["LastUpdated"]) > int(first["LastUpdated"])


class TestOutputs:
    def test_the_deeplink_decodes_to_the_profile(self, tmp_path):
        run(tmp_path, CONF)
        link = (tmp_path / "out" / "p.deeplink").read_text().strip()
        assert link.startswith("incy://routing/onadd/")
        decoded = json.loads(base64.b64decode(link.removeprefix("incy://routing/onadd/")))
        assert decoded == json.loads((tmp_path / "out" / "p.json").read_text())

    def test_autolink_points_at_the_published_json(self, tmp_path):
        run(tmp_path, CONF, "--publish-base", "https://example.org/r/")
        assert (tmp_path / "out" / "p.autolink").read_text().strip() == \
            "incy://autorouting/onadd/https://example.org/r/p.json"


@pytest.mark.parametrize("url, expected", [
    ("https://cdn.jsdelivr.net/gh/u/r@main/rules/a.list", "https://raw.githubusercontent.com/u/r/main/rules/a.list"),
    ("https://cdn.jsdelivr.net/gh/u/r/a.list", "https://raw.githubusercontent.com/u/r/HEAD/a.list"),
    ("https://example.org/a.list", None),
])
def test_jsdelivr_falls_back_to_github(url, expected):
    assert convert.jsdelivr_to_github(url) == expected


def test_the_mirror_maps_urls_to_files(tmp_path):
    (tmp_path / "example.org" / "x").mkdir(parents=True)
    (tmp_path / "example.org" / "x" / "a.list").write_text("DOMAIN,a.example\n")
    text, ref = convert.Fetcher(mirror=tmp_path).get("https://example.org/x/a.list")
    assert text == "DOMAIN,a.example\n" and ref == "https://example.org/x/a.list"
