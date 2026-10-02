"""Read Xray/V2Ray geo files (geosite.dat, geoip.dat) and match names the way Xray does.

Used to check that every `geosite:`/`geoip:` tag a profile names exists (Xray refuses to start
otherwise), and to drop entries a tag in the same bucket already covers. Standard library only.

    GeoSiteList { repeated GeoSite entry = 1; }   GeoSite { string country_code = 1; repeated Domain domain = 2; }
    Domain      { Type type = 1; string value = 2; ... }   Type: 0 keyword, 1 regexp, 2 domain (suffix), 3 full
    GeoIPList   { repeated GeoIP entry = 1; }     GeoIP { string country_code = 1; repeated CIDR cidr = 2; ... }
"""

from __future__ import annotations

import contextlib
import mmap
import re
from collections.abc import Iterator
from pathlib import Path

KINDS = {0: "keyword", 1: "regexp", 2: "domain", 3: "full"}


def _varint(buf, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _fields(buf, start: int = 0, end: int | None = None) -> Iterator[tuple[int, int, object]]:
    """Yield (field number, wire type, value); length-delimited values come back as (start, end)."""
    i = start
    end = len(buf) if end is None else end
    while i < end:
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
            yield number, wire, value
        elif wire == 2:
            length, i = _varint(buf, i)
            yield number, wire, (i, i + length)
            i += length
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire} at byte {i}")


class GeoFile:
    """A geosite.dat or geoip.dat, memory-mapped; tags are indexed lower-case."""

    def __init__(self, path: str | Path):
        with open(path, "rb") as fh:
            self._map = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        self.buf = memoryview(self._map)
        self.index: dict[str, tuple[int, int]] = {}
        for number, wire, span in _fields(self.buf):
            if number != 1 or wire != 2:
                continue
            for n2, w2, v2 in _fields(self.buf, *span):
                if n2 == 1 and w2 == 2:
                    self.index[bytes(self.buf[v2[0]:v2[1]]).decode().lower()] = span
                    break

    def __contains__(self, tag: str) -> bool:
        return tag.lower() in self.index

    def domains(self, tag: str) -> list[tuple[str, str]]:
        """[(kind, value)] for a geosite tag; kind is keyword, regexp, domain or full."""
        out = []
        for number, wire, span in _fields(self.buf, *self.index[tag.lower()]):
            if number == 2 and wire == 2:
                kind, value = 0, ""
                for n2, w2, v2 in _fields(self.buf, *span):
                    if n2 == 1 and w2 == 0:
                        kind = v2
                    elif n2 == 2 and w2 == 2:
                        value = bytes(self.buf[v2[0]:v2[1]]).decode("utf-8", "replace")
                out.append((KINDS.get(kind, "keyword"), value))
        return out

    def close(self) -> None:
        try:
            self.buf.release()
            self._map.close()
        except BufferError:  # a slice is still referenced somewhere; the GC will unmap it
            pass


class Matcher:
    """Xray's domain semantics for `domain:`, `full:`, `keyword:`, `regexp:` and `geosite:TAG` entries."""

    def __init__(self, items: list[str], geosite: GeoFile | None = None):
        self.full: set[str] = set()
        self.suffix: set[str] = set()
        self.keywords: list[str] = []
        self.regexps: list[re.Pattern] = []
        for item in items:
            kind, _, value = item.partition(":")
            if kind == "geosite":
                if geosite is not None and value in geosite:
                    for k, v in geosite.domains(value):
                        self._add(k, v)
            elif kind in ("domain", "full", "keyword", "regexp"):
                self._add(kind, value)

    def _add(self, kind: str, value: str) -> None:
        value = value.lower()
        if kind == "full":
            self.full.add(value)
        elif kind == "domain":
            self.suffix.add(value)
        elif kind == "keyword":
            self.keywords.append(value)
        else:
            with contextlib.suppress(re.error):  # Xray's RE2 accepts a few things Python's re does not
                self.regexps.append(re.compile(value))

    def match(self, host: str) -> bool:
        """Does a connection to `host` match? Suffixes match on a label boundary, like Xray."""
        host = host.lower().rstrip(".")
        if host in self.full:
            return True
        labels = host.split(".")
        if any(".".join(labels[i:]) in self.suffix for i in range(len(labels))):
            return True
        return any(k in host for k in self.keywords) or any(r.search(host) for r in self.regexps)

    def covers(self, kind: str, value: str) -> bool:
        """Is every name the entry `kind:value` can match also matched here?

        `full:x` is covered when x itself matches. `domain:x` also matches every *.x, so a regexp that
        happens to accept "x" proves nothing (geosite:private's single-label regexp accepts "ru"); only a
        suffix rule at or above x, or a keyword inside x, covers it.
        """
        value = value.lower()
        if kind == "full":
            return self.match(value)
        if kind != "domain":
            return False
        labels = value.split(".")
        if any(".".join(labels[i:]) in self.suffix for i in range(len(labels))):
            return True
        return any(k in value for k in self.keywords)
