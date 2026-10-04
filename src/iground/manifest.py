"""SQLite manifest stored on the SSD so migrations can resume and be verified later."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional

MANIFEST_DIR = ".iground"
MANIFEST_NAME = "manifest.sqlite"

PENDING = "pending"
COPIED = "copied"
VERIFIED = "verified"
FAILED = "failed"
EVICTED = "evicted"  # copied + verified, then local copy removed from the Mac

# Photos library items
EXPORTED = "exported"

DONE_STATUSES = (COPIED, VERIFIED, EVICTED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    rel_path   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime_ns   INTEGER NOT NULL,
    sha256     TEXT,
    status     TEXT NOT NULL,
    error      TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS photos (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    taken_at   REAL,
    files      TEXT,
    error      TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Record:
    rel_path: str
    kind: str
    size: int
    mtime_ns: int
    sha256: Optional[str]
    status: str
    error: Optional[str]


class Manifest:
    def __init__(self, dest: Path) -> None:
        directory = Path(dest) / MANIFEST_DIR
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / MANIFEST_NAME
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    @classmethod
    def exists(cls, dest: Path) -> bool:
        return (Path(dest) / MANIFEST_DIR / MANIFEST_NAME).exists()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            self._db.commit()

    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def get(self, rel_path: str) -> Optional[Record]:
        with self._lock:
            row = self._db.execute(
                "SELECT rel_path, kind, size, mtime_ns, sha256, status, error FROM files WHERE rel_path = ?",
                (rel_path,),
            ).fetchone()
        return Record(*row) if row else None

    def put(
        self,
        rel_path: str,
        kind: str,
        size: int,
        mtime_ns: int,
        status: str,
        sha256: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rel_path, kind, size, mtime_ns, sha256, status, error, time.time()),
            )
            self._db.commit()

    def set_status(self, rel_path: str, status: str, error: Optional[str] = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE files SET status = ?, error = ?, updated_at = ? WHERE rel_path = ?",
                (status, error, time.time(), rel_path),
            )
            self._db.commit()

    def records(self, status: Optional[str] = None) -> Iterator[Record]:
        query = "SELECT rel_path, kind, size, mtime_ns, sha256, status, error FROM files"
        args: tuple = ()
        if status:
            query += " WHERE status = ?"
            args = (status,)
        query += " ORDER BY rel_path"
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        for row in rows:
            yield Record(*row)

    def counts(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*), COALESCE(SUM(size), 0) FROM files GROUP BY status"
            ).fetchall()
        return {status: {"files": n, "bytes": b} for status, n, b in rows}

    # --- Photos library items -------------------------------------------------

    def put_photo(
        self,
        photo_id: str,
        status: str,
        taken_at: Optional[float] = None,
        files: Optional[List[str]] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO photos VALUES (?, ?, ?, ?, ?, ?)",
                (photo_id, status, taken_at, json.dumps(files or []), error, time.time()),
            )
            self._db.commit()

    def photo_statuses(self) -> Dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT id, status FROM photos").fetchall()
        return dict(rows)

    def photo_files(self) -> Dict[str, List[str]]:
        with self._lock:
            rows = self._db.execute("SELECT id, files FROM photos WHERE status = ?", (EXPORTED,)).fetchall()
        return {pid: json.loads(files or "[]") for pid, files in rows}

    def photo_errors(self) -> List[tuple]:
        with self._lock:
            return self._db.execute(
                "SELECT id, error FROM photos WHERE status = ? ORDER BY id", (FAILED,)
            ).fetchall()
