from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import geodata  # noqa: E402

MIRROR = ROOT / "tests" / "fixtures" / "upstream"
SOURCE = "https://raw.githubusercontent.com/misha-tgshv/shadowrocket-configuration-file/main"


@pytest.fixture
def geo(tmp_path: Path) -> tuple[Path, Path]:
    """Synthetic (geosite.dat, geoip.dat)."""
    return geodata.write(tmp_path / "geo")


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    """A private copy of the upstream mirror, safe to break."""
    copy = tmp_path / "upstream"
    shutil.copytree(MIRROR, copy)
    return copy


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A data directory whose geo files are already in place (builds never download them)."""
    data = tmp_path / "data"
    geodata.write(data / "geo")
    return data
