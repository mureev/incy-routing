"""Published generations and their short read/write lock."""

from __future__ import annotations

import fcntl
import os
import shutil
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(data_dir: Path, name: str = "publication", *, shared: bool = False):
    with (data_dir / f".{name}.lock").open("a") as file:
        fcntl.flock(file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def published_dir(data_dir: Path) -> Path:
    current = data_dir / "current"
    # Old images still serve this directory if a deployment is rolled back.
    return current.resolve() if current.is_symlink() else data_dir / "public"


def publish(data_dir: Path, generation: Path) -> None:
    """Switch to the fully staged generation with one atomic rename."""
    current = data_dir / "current"
    previous = published_dir(data_dir)
    pointer = generation.with_name(f".{generation.name}.link")
    pointer.symlink_to(generation.relative_to(data_dir), target_is_directory=True)
    try:
        os.replace(pointer, current)
    finally:
        pointer.unlink(missing_ok=True)
    for old in generation.parent.glob("build-*"):
        if old not in {generation, previous}:
            shutil.rmtree(old, ignore_errors=True)
