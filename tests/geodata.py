"""Small, synthetic geosite.dat / geoip.dat files, so the suite never downloads 27 MB of the real ones.

The tags are the ones the profiles use, with enough entries to exercise matching and pruning. The real files
are checked by the `routing` CI job, against real Xray.

    python tests/geodata.py DIR    writes DIR/geosite.dat and DIR/geoip.dat
"""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

KIND = {"keyword": 0, "regexp": 1, "domain": 2, "full": 3}

GEOSITE = {
    "PRIVATE": [("domain", "local"), ("domain", "localhost"), ("domain", "lan"), ("domain", "example"),
                ("regexp", "^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")],
    "CATEGORY-RU": [("domain", "ru"), ("domain", "su"), ("domain", "xn--p1ai"), ("domain", "vk.com"),
                    ("domain", "sberbank.com"), ("domain", "eu.2gis.com"), ("domain", "x5static.net"),
                    ("domain", "ping.ozon.kz"), ("full", "mos.transport.vtb.ru")],
    "TELEGRAM": [("domain", "t.me"), ("domain", "telegram.org")],
}
GEOIP = {
    "PRIVATE": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7"],
    "RU": ["77.88.0.0/18", "87.240.128.0/18", "194.54.14.0/24", "217.12.96.0/21"],
    "TELEGRAM": ["149.154.160.0/20", "91.108.4.0/22", "2001:67c:4e8::/48"],
}


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _bytes(field: int, data: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def _int(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value) if value else b""  # proto3 leaves zero out


def geosite(tags: dict[str, list[tuple[str, str]]] = GEOSITE) -> bytes:
    out = b""
    for tag, entries in tags.items():
        site = _bytes(1, tag.encode())
        for kind, value in entries:
            site += _bytes(2, _int(1, KIND[kind]) + _bytes(2, value.encode()))
        out += _bytes(1, site)
    return out


def geoip(tags: dict[str, list[str]] = GEOIP) -> bytes:
    out = b""
    for tag, cidrs in tags.items():
        entry = _bytes(1, tag.encode())
        for cidr in cidrs:
            net = ipaddress.ip_network(cidr)
            entry += _bytes(2, _bytes(1, net.network_address.packed) + _int(2, net.prefixlen))
        out += _bytes(1, entry)
    return out


def write(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "geosite.dat").write_bytes(geosite())
    (directory / "geoip.dat").write_bytes(geoip())
    return directory / "geosite.dat", directory / "geoip.dat"


if __name__ == "__main__":
    for path in write(Path(sys.argv[1])):
        print(path)
