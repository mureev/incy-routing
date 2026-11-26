"""Validate profiles and geodata, then atomically publish a complete generation.

    python -m incy_routing.build --data-dir ./data [--mirror DIR] [--no-geo-refresh]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import redirect_stderr
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path

from . import convert, publication
from .geo import GeoFile
from .profiles import PROFILES, SOURCE
from .xray import validate_profile

GEO_BASE = "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download"
GEO_MAX_AGE = 7 * 86400


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stage_geo(cache: Path, stage: Path, refresh: bool, log) -> list[str]:
    """Prepare geodata without replacing any cached file until the whole build passes."""
    stage.mkdir()
    downloaded = []
    for name, rule in (("geosite.dat", {"DirectSites": ["geosite:private"]}),
                       ("geoip.dat", {"DirectIp": ["geoip:private"]})):
        path, candidate = cache / name, stage / name
        if refresh and (not path.is_file() or not path.stat().st_size or
                        time.time() - path.stat().st_mtime >= GEO_MAX_AGE):
            try:
                req = urllib.request.Request(f"{GEO_BASE}/{name}", headers={"User-Agent": "incy-routing"})
                with urllib.request.urlopen(req, timeout=300) as response, candidate.open("wb") as file:
                    shutil.copyfileobj(response, file)
                data = GeoFile(candidate)
                try:
                    if "private" not in data:
                        raise ValueError("missing private tag")
                finally:
                    data.close()
                validate_profile(rule, stage)
                downloaded.append(name)
                log(f"geo: validated {name} ({candidate.stat().st_size} bytes)")
                continue
            except (OSError, HTTPException, ValueError, subprocess.TimeoutExpired) as exc:
                candidate.unlink(missing_ok=True)
                if not path.is_file():
                    raise RuntimeError(f"cannot validate downloaded {name}: {exc}") from exc
                log(f"geo: kept the old {name} ({exc})")
        candidate.symlink_to(path.resolve())
    return downloaded


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
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    with publication.locked(data_dir, "build"):
        return _run(data_dir, source, mirror, geo_refresh, PROFILES if profiles is None else profiles, log)


def _run(data_dir: Path, source: str, mirror: Path | None, geo_refresh: bool, profiles: list[dict], log) -> dict:
    public = publication.published_dir(data_dir)
    cache = data_dir / "geo"
    generations = data_dir / "generations"
    for directory in (cache, generations):
        directory.mkdir(exist_ok=True)
    status_path = data_dir / "status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    status["last_attempt"] = _now()
    stage = Path(tempfile.mkdtemp(prefix="build-", dir=generations))
    switched = False
    try:
        geo = stage / "geo"
        downloaded = stage_geo(cache, geo, geo_refresh, log)
        for live in public.glob("*.json"):
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
            issues = check(p["slug"], built, p["expect"], p["name"])
            if not issues:
                try:
                    validate_profile(built, geo)
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    issues.append(f"{p['slug']}: Xray validation failed: {exc}")
            problems += issues
        if problems:
            raise RuntimeError("; ".join(problems))

        results = {}
        for p in profiles:
            name = f"{p['slug']}.json"
            src, dst = stage / name, public / name
            if dst.exists() and dst.read_bytes() == src.read_bytes():
                shutil.copystat(dst, src)
                results[p["slug"]] = "unchanged"
            else:
                results[p["slug"]] = "updated"
        for name in downloaded:
            os.replace(geo / name, cache / name)
        shutil.rmtree(geo)
        for path in stage.iterdir():
            if path.suffix != ".json":
                path.unlink()

        with publication.locked(data_dir):
            if not (data_dir / "current").is_symlink() or "updated" in results.values():
                publication.publish(data_dir, stage)
                switched = True
            status.update(last_success=status["last_attempt"], results=results, error=None)
            _write_status(status_path, status)
        log("published: " + " ".join(f"{k}={v}" for k, v in results.items()))
    except Exception as exc:
        status["error"] = str(exc)
        log(f"{'profiles published, status update failed' if switched else 'FAILED, nothing published'}: {exc}")
        with publication.locked(data_dir):
            _write_status(status_path, status)
    finally:
        if not switched:
            shutil.rmtree(stage, ignore_errors=True)
    return status


def _write_status(path: Path, status: dict) -> None:
    tmp = path.with_name(".status.json.tmp")
    tmp.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
