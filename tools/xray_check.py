#!/usr/bin/env python3
"""Load an INCY routing profile into real Xray-core and ask it where traffic goes.

The profile is compiled to Xray routing the way Happ/INCY-style profiles are compiled by 3x-ui's
json_routing.go (block / direct / proxy groups in RouteOrder, domain rule then IP rule per group), except
that GlobalProxy picks Xray's default outbound instead of appending a catch-all rule (see compile_profile).
INCY itself is closed source; this is the community convention, not INCY's code.

For each test destination we open a SOCKS5 CONNECT through Xray and read the access log to see which
outbound Xray picked. All three outbounds are blackholes, so nothing leaves the machine.

Usage: tools/xray_check.py PROFILE.json CASES.json --xray DIR    (DIR holds xray, geosite.dat, geoip.dat;
tools/routing_check.sh sets all of this up from pinned downloads)
CASES.json: {"hosts": {name: ip, ...}, "cases": [{"dest": "gosuslugi.ru", "expect": "direct", "why": "..."}, ...]}
            "dest" may be a hostname or an IP; "hosts" pins test names to IPs for the geoip cases.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def compile_profile(p: dict, hosts: dict[str, str]) -> dict:
    order = [g for g in (p.get("RouteOrder") or "block-direct-proxy").split("-") if g in ("block", "direct", "proxy")]
    groups = {
        "block": (p.get("BlockSites") or [], p.get("BlockIp") or []),
        "direct": (p.get("DirectSites") or [], p.get("DirectIp") or []),
        "proxy": (p.get("ProxySites") or [], p.get("ProxyIp") or []),
    }
    rules = []
    for g in order:
        sites, ips = groups[g]
        if sites:
            rules.append({"type": "field", "domain": sites, "outboundTag": g})
        if ips:
            rules.append({"type": "field", "ip": ips, "outboundTag": g})
    final = "proxy" if str(p.get("GlobalProxy", "true")).lower() == "true" else "direct"
    # "Everything else" = Xray's default outbound (the first one), NOT a catch-all rule: a
    # `network: tcp,udp` rule matches in the first, domain-only pass, so IPIfNonMatch would never get
    # to resolve the name and try geoip:ru. (3x-ui's json_routing.go appends exactly such a rule.)
    outbound_order = [final] + [t for t in ("proxy", "direct", "block") if t != final]
    return {
        "log": {"loglevel": "warning"},
        # static answers only: the sandbox has no resolver, and we want deterministic geoip tests
        "dns": {"hosts": hosts, "servers": ["localhost"]},
        "inbounds": [{"tag": "socks-in", "listen": "127.0.0.1", "port": 0, "protocol": "socks",
                      "settings": {"auth": "noauth", "udp": False}}],
        "outbounds": [{"tag": t, "protocol": "blackhole"} for t in outbound_order],
        "routing": {"domainStrategy": p.get("DomainStrategy", "AsIs"), "rules": rules},
    }


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def socks_connect(port: int, dest: str, dport: int = 443):
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    try:
        s.sendall(b"\x05\x01\x00")
        s.recv(2)
        try:
            packed = socket.inet_pton(socket.AF_INET, dest)
            addr = b"\x01" + packed
        except OSError:
            try:
                packed = socket.inet_pton(socket.AF_INET6, dest)
                addr = b"\x04" + packed
            except OSError:
                raw = dest.encode("idna")
                addr = b"\x03" + bytes([len(raw)]) + raw
        s.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", dport))
        with contextlib.suppress(OSError):
            s.recv(64)
    finally:
        s.close()


def run(profile_path: str, cases_path: str, xray_dir: str) -> int:
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    spec = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    cases, hosts = spec["cases"], spec.get("hosts", {})
    cfg = compile_profile(profile, hosts)
    port = free_port()
    cfg["inbounds"][0]["port"] = port
    work = Path(tempfile.mkdtemp(prefix="xraycheck-"))
    access = work / "access.log"
    cfg["log"]["access"] = str(access)
    (work / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    env = {"XRAY_LOCATION_ASSET": xray_dir, "PATH": "/usr/bin:/bin"}
    test = subprocess.run([f"{xray_dir}/xray", "run", "-test", "-c", str(work / "config.json")],
                          capture_output=True, text=True, env=env)
    ok_line = (test.stdout + test.stderr).strip().splitlines()[-1] if (test.stdout + test.stderr).strip() else ""
    print(f"xray -test: {'OK' if test.returncode == 0 else 'FAILED'} — {ok_line}")
    if test.returncode != 0:
        return 2

    proc = subprocess.Popen([f"{xray_dir}/xray", "run", "-c", str(work / "config.json")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        for c in cases:
            socks_connect(port, c["dest"])
            time.sleep(0.05)
        time.sleep(0.5)
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    seen: dict[str, str] = {}
    for line in access.read_text(encoding="utf-8", errors="replace").splitlines():
        if " accepted " not in line:
            continue
        target = line.split(" accepted ", 1)[1].split()[0]           # tcp:host:443
        host = target.split(":", 1)[1].rsplit(":", 1)[0].strip("[]")
        outbound = line.rsplit("]", 1)[0].rsplit(" ", 1)[-1]       # [socks-in -> proxy] / [socks-in >> proxy]
        seen[host] = outbound
    fails = 0
    print(f"{'destination':34} {'xray':7} {'expect':7}  why")
    for c in cases:
        got = seen.get(c["dest"].encode("idna").decode() if not c["dest"].replace('.', '').replace(':', '').isdigit() else c["dest"], seen.get(c["dest"], "?"))
        mark = "ok" if got == c["expect"] else "MISMATCH"
        fails += mark != "ok"
        print(f"{c['dest']:34} {got:7} {c['expect']:7}  {mark:8} {c.get('why', '')}")
    print(f"{len(cases) - fails}/{len(cases)} as expected")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("cases")
    ap.add_argument("--xray", default=str(Path(__file__).resolve().parent.parent / "xray"))
    a = ap.parse_args()
    sys.exit(run(a.profile, a.cases, a.xray))
