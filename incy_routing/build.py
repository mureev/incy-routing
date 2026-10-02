"""Build every profile in profiles.py and publish the ones that changed, all or nothing.

    <data>/geo/      geosite.dat, geoip.dat: Loyalsoldier's, the files INCY bundles; used only to validate
                     tags and prune. Refreshed weekly; a failed download keeps the old copy.
    <data>/stage/    this run's output, seeded with what is live so unchanged rules keep their LastUpdated
    <data>/public/   what is served; files are swapped in one atomic rename each, and only when they changed
    <data>/status.json   the last attempt and the last success, for /status

A run publishes nothing unless every profile built with --strict (every list and include loaded) and passed
its `expect` gate. Phones then simply keep the last good profile.

    python -m incy_routing.build --data-dir ./data [--mirror DIR] [--no-geo-refresh]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import time
import urllib.request
from contextlib import redirect_stderr
from datetime import UTC, datetime
from pathlib import Path

from . import convert
from .profiles import PROFILES, SOURCE

GEO_BASE = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download"
GEO_MAX_AGE = 7 * 86400


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh_geo(geo_dir: Path, log) -> None:
    geo_dir.mkdir(parents=True, exist_ok=True)
    for name in ("geosite.dat", "geoip.dat"):
        path = geo_dir / name
        if path.exists() and path.stat().st_size and time.time() - path.stat().st_mtime < GEO_MAX_AGE:
            continue
        tmp = path.with_suffix(".new")
        try:
            req = urllib.request.Request(f"{GEO_BASE}/{name}", headers={"User-Agent": "incy-routing"})
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as fh:
                shutil.copyfileobj(r, fh)
            os.replace(tmp, path)
            log(f"geo: downloaded {name} ({path.stat().st_size} bytes)")
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            if not path.exists():
                raise RuntimeError(f"cannot download {name}: {exc}") from exc
            log(f"geo: kept the old {name} ({exc})")


def check(slug: str, profile: dict, expect: dict, name: str) -> list[str]:
    problems = []
    if profile.get("Name") != name:
        problems.append(f"{slug}: Name is {profile.get('Name')!r}, expected {name!r}")
    if profile.get("GlobalProxy") != expect["GlobalProxy"]:
        problems.append(f"{slug}: GlobalProxy is {profile.get('GlobalProxy')!r}")
    for key, minimum in expect.get("min", {}).items():
        if len(profile.get(key, [])) < minimum:
            problems.append(f"{slug}: {key} has {len(profile.get(key, []))} entries, expected >= {minimum}")
    entries = {e for v in profile.values() if isinstance(v, list) for e in v}
    problems += [f"{slug}: {m} missing" for m in expect.get("must", []) if m not in entries]
    return problems


def run(data_dir: Path, *, source: str = SOURCE, mirror: Path | None = None, geo_refresh: bool = True,
        profiles: list[dict] | None = None, log=print) -> dict:
    """One build. Returns the status written to <data>/status.json."""
    profiles = PROFILES if profiles is None else profiles
    public, stage, geo = data_dir / "public", data_dir / "stage", data_dir / "geo"
    for d in (public, stage, geo):
        d.mkdir(parents=True, exist_ok=True)
    status_path = data_dir / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    status["last_attempt"] = _now()

    try:
        if geo_refresh:
            refresh_geo(geo, log)
        for p in profiles:
            for f in stage.glob(f"{p['slug']}.*"):
                f.unlink()
            live = public / f"{p['slug']}.json"
            if live.exists():
                shutil.copy2(live, stage / live.name)
        problems: list[str] = []
        for p in profiles:
            argv = [p["conf"].format(source=source), "--name", p["name"], "--slug", p["slug"],
                    "--out-dir", str(stage), "--strict", "--cache-dir", str(data_dir / "cache"),
                    "--geosite", str(geo / "geosite.dat"), "--geoip", str(geo / "geoip.dat")]
            argv += [a.format(source=source) for a in p["args"]]
            if mirror is not None:
                argv += ["--mirror", str(mirror)]
            out, err = io.StringIO(), io.StringIO()
            with redirect_stderr(err):
                code = convert.main(argv, stdout=out)
            for line in (out.getvalue() + err.getvalue()).splitlines():
                log(f"  {line}")
            if code != 0:
                problems.append(f"{p['slug']}: the converter exited {code}")
                continue
            built = json.loads((stage / f"{p['slug']}.json").read_text(encoding="utf-8"))
            problems += check(p["slug"], built, p["expect"], p["name"])
        if problems:
            raise RuntimeError("; ".join(problems))

        results = {}
        for p in profiles:
            src, dst = stage / f"{p['slug']}.json", public / f"{p['slug']}.json"
            if dst.exists() and dst.read_bytes() == src.read_bytes():
                results[p["slug"]] = "unchanged"
                continue
            tmp = public / f".{p['slug']}.json.tmp"
            shutil.copyfile(src, tmp)
            os.chmod(tmp, 0o644)
            os.replace(tmp, dst)
            results[p["slug"]] = "updated"
        status.update(last_success=status["last_attempt"], results=results, error=None)
        log("published: " + " ".join(f"{k}={v}" for k, v in results.items()))
    except Exception as exc:
        status["error"] = str(exc)
        log(f"FAILED, nothing published: {exc}")
    tmp = data_dir / ".status.json.tmp"
    tmp.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, status_path)
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m incy_routing.build", description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", default=os.environ.get("INCY_DATA_DIR", "./data"))
    ap.add_argument("--source", default=os.environ.get("INCY_SOURCE", SOURCE))
    ap.add_argument("--mirror", default=os.environ.get("INCY_MIRROR") or None,
                    help="read every URL from <mirror>/<host>/<path> instead of the network")
    ap.add_argument("--no-geo-refresh", action="store_true",
                    help="use the geo files already in <data>/geo and never download them")
    args = ap.parse_args(argv)
    status = run(Path(args.data_dir), source=args.source, mirror=Path(args.mirror) if args.mirror else None,
                 geo_refresh=not args.no_geo_refresh, log=lambda m: print(m, flush=True))
    return 0 if status.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
