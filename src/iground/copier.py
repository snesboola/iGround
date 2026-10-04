"""Crash-safe file copying with integrity hashing."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

CHUNK = 8 * 1024 * 1024
PARTIAL_SUFFIX = ".iground-partial"


def partial_path(dst: Path) -> Path:
    return dst.with_name(f".{dst.name}{PARTIAL_SUFFIX}")


def copy_file(src: Path, dst: Path) -> str:
    """Copy `src` to `dst` atomically, returning the SHA-256 of the bytes read.

    Data is written to a hidden partial file, fsynced, then renamed into place, so
    an interrupted run never leaves a truncated file under the final name.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = partial_path(dst)
    digest = hashlib.sha256()
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                chunk = fin.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                fout.write(chunk)
            fout.flush()
            os.fsync(fout.fileno())
        shutil.copystat(src, tmp, follow_symlinks=False)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return digest.hexdigest()


def copy_symlink(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    target = os.readlink(src)
    if dst.is_symlink() or dst.exists():
        if dst.is_symlink() and os.readlink(dst) == target:
            return
        dst.unlink()
    os.symlink(target, dst)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
