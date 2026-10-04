"""Walk an iCloud Drive tree and describe every file that needs migrating."""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator, List, Sequence

from . import icloud

DEFAULT_EXCLUDES = (".DS_Store", "Icon\r", ".localized", ".iground", ".Trash")


class State(str, Enum):
    LOCAL = "local"  # contents already on this Mac
    CLOUD = "cloud"  # dataless stub, contents only in iCloud
    PLACEHOLDER = "placeholder"  # legacy `.name.icloud` stub


class Kind(str, Enum):
    FILE = "file"
    SYMLINK = "symlink"


@dataclass
class Entry:
    rel_path: str  # POSIX-style path relative to the source root
    path: Path  # where the real file lives (or will live once downloaded)
    kind: Kind
    state: State
    size: int
    mtime_ns: int

    @property
    def needs_download(self) -> bool:
        return self.state is not State.LOCAL


def is_excluded(rel_path: str, patterns: Sequence[str]) -> bool:
    name = rel_path.rsplit("/", 1)[-1]
    parts = rel_path.split("/")
    for pat in patterns:
        if fnmatch.fnmatchcase(rel_path, pat) or fnmatch.fnmatchcase(name, pat):
            return True
        if any(fnmatch.fnmatchcase(p, pat) for p in parts[:-1]):
            return True
    return False


def scan(root: Path, excludes: Sequence[str] = DEFAULT_EXCLUDES) -> Iterator[Entry]:
    """Yield an Entry for every file and symlink under `root`, depth first, sorted."""
    root = Path(root)
    stack: List[Path] = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as it:
                items = sorted(it, key=lambda e: e.name)
        except (PermissionError, FileNotFoundError):
            continue
        names = {e.name for e in items}
        subdirs: List[Path] = []
        for item in items:
            rel = Path(item.path).relative_to(root).as_posix()
            if is_excluded(rel, excludes):
                continue
            st = item.stat(follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                subdirs.append(Path(item.path))
            elif stat.S_ISLNK(st.st_mode):
                yield Entry(rel, Path(item.path), Kind.SYMLINK, State.LOCAL, 0, st.st_mtime_ns)
            elif stat.S_ISREG(st.st_mode):
                if icloud.is_placeholder_name(item.name):
                    real = icloud.placeholder_real_name(item.name)
                    if real in names:
                        continue  # real file already present; placeholder is stale
                    real_path = Path(directory) / real
                    real_rel = real_path.relative_to(root).as_posix()
                    if is_excluded(real_rel, excludes):
                        continue
                    yield Entry(
                        real_rel,
                        real_path,
                        Kind.FILE,
                        State.PLACEHOLDER,
                        icloud.placeholder_size(Path(item.path)),
                        st.st_mtime_ns,
                    )
                else:
                    state = State.CLOUD if icloud.is_dataless(st) else State.LOCAL
                    yield Entry(rel, Path(item.path), Kind.FILE, state, st.st_size, st.st_mtime_ns)
        stack.extend(reversed(subdirs))


@dataclass
class Summary:
    files: int = 0
    symlinks: int = 0
    total_bytes: int = 0
    local_files: int = 0
    local_bytes: int = 0
    cloud_files: int = 0
    cloud_bytes: int = 0

    def add(self, e: Entry) -> None:
        if e.kind is Kind.SYMLINK:
            self.symlinks += 1
            return
        self.files += 1
        self.total_bytes += e.size
        if e.needs_download:
            self.cloud_files += 1
            self.cloud_bytes += e.size
        else:
            self.local_files += 1
            self.local_bytes += e.size
