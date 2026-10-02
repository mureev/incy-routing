"""Reading geo files, and matching names the way Xray does."""

from __future__ import annotations

import io

from incy_routing import convert
from incy_routing.geo import GeoFile, Matcher


def test_tags_and_entries_round_trip(geo):
    site, ip = (GeoFile(p) for p in geo)
    assert {"private", "category-ru", "telegram"} <= set(site.index)
    assert {"private", "ru", "telegram"} <= set(ip.index)
    assert ("domain", "vk.com") in site.domains("CATEGORY-RU")
    assert "Category-RU" in site and "ru" not in site


class TestMatcher:
    def test_suffix_matches_on_a_label_boundary_only(self):
        m = Matcher(["domain:example.com"])
        assert m.match("example.com") and m.match("a.b.example.com")
        assert not m.match("notexample.com")

    def test_full_keyword_and_regexp(self):
        m = Matcher(["full:a.example", "keyword:gram", "regexp:^cdn[0-9]+\\."])
        assert m.match("a.example") and not m.match("b.a.example")
        assert m.match("instagram.com")
        assert m.match("cdn42.example.net") and not m.match("cdnx.example.net")

    def test_geosite_tags_expand(self, geo):
        m = Matcher(["geosite:category-ru"], GeoFile(geo[0]))
        assert m.match("gosuslugi.ru") and m.match("vk.com") and not m.match("youtube.com")


class TestCovers:
    def test_a_single_label_regexp_does_not_cover_a_suffix(self, geo):
        # geosite:private accepts every single-label name, "ru" included; it does not cover all of *.ru
        private = Matcher(["geosite:private"], GeoFile(geo[0]))
        assert private.match("ru")
        assert not private.covers("domain", "ru")

    def test_a_parent_suffix_covers_its_children(self):
        m = Matcher(["domain:ru"])
        assert m.covers("domain", "bank.ru") and m.covers("full", "www.bank.ru")
        assert not m.covers("domain", "bank.com")

    def test_a_keyword_covers_suffixes_containing_it(self):
        assert Matcher(["keyword:bank"]).covers("domain", "mybank.example")


def test_an_unknown_tag_is_an_error(tmp_path, geo):
    conf = tmp_path / "c.conf"
    conf.write_text("[Rule]\nFINAL,PROXY\n")
    out = io.StringIO()
    code = convert.main([str(conf), "--name", "T", "--slug", "t", "--out-dir", str(tmp_path / "o"),
                         "--add-direct-site", "geosite:ru", "--geosite", str(geo[0]), "--geoip", str(geo[1])],
                        stdout=out)
    assert code == 1
    assert "geosite:ru is not in the geosite file" in out.getvalue()


def test_entries_a_tag_already_covers_are_pruned(tmp_path, geo):
    conf = tmp_path / "c.conf"
    conf.write_text("[Rule]\nDOMAIN-SUFFIX,bank.ru,DIRECT\nDOMAIN-SUFFIX,bank.com,DIRECT\n"
                    "DOMAIN-SUFFIX,ru,DIRECT\nFINAL,PROXY\n")
    out = io.StringIO()
    convert.main([str(conf), "--name", "T", "--slug", "t", "--out-dir", str(tmp_path / "o"),
                  "--add-direct-site", "geosite:category-ru", "--geosite", str(geo[0]), "--geoip", str(geo[1])],
                 stdout=out)
    sites = (tmp_path / "o" / "t.json").read_text()
    assert "domain:bank.ru" not in sites  # covered by category-ru's domain:ru
    assert "domain:bank.com" in sites
    assert '"domain:ru"' in sites  # a TLD-level suffix is never pruned
