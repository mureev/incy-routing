"""Load generated rules with the same Xray compiler used by the routing checks."""

from __future__ import annotations

import json
import os
import subprocess
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


def validate_profile(profile: dict, assets: Path) -> None:
    """Check rules and referenced geodata without starting a listener."""
    result = subprocess.run(
        [os.environ.get("INCY_XRAY", "xray"), "run", "-test", "-c", "stdin:"],
        input=json.dumps(compile_profile(profile, {})), capture_output=True, text=True,
        env={**os.environ, "XRAY_LOCATION_ASSET": str(assets.resolve())}, timeout=45, check=False,
    )
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        raise ValueError(detail[-1] if detail else f"Xray exited {result.returncode}")
