"""Where each kind of iCloud data lives on the Mac, and where it goes on the SSD."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

DRIVE = "drive"
APPS = "apps"
PHOTOS = "photos"
MESSAGES = "messages"
ALL_KINDS = (DRIVE, APPS, PHOTOS, MESSAGES)

# Folder names on the SSD.
DRIVE_DIR = "iCloud Drive"
APPS_DIR = "iCloud App Folders"
PHOTOS_DIR = "Photos"
MESSAGES_DIR = "Messages"
BACKUPS_DIR = "iPhone Backups"


@dataclass
class Locations:
    home: Path

    @classmethod
    def default(cls) -> "Locations":
        # IGROUND_HOME lets tests (or an admin migrating another account) point elsewhere.
        return cls(Path(os.environ.get("IGROUND_HOME") or Path.home()))

    @property
    def mobile_documents(self) -> Path:
        return self.home / "Library" / "Mobile Documents"

    @property
    def drive(self) -> Path:
        return self.mobile_documents / "com~apple~CloudDocs"

    @property
    def messages(self) -> Path:
        return self.home / "Library" / "Messages"

    @property
    def mobilesync_backup(self) -> Path:
        return self.home / "Library" / "Application Support" / "MobileSync" / "Backup"


@dataclass
class FolderSource:
    """A plain folder of files migrated with the file engine."""

    kind: str
    label: str
    path: Path
    dest_rel: str
    icloud_managed: bool  # files may be cloud-only and can be evicted with brctl

    def dest(self, root: Path) -> Path:
        return Path(root) / self.dest_rel


def friendly_container_name(name: str) -> str:
    """'iCloud~com~example~App' -> 'com.example.App'; 'com~apple~Pages' -> 'com.apple.Pages'."""
    if name.startswith("iCloud~"):
        name = name[len("iCloud~"):]
    return name.replace("~", ".")


def app_containers(loc: Locations) -> List[Path]:
    root = loc.mobile_documents
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != loc.drive.name
    )


def parse_kinds(only: Optional[str]) -> Sequence[str]:
    if not only:
        return ALL_KINDS
    kinds = [k.strip() for k in only.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in ALL_KINDS]
    if unknown:
        raise ValueError(f"unknown kind(s): {', '.join(unknown)} (choose from {', '.join(ALL_KINDS)})")
    return kinds


def folder_sources(loc: Locations, kinds: Sequence[str] = ALL_KINDS) -> List[FolderSource]:
    out: List[FolderSource] = []
    if DRIVE in kinds and loc.drive.is_dir():
        out.append(FolderSource(DRIVE, "iCloud Drive", loc.drive, DRIVE_DIR, True))
    if APPS in kinds:
        for c in app_containers(loc):
            name = friendly_container_name(c.name)
            out.append(FolderSource(APPS, f"App folder {name}", c, f"{APPS_DIR}/{name}", True))
    if MESSAGES in kinds and loc.messages.is_dir():
        out.append(FolderSource(MESSAGES, "Messages", loc.messages, MESSAGES_DIR, False))
    return out
