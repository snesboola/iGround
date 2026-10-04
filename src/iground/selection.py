"""What the user chose to copy.

Choices are stored as things to *skip*, so anything new that appears in iCloud
(a new folder, a new year of photos, a new app) is copied by default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import icloud
from .scanner import DEFAULT_EXCLUDES, Summary, scan
from .sources import ALL_KINDS, APPS, FolderSource

LOOSE_FILES = "__files__"  # files sitting directly in iCloud Drive, not in a folder
UNKNOWN_YEAR = "unknown"


def photo_year(taken_at: Optional[float]) -> str:
    return str(datetime.fromtimestamp(taken_at).year) if taken_at is not None else UNKNOWN_YEAR


def app_name(src: FolderSource) -> str:
    return src.dest_rel.split("/", 1)[-1]


@dataclass
class Selection:
    kinds: List[str] = field(default_factory=lambda: list(ALL_KINDS))
    skip_drive: List[str] = field(default_factory=list)
    skip_apps: List[str] = field(default_factory=list)
    skip_years: List[str] = field(default_factory=list)

    # -- persistence ---------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "Selection":
        kinds = [k for k in data.get("kinds", ALL_KINDS) if k in ALL_KINDS] or list(ALL_KINDS)
        skip = data.get("skip", {})
        return cls(
            kinds=[k for k in ALL_KINDS if k in kinds],
            skip_drive=[str(x) for x in skip.get("drive", [])],
            skip_apps=[str(x) for x in skip.get("apps", [])],
            skip_years=[str(x) for x in skip.get("years", [])],
        )

    def to_dict(self) -> dict:
        return {"kinds": list(self.kinds),
                "skip": {"drive": list(self.skip_drive), "apps": list(self.skip_apps), "years": list(self.skip_years)}}

    # -- questions the engine asks -------------------------------------------------

    def wants_app(self, src: FolderSource) -> bool:
        return src.kind != APPS or app_name(src) not in self.skip_apps

    def wants_photo(self, taken_at: Optional[float]) -> bool:
        return photo_year(taken_at) not in self.skip_years

    def drive_excludes(self, drive_root: Path) -> List[str]:
        """Anchored exclude patterns ('/Name') for the iCloud Drive items the user skipped."""
        out = [f"/{name}" for name in self.skip_drive if name != LOOSE_FILES]
        if LOOSE_FILES in self.skip_drive:
            out += [f"/{name}" for name in loose_file_names(drive_root)]
        return out


def loose_file_names(root: Path) -> List[str]:
    names = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return names
    for e in entries:
        if e.is_dir(follow_symlinks=False):
            continue
        names.append(icloud.placeholder_real_name(e.name) if icloud.is_placeholder_name(e.name) else e.name)
    return names


def top_level_groups(root: Path, excludes: Sequence[str] = DEFAULT_EXCLUDES) -> Dict[str, Summary]:
    """Size of each top-level folder in `root`; loose files are grouped under LOOSE_FILES."""
    groups: Dict[str, Summary] = {}
    for e in scan(root, excludes):
        key = e.rel_path.split("/", 1)[0] if "/" in e.rel_path else LOOSE_FILES
        groups.setdefault(key, Summary()).add(e)
    return groups


def load(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def save(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
