"""Translate a Shadowrocket .conf, and the RULE-SETs it pulls in, into an INCY routing profile.

INCY (an Xray-core client) routes with a flat JSON profile: DirectSites/DirectIp, ProxySites/ProxyIp,
BlockSites/BlockIp, plus GlobalProxy for everything that matches nothing. Shadowrocket routes with an
ordered list of rules. This flattens one into the other and reports what it had to drop.

    DOMAIN-SUFFIX,x    -> domain:x       suffix on a label boundary, as in Shadowrocket
    DOMAIN,x           -> full:x
    DOMAIN-KEYWORD,x   -> keyword:x      substring; always prefixed, a bare x is a substring in Xray too
    DOMAIN-WILDCARD,x  -> regexp:^...$
    IP-CIDR(6),x       -> x
    GEOIP,RU           -> geoip:ru
    FINAL,PROXY|DIRECT -> GlobalProxy "true" | "false"
    Cyrillic names     -> punycode: *.рф is domain:xn--p1ai, because Xray matches the punycode name
    skip-proxy         -> nothing by default: on iOS it moves connections from the app's proxy layer to its
                          TUN, where the rules still decide (Surge manual, "Misc Options")
    DST-PORT, IP-ASN, USER-AGENT, AND/OR/NOT, URL-REGEX, PROCESS-NAME -> not expressible; reported

Run `python -m incy_routing.convert --help`. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .geo import GeoFile, Matcher

LOYALSOLDIER = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download"
PRIVATE_IP = ["geoip:private", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
              "169.254.0.0/16", "100.64.0.0/10", "224.0.0.0/4", "255.255.255.255/32"]
REJECT = {"REJECT", "REJECT-DROP", "REJECT-NO-DROP", "REJECT-TINYGIF", "REJECT-200",
          "REJECT-DICT", "REJECT-ARRAY", "REJECT-IMG"}
UNSUPPORTED = {"DST-PORT", "SRC-PORT", "IN-PORT", "IP-ASN", "AND", "OR", "NOT", "USER-AGENT",
               "URL-REGEX", "PROCESS-NAME", "SUBNET", "SCRIPT", "SRC-IP", "PROTOCOL", "GEOSITE"}
BUCKETS = ("block", "proxy", "direct")


# ----------------------------------------------------------------------------------------- fetching

def jsdelivr_to_github(url: str) -> str | None:
    """https://cdn.jsdelivr.net/gh/USER/REPO@REF/PATH -> https://raw.githubusercontent.com/USER/REPO/REF/PATH"""
    m = re.match(r"^https?://(?:cdn|fastly|gcore)\.jsdelivr\.net/gh/([^/]+)/([^/@]+)(?:@([^/]+))?/(.+)$", url)
    if not m:
        return None
    user, repo, ref, path = m.groups()
    return f"https://raw.githubusercontent.com/{user}/{repo}/{ref or 'HEAD'}/{path}"


class Fetcher:
    """Fetch configs and lists by URL or path.

    `mirror` maps every URL to `<mirror>/<host>/<path>` and never touches the network (tests, offline runs).
    jsDelivr's /gh/ path is a CDN copy of a GitHub file and is often unreachable from Russia, so a failed
    jsDelivr download is retried at the file's origin on GitHub.
    """

    def __init__(self, cache_dir: Path | None = None, mirror: Path | None = None, timeout: int = 60):
        self.cache_dir = cache_dir
        self.mirror = mirror
        self.timeout = timeout
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, ref: str, base: str | None = None) -> tuple[str, str]:
        """Return (text, resolved ref). `ref` may be a URL, a path, or relative to `base`."""
        if base and not re.match(r"^[a-z]+://", ref) and not Path(ref).is_absolute():
            if re.match(r"^[a-z]+://", base):
                ref = urllib.parse.urljoin(base, ref)
            else:
                ref = str(Path(base).parent / ref)
        if not re.match(r"^https?://", ref):
            return Path(ref).read_text(encoding="utf-8-sig"), ref
        if self.mirror is not None:
            u = urllib.parse.urlparse(ref)
            return (self.mirror / u.netloc / u.path.lstrip("/")).read_text(encoding="utf-8-sig"), ref
        try:
            text = self._download(ref)
        except Exception:
            alt = jsdelivr_to_github(ref)
            if not alt:
                raise
            text = self._download(alt)
        if self.cache_dir:
            (self.cache_dir / hashlib.sha256(ref.encode()).hexdigest()[:24]).write_text(text, encoding="utf-8")
        return text, ref

    def _download(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "incy-routing"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.read().decode("utf-8-sig", "replace")


# ----------------------------------------------------------------------------------------- model

def to_ascii_host(value: str) -> str | None:
    """A hostname in the form Xray matches: lower case, punycode labels."""
    value = value.strip().strip(".").lower()
    if not value:
        return None
    if value.isascii():
        return value
    try:
        return ".".join(label if label.isascii() else label.encode("idna").decode() for label in value.split("."))
    except UnicodeError:
        return None


def as_cidr(value: str) -> str | None:
    try:
        return str(ipaddress.ip_network(value.strip(), strict=False))
    except ValueError:
        return None


def wildcard_to_regexp(pattern: str) -> str:
    return "^" + "".join("[^.]*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern.lower()) + "$"


def policy_bucket(policy: str) -> str:
    p = policy.strip().upper()
    if p == "DIRECT":
        return "direct"
    if p in REJECT or p.startswith("REJECT"):
        return "block"
    return "proxy"  # PROXY, a proxy group (AUTO, Israel-Group...) or a server name


class Profile:
    def __init__(self):
        self.sites: dict[str, list[str]] = {b: [] for b in BUCKETS}
        self.ips: dict[str, list[str]] = {b: [] for b in BUCKETS}
        self.final: str | None = None
        self.policy_order: list[str] = []
        self.skip_proxy: list[str] = []
        self.dropped: dict[str, list[str]] = {}
        self.notes: list[str] = []
        self.failures: list[str] = []  # a list or include that could not be loaded
        self.resolves_for_ip_rules = False  # Shadowrocket resolves names for GEOIP/IP-CIDR unless no-resolve

    def drop(self, reason: str, item: str) -> None:
        self.dropped.setdefault(reason, []).append(item)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def add_site(self, bucket: str, item: str) -> None:
        if item not in self.sites[bucket]:
            self.sites[bucket].append(item)

    def add_ip(self, bucket: str, item: str) -> None:
        if item not in self.ips[bucket]:
            self.ips[bucket].append(item)

    def saw_policy(self, bucket: str) -> None:
        if bucket != "block" and bucket not in self.policy_order:
            self.policy_order.append(bucket)


# ----------------------------------------------------------------------------------------- parsing

def add_rule(prof: Profile, rtype: str, value: str, bucket: str, origin: str, no_resolve: bool = False) -> None:
    rtype = rtype.strip().upper()
    value = value.split("#", 1)[0].strip()
    if not value:
        return
    if rtype in ("IP-CIDR", "IP-CIDR6", "IP6-CIDR", "GEOIP") and not no_resolve:
        prof.resolves_for_ip_rules = True
    if rtype in ("DOMAIN-SUFFIX", "DOMAIN"):
        host = to_ascii_host(value)
        if host:
            prof.add_site(bucket, ("domain:" if rtype == "DOMAIN-SUFFIX" else "full:") + host)
        else:
            prof.drop("bad hostname", f"{origin}: {value}")
    elif rtype == "DOMAIN-KEYWORD":
        keyword = value.lower() if value.isascii() else to_ascii_host(value)
        if keyword:
            prof.add_site(bucket, f"keyword:{keyword}")
        else:
            prof.drop("bad keyword", f"{origin}: {value}")
    elif rtype == "DOMAIN-WILDCARD":
        prof.add_site(bucket, "regexp:" + wildcard_to_regexp(to_ascii_host(value) or value))
    elif rtype in ("DOMAIN-REGEX", "DOMAIN-REGEXP"):
        prof.add_site(bucket, "regexp:" + value)
    elif rtype in ("IP-CIDR", "IP-CIDR6", "IP6-CIDR"):
        cidr = as_cidr(value)
        if not cidr:
            prof.drop("bad CIDR", f"{origin}: {value}")
        elif bucket == "proxy" and (ipaddress.ip_network(cidr).is_private or ipaddress.ip_network(cidr).is_loopback):
            prof.drop("private/loopback IP sent to PROXY (kept local instead)", f"{origin}: {cidr}")
        else:
            prof.add_ip(bucket, cidr)
    elif rtype == "GEOIP":
        prof.add_ip(bucket, f"geoip:{value.lower()}")
    elif rtype in UNSUPPORTED:
        prof.drop(f"{rtype} rules (no INCY profile equivalent)", f"{origin}: {rtype},{value}")
    else:
        prof.drop(f"unknown rule type {rtype}", f"{origin}: {rtype},{value}")


def parse_list(prof: Profile, text: str, bucket: str, origin: str, no_resolve: bool = True) -> None:
    """A RULE-SET body: `TYPE,VALUE[,no-resolve]` per line, or bare domains and IPs."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "//")):
            continue
        if "," not in line:
            bare = line.split("#", 1)[0].strip()
            if as_cidr(bare):
                add_rule(prof, "IP-CIDR", bare, bucket, origin, no_resolve)
            elif bare:
                add_rule(prof, "DOMAIN-SUFFIX", bare.lstrip("."), bucket, origin)
            continue
        parts = [p.strip() for p in line.split(",")]
        add_rule(prof, parts[0], parts[1], bucket, origin,
                 no_resolve or any(p.lower() == "no-resolve" for p in parts[2:]))


def _origin(ref: str) -> str:
    return Path(urllib.parse.urlparse(ref).path).name or ref


def parse_conf(prof: Profile, text: str, ref: str, fetch: Fetcher, depth: int = 0) -> None:
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "//")):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            section = m.group(1).strip().lower()
            continue
        if section == "general" and "=" in line:
            key, val = (s.strip() for s in line.split("=", 1))
            key = key.lower()
            if key == "skip-proxy":
                for item in (v.strip() for v in val.split(",")):
                    if not item:
                        continue
                    if as_cidr(item):
                        prof.add_ip("direct", as_cidr(item))  # private ranges; INCY keeps them local anyway
                    elif item != "localhost":
                        host = to_ascii_host(item[2:] if item.startswith("*.") else item)
                        entry = (f"domain:{host}" if item.startswith("*.") else f"full:{host}") if host else None
                        if entry and entry not in prof.skip_proxy:
                            prof.skip_proxy.append(entry)
            elif key == "include" and depth < 3:
                try:
                    sub, sub_ref = fetch.get(val, base=ref)
                    parse_conf(prof, sub, sub_ref, fetch, depth + 1)
                except Exception as exc:
                    prof.failures.append(f"include {val!r} not loaded: {exc}")
            elif key == "dns-server":
                prof.note(f"Shadowrocket dns-server was: {val[:90]}{'…' if len(val) > 90 else ''}")
        elif section == "rule":
            parts = [p.strip() for p in line.split(",")]
            rtype = parts[0].upper()
            if rtype == "FINAL":
                prof.final = policy_bucket(parts[1]) if len(parts) > 1 else "proxy"
            elif rtype == "RULE-SET" and len(parts) >= 3:
                bucket = policy_bucket(parts[2])
                prof.saw_policy(bucket)
                try:
                    body, list_ref = fetch.get(parts[1], base=ref)
                except Exception as exc:
                    prof.failures.append(f"RULE-SET {parts[1]} not loaded: {exc}")
                    continue
                parse_list(prof, body, bucket, _origin(list_ref),
                           no_resolve=any(p.lower() == "no-resolve" for p in parts[3:]))
            elif len(parts) >= 3:
                bucket = policy_bucket(parts[2])
                prof.saw_policy(bucket)
                add_rule(prof, rtype, parts[1], bucket, "inline", any(p.lower() == "no-resolve" for p in parts[3:]))
            else:
                prof.drop("unparsed rule", line)
        elif section == "proxy group":
            prof.drop("proxy groups (an INCY profile has one proxy: the server you pick)", line.split("=", 1)[0].strip())
        elif section in ("url rewrite", "mitm", "script", "host", "header rewrite", "body rewrite", "map local"):
            prof.drop(f"[{section}] section (no INCY equivalent)", line[:80])


# ----------------------------------------------------------------------------------------- checks

def prune_and_check(prof: Profile, geosite: GeoFile | None, geoip: GeoFile | None) -> list[str]:
    report = []
    for bucket in BUCKETS:
        for item in prof.sites[bucket]:
            if item.startswith("geosite:") and geosite is not None and item[8:] not in geosite:
                report.append(f"ERROR: {item} is not in the geosite file; Xray would refuse to start")
        for item in prof.ips[bucket]:
            if item.startswith("geoip:") and geoip is not None and item[6:] not in geoip:
                report.append(f"ERROR: {item} is not in the geoip file; Xray would refuse to start")
    if geosite is None:
        return report
    for bucket in BUCKETS:  # drop entries a geosite tag in the same bucket already covers
        tags = [s for s in prof.sites[bucket] if s.startswith("geosite:")]
        if not tags:
            continue
        matcher = Matcher(tags, geosite)
        keep = []
        for item in prof.sites[bucket]:
            kind, _, value = item.partition(":")
            # TLD-level suffixes (domain:ru) stay: cheap, and they keep working if a tag shrinks
            if kind in ("domain", "full") and "." in value and matcher.covers(kind, value):
                continue
            keep.append(item)
        if len(keep) != len(prof.sites[bucket]):
            report.append(f"pruned {len(prof.sites[bucket]) - len(keep)} {bucket} entries already covered by {', '.join(tags)}")
            prof.sites[bucket] = keep
    direct = Matcher(prof.sites["direct"], geosite)
    overlap = [s for s in prof.sites["proxy"] if s.partition(":")[0] in ("domain", "full")
               and direct.covers(*s.split(":", 1))]
    if overlap:
        report.append(f"{len(overlap)} proxy entries also match a direct entry; RouteOrder decides them: "
                      + ", ".join(overlap[:12]) + (" …" if len(overlap) > 12 else ""))
    return report


# ----------------------------------------------------------------------------------------- output

def build_profile(prof: Profile, name: str, *, final: str | None = None, route_order: str | None = None,
                  domain_strategy: str | None = None, geoip_url: str = f"{LOYALSOLDIER}/geoip.dat",
                  geosite_url: str = f"{LOYALSOLDIER}/geosite.dat") -> dict:
    final = final or prof.final or "proxy"
    first = prof.policy_order[0] if prof.policy_order else "proxy"
    direct_sites = ["geosite:private"] + [s for s in prof.sites["direct"] if s != "geosite:private"]
    return {
        "Name": name,
        "GlobalProxy": "true" if final == "proxy" else "false",
        "RouteOrder": route_order or ("block-proxy-direct" if first == "proxy" else "block-direct-proxy"),
        # IPIfNonMatch is Shadowrocket's default "resolve the name, then test GEOIP/IP-CIDR";
        # AsIs is its no-resolve.
        "DomainStrategy": domain_strategy or ("IPIfNonMatch" if prof.resolves_for_ip_rules else "AsIs"),
        "FakeDNS": "false",
        "RemoteDNSType": "DoH",
        "RemoteDNSDomain": "https://security.cloudflare-dns.com/dns-query",
        "RemoteDNSIP": "1.1.1.2",
        "DomesticDNSType": "DoH",
        "DomesticDNSDomain": "https://77.88.8.8/dns-query",
        "DomesticDNSIP": "77.88.8.8",
        "DnsHosts": {"security.cloudflare-dns.com": "1.1.1.2"},
        "Geoipurl": geoip_url,
        "Geositeurl": geosite_url,
        "useChunkFiles": True,
        "DirectSites": direct_sites,
        "DirectIp": list(dict.fromkeys(PRIVATE_IP + prof.ips["direct"])),
        "ProxySites": prof.sites["proxy"],
        "ProxyIp": prof.ips["proxy"],
        "BlockSites": prof.sites["block"],
        "BlockIp": prof.ips["block"],
    }


def content_hash(data: dict) -> str:
    return hashlib.sha256(json.dumps({k: v for k, v in data.items() if k != "LastUpdated"},
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def deeplink(data: dict) -> str:
    minified = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return "incy://routing/onadd/" + base64.b64encode(minified.encode()).decode()


def main(argv: list[str] | None = None, *, stdout=None) -> int:
    out_stream = stdout or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m incy_routing.convert",
                                 description="Translate a Shadowrocket config into an INCY routing profile.")
    ap.add_argument("conf", help="Shadowrocket .conf URL or path")
    ap.add_argument("--name", required=True, help="profile Name; INCY updates a profile with the same Name in place")
    ap.add_argument("--slug", required=True, help="output file stem")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--final", choices=["proxy", "direct"], help="override FINAL")
    ap.add_argument("--route-order", help="override RouteOrder, e.g. block-proxy-direct")
    ap.add_argument("--domain-strategy", choices=["AsIs", "IPIfNonMatch", "IPOnDemand"])
    for b in ("direct", "proxy", "block"):
        ap.add_argument(f"--add-{b}-site", action="append", default=[], metavar="ITEM")
        ap.add_argument(f"--add-{b}-ip", action="append", default=[], metavar="ITEM")
        ap.add_argument(f"--add-{b}-list", action="append", default=[], metavar="URL",
                        help=f"a Shadowrocket rule list whose entries all go to {b}")
    ap.add_argument("--skip-proxy-direct", action="store_true",
                    help="treat skip-proxy names (*.ru, *.su, *.рф …) as DIRECT")
    ap.add_argument("--geosite", help="local geosite.dat: validate tags and prune covered entries")
    ap.add_argument("--geoip", help="local geoip.dat: validate tags")
    ap.add_argument("--geosite-url", default=f"{LOYALSOLDIER}/geosite.dat", help="Geositeurl written into the profile")
    ap.add_argument("--geoip-url", default=f"{LOYALSOLDIER}/geoip.dat", help="Geoipurl written into the profile")
    ap.add_argument("--publish-base", help="URL prefix where <slug>.json will be served; writes <slug>.autolink")
    ap.add_argument("--cache-dir", help="keep a copy of every download here")
    ap.add_argument("--mirror", help="read every URL from <mirror>/<host>/<path> instead of the network")
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 without writing anything if any list or include fails to load")
    args = ap.parse_args(argv)

    fetch = Fetcher(Path(args.cache_dir) if args.cache_dir else None, Path(args.mirror) if args.mirror else None)
    prof = Profile()
    try:
        text, ref = fetch.get(args.conf)
    except Exception as exc:
        print(f"FAILED: {args.conf} not loaded: {exc}", file=sys.stderr)
        return 2
    parse_conf(prof, text, ref, fetch)
    if prof.skip_proxy:
        if args.skip_proxy_direct:
            for item in prof.skip_proxy:
                prof.add_site("direct", item)
        else:
            prof.note("skip-proxy names not routed (in Shadowrocket they still hit the rules): "
                      + ", ".join(prof.skip_proxy) + "; --skip-proxy-direct sends them DIRECT")
    for b in ("direct", "proxy", "block"):
        for item in getattr(args, f"add_{b}_site"):
            prof.add_site(b, item)
        for item in getattr(args, f"add_{b}_ip"):
            prof.add_ip(b, item)
        for url in getattr(args, f"add_{b}_list"):
            try:
                body, list_ref = fetch.get(url)
            except Exception as exc:
                prof.failures.append(f"list {url} not loaded: {exc}")
                continue
            parse_list(prof, body, b, _origin(list_ref))
    if prof.failures and args.strict:
        for failure in prof.failures:
            print(f"FAILED: {failure}", file=sys.stderr)
        print(f"{args.name}: nothing written (--strict)", file=sys.stderr)
        return 2

    geosite = GeoFile(args.geosite) if args.geosite else None
    geoip = GeoFile(args.geoip) if args.geoip else None
    checks = prune_and_check(prof, geosite, geoip)
    data = build_profile(prof, args.name, final=args.final, route_order=args.route_order,
                         domain_strategy=args.domain_strategy, geoip_url=args.geoip_url, geosite_url=args.geosite_url)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{args.slug}.json"
    # LastUpdated moves only when the rules change: a newer value makes INCY re-download its geo files
    # (~27 MB from Loyalsoldier, which publishes no .sha256 for INCY's skip check).
    stamp = str(int(time.time()))
    if json_path.exists():
        try:
            old = json.loads(json_path.read_text(encoding="utf-8"))
            if content_hash(old) == content_hash(data) and old.get("LastUpdated"):
                stamp = old["LastUpdated"]
        except ValueError:
            pass
    data["LastUpdated"] = stamp
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    link = deeplink(data)
    (out / f"{args.slug}.deeplink").write_text(link + "\n", encoding="utf-8")
    if args.publish_base:
        (out / f"{args.slug}.autolink").write_text(
            f"incy://autorouting/onadd/{args.publish_base.rstrip('/')}/{json_path.name}\n", encoding="utf-8")

    lines = [f"{args.name}: {json_path} ({json_path.stat().st_size} bytes, deep link {len(link)} chars)",
             f"  GlobalProxy={data['GlobalProxy']}  RouteOrder={data['RouteOrder']}  DomainStrategy={data['DomainStrategy']}",
             f"  sites: direct {len(data['DirectSites'])}, proxy {len(data['ProxySites'])}, block {len(data['BlockSites'])};"
             f" ips: direct {len(data['DirectIp'])}, proxy {len(data['ProxyIp'])}, block {len(data['BlockIp'])}"]
    lines += [f"  check: {c}" for c in checks]
    lines += [f"  FAILED: {f}" for f in prof.failures]
    lines += [f"  note: {n}" for n in prof.notes]
    for reason, items in prof.dropped.items():
        lines.append(f"  dropped {len(items)} × {reason}: " + "; ".join(items[:6]) + (" …" if len(items) > 6 else ""))
    report = "\n".join(lines)
    (out / f"{args.slug}.report.txt").write_text(report + "\n", encoding="utf-8")
    print(report, file=out_stream)
    for geo in (geosite, geoip):
        if geo is not None:
            geo.close()
    if any(c.startswith("ERROR") for c in checks):
        return 1
    return 2 if prof.failures else 0


if __name__ == "__main__":
    sys.exit(main())
