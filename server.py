#!/usr/bin/env python3
"""Private photo gallery with an incremental SQLite metadata index."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    from PIL import Image, ImageOps
except ImportError:  # The gallery still works, but EXIF and thumbnails are unavailable.
    Image = ImageOps = None


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
DATE_IN_NAME = re.compile(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})?(\d{2})?(\d{2})?")
PHOTO_ID = re.compile(r"^[0-9a-f]{16}$")
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


def safe_join(root: Path, relative: str) -> Path | None:
    try:
        target = (root / unquote(relative)).resolve()
        target.relative_to(root)
        return target
    except (ValueError, OSError):
        return None


def filename_date(path: Path, stat: os.stat_result) -> tuple[datetime, str]:
    match = DATE_IN_NAME.search(path.stem)
    if match:
        parts = [int(value or default) for value, default in zip(match.groups(), ("2000", "01", "01", "00", "00", "00"))]
        try:
            return datetime(*parts), "filename"
        except ValueError:
            pass
    return datetime.fromtimestamp(stat.st_mtime), "modified"


def exif_datetime(value) -> datetime | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    if not value:
        return None
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value).strip(), pattern)
        except ValueError:
            continue
    return None


def rational_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        numerator = getattr(value, "numerator", 0)
        denominator = getattr(value, "denominator", 1)
        return float(numerator) / float(denominator or 1)


def gps_coordinate(values, reference) -> float | None:
    try:
        degrees, minutes, seconds = (rational_float(value) for value in values)
        result = degrees + minutes / 60 + seconds / 3600
        if isinstance(reference, bytes):
            reference = reference.decode("ascii", "ignore")
        if str(reference).upper() in {"S", "W"}:
            result *= -1
        return round(result, 7)
    except (TypeError, ValueError):
        return None


def difference_hash(image) -> str:
    """Return a small visual fingerprint suitable for finding similar photos."""
    try:
        image.draft("L", (64, 64))
    except (AttributeError, OSError):
        pass
    grayscale = ImageOps.exif_transpose(image).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(grayscale.get_flattened_data() if hasattr(grayscale, "get_flattened_data") else grayscale.getdata())
    value = 0
    for row in range(8):
        for column in range(8):
            value = (value << 1) | int(pixels[row * 9 + column] > pixels[row * 9 + column + 1])
    return f"{value:016x}"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def image_metadata(path: Path, fallback: datetime, fallback_source: str) -> dict:
    metadata = {
        "taken": fallback,
        "date_source": fallback_source,
        "width": None,
        "height": None,
        "camera_make": None,
        "camera_model": None,
        "latitude": None,
        "longitude": None,
        "perceptual_hash": None,
    }
    if Image is None:
        return metadata
    try:
        with Image.open(path) as image:
            metadata["width"], metadata["height"] = image.size
            exif = image.getexif()
            if not exif:
                return metadata
            taken = exif_datetime(exif.get(36867)) or exif_datetime(exif.get(36868)) or exif_datetime(exif.get(306))
            if taken:
                metadata["taken"] = taken
                metadata["date_source"] = "exif"
            make = exif.get(271)
            model = exif.get(272)
            metadata["camera_make"] = str(make).strip()[:120] if make else None
            metadata["camera_model"] = str(model).strip()[:120] if model else None
            try:
                gps = exif.get_ifd(34853)
            except (AttributeError, KeyError, TypeError):
                gps = exif.get(34853) or {}
            if gps:
                metadata["latitude"] = gps_coordinate(gps.get(2), gps.get(1))
                metadata["longitude"] = gps_coordinate(gps.get(4), gps.get(3))
    except Exception:
        pass
    return metadata


class GalleryDatabase:
    def __init__(self, root: Path, database: Path, backup_dir: Path | None = None,
                 marker_file: str = ".photodom-storage"):
        self.root = root.resolve()
        self.database = database.resolve()
        self.backup_dir = (backup_dir or self.database.parent / "backups").resolve()
        marker_file = marker_file.strip()
        if not marker_file or Path(marker_file).name != marker_file or marker_file in {".", ".."}:
            raise ValueError("Некорректное имя файла-маркера")
        self.marker_file = marker_file
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.refresh_lock = threading.Lock()
        self.hash_lock = threading.Lock()
        self.last_scan = ""
        self._initialize()
        self.refresh()

    def storage_available(self) -> bool:
        """The marker distinguishes a mounted photo disk from an empty mount point."""
        return self.root.is_dir() and (self.root / self.marker_file).is_file()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS photos (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('image', 'video')),
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    taken TEXT NOT NULL,
                    date_source TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL,
                    width INTEGER,
                    height INTEGER,
                    camera_make TEXT,
                    camera_model TEXT,
                    latitude REAL,
                    longitude REAL,
                    content_hash TEXT,
                    perceptual_hash TEXT,
                    indexed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS photos_taken_idx ON photos(taken DESC);
                CREATE INDEX IF NOT EXISTS photos_period_idx ON photos(year DESC, month DESC, taken DESC);
                CREATE INDEX IF NOT EXISTS photos_location_idx ON photos(latitude, longitude);

                CREATE TABLE IF NOT EXISTS favorites (
                    photo_id TEXT PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS albums (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS album_photos (
                    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                    photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY(album_id, photo_id)
                );
                CREATE INDEX IF NOT EXISTS album_photos_photo_idx ON album_photos(photo_id);

                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(photos)")}
            if "content_hash" not in columns:
                connection.execute("ALTER TABLE photos ADD COLUMN content_hash TEXT")
            if "perceptual_hash" not in columns:
                connection.execute("ALTER TABLE photos ADD COLUMN perceptual_hash TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS photos_content_hash_idx ON photos(content_hash)")
            connection.execute("CREATE INDEX IF NOT EXISTS photos_perceptual_hash_idx ON photos(perceptual_hash)")

    def refresh(self) -> dict:
        if not self.storage_available():
            result = {"status": "storage_unavailable", "marker": self.marker_file}
            print(f"Index refresh skipped: storage marker {self.marker_file!r} is missing", file=sys.stderr, flush=True)
            return result
        if not self.refresh_lock.acquire(blocking=False):
            return {"status": "already_running"}
        try:
            with self.connect() as connection:
                existing = {
                    row["path"]: (row["mtime_ns"], row["size"])
                    for row in connection.execute("SELECT path, mtime_ns, size FROM photos")
                }
            seen: set[str] = set()
            changed: list[dict] = []
            scanned = 0
            for path in self.root.rglob("*"):
                if not path.is_file() or path.name.startswith("."):
                    continue
                suffix = path.suffix.lower()
                if suffix not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                relative = path.relative_to(self.root).as_posix()
                seen.add(relative)
                scanned += 1
                if existing.get(relative) == (stat.st_mtime_ns, stat.st_size):
                    continue
                fallback, source = filename_date(path, stat)
                metadata = image_metadata(path, fallback, source) if suffix in IMAGE_EXTENSIONS else {
                    "taken": fallback,
                    "date_source": source,
                    "width": None,
                    "height": None,
                    "camera_make": None,
                    "camera_model": None,
                    "latitude": None,
                    "longitude": None,
                    "perceptual_hash": None,
                }
                taken = metadata.pop("taken")
                changed.append(
                    {
                        "id": hashlib.sha1(relative.encode()).hexdigest()[:16],
                        "path": relative,
                        "name": path.name,
                        "type": "video" if suffix in VIDEO_EXTENSIONS else "image",
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "taken": taken.isoformat(timespec="seconds"),
                        "year": taken.year,
                        "month": taken.month,
                        "indexed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        **metadata,
                    }
                )
            removed = sorted(set(existing) - seen)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            with self.connect() as connection:
                if changed:
                    connection.executemany(
                        """
                        INSERT INTO photos (
                            id, path, name, type, size, mtime_ns, taken, date_source,
                            year, month, width, height, camera_make, camera_model,
                            latitude, longitude, content_hash, perceptual_hash, indexed_at
                        ) VALUES (
                            :id, :path, :name, :type, :size, :mtime_ns, :taken, :date_source,
                            :year, :month, :width, :height, :camera_make, :camera_model,
                            :latitude, :longitude, NULL, :perceptual_hash, :indexed_at
                        )
                        ON CONFLICT(path) DO UPDATE SET
                            name=excluded.name, type=excluded.type, size=excluded.size,
                            mtime_ns=excluded.mtime_ns, taken=excluded.taken,
                            date_source=excluded.date_source, year=excluded.year,
                            month=excluded.month, width=excluded.width, height=excluded.height,
                            camera_make=excluded.camera_make, camera_model=excluded.camera_model,
                            latitude=excluded.latitude, longitude=excluded.longitude,
                            content_hash=NULL, perceptual_hash=excluded.perceptual_hash,
                            indexed_at=excluded.indexed_at
                        """,
                        changed,
                    )
                if removed:
                    connection.executemany("DELETE FROM photos WHERE path=?", ((path,) for path in removed))
                connection.execute(
                    "INSERT INTO app_meta(key,value) VALUES('last_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (now,),
                )
            self.last_scan = now
            result = {"status": "complete", "scanned": scanned, "updated": len(changed), "removed": len(removed)}
            print(f"Index refresh: {result}", flush=True)
            return result
        finally:
            self.refresh_lock.release()

    def enrich_hashes(self) -> dict:
        """Fill missing visual hashes and exact hashes only for equal-size candidates."""
        if not self.storage_available():
            return {"status": "storage_unavailable", "visual": 0, "exact": 0}
        if not self.hash_lock.acquire(blocking=False):
            return {"status": "already_running", "visual": 0, "exact": 0}
        visual_updates: list[tuple[str, str]] = []
        exact_updates: list[tuple[str, str]] = []
        visual_count = exact_count = 0

        def flush(sql: str, updates: list[tuple[str, str]]) -> int:
            if not updates:
                return 0
            with self.connect() as connection:
                connection.executemany(sql, updates)
            count = len(updates)
            updates.clear()
            return count

        try:
            with self.connect() as connection:
                visual_rows = connection.execute(
                    "SELECT id,path FROM photos WHERE type='image' AND perceptual_hash IS NULL"
                ).fetchall()
                exact_rows = connection.execute(
                    """
                    SELECT p.id,p.path FROM photos p
                    JOIN (SELECT size FROM photos GROUP BY size HAVING COUNT(*)>1) candidates USING(size)
                    WHERE p.content_hash IS NULL
                    """
                ).fetchall()
            for row in visual_rows:
                target = safe_join(self.root, row["path"])
                fingerprint = ""
                if target and target.is_file() and Image is not None:
                    try:
                        with Image.open(target) as image:
                            fingerprint = difference_hash(image)
                    except Exception:
                        pass
                visual_updates.append((fingerprint, row["id"]))
                if len(visual_updates) >= 100:
                    visual_count += flush("UPDATE photos SET perceptual_hash=? WHERE id=?", visual_updates)
            for row in exact_rows:
                target = safe_join(self.root, row["path"])
                fingerprint = ""
                if target and target.is_file():
                    try:
                        fingerprint = file_sha256(target)
                    except OSError:
                        pass
                exact_updates.append((fingerprint, row["id"]))
                if len(exact_updates) >= 100:
                    exact_count += flush("UPDATE photos SET content_hash=? WHERE id=?", exact_updates)
            visual_count += flush("UPDATE photos SET perceptual_hash=? WHERE id=?", visual_updates)
            exact_count += flush("UPDATE photos SET content_hash=? WHERE id=?", exact_updates)
            result = {"status": "complete", "visual": visual_count, "exact": exact_count}
            if visual_count or exact_count:
                print(f"Hash enrichment: {result}", flush=True)
            return result
        finally:
            self.hash_lock.release()

    @staticmethod
    def _filters(query: str, kind: str, year: int | None, month: int | None, favorite: bool,
                 album: int | None, filters: dict | None = None, day: int | None = None,
                 before_year: int | None = None):
        clauses: list[str] = []
        values: list = []
        if query.strip():
            needle = f"%{query.strip()}%"
            clauses.append("(p.name LIKE ? COLLATE NOCASE OR p.taken LIKE ? OR p.camera_make LIKE ? COLLATE NOCASE OR p.camera_model LIKE ? COLLATE NOCASE)")
            values.extend([needle, needle, needle, needle])
        if kind != "all":
            clauses.append("p.type=?")
            values.append(kind)
        if year is not None:
            clauses.append("p.year=?")
            values.append(year)
        if month is not None:
            clauses.append("p.month=?")
            values.append(month)
        if day is not None:
            clauses.append("CAST(SUBSTR(p.taken,9,2) AS INTEGER)=?")
            values.append(day)
        if before_year is not None:
            clauses.append("p.year<=?")
            values.append(before_year)
        if favorite:
            clauses.append("EXISTS(SELECT 1 FROM favorites f WHERE f.photo_id=p.id)")
        if album is not None:
            clauses.append("EXISTS(SELECT 1 FROM album_photos ap WHERE ap.photo_id=p.id AND ap.album_id=?)")
            values.append(album)
        filters = filters or {}
        if filters.get("camera"):
            clauses.append("p.camera_model=?")
            values.append(filters["camera"])
        if filters.get("gps") == "yes":
            clauses.append("p.latitude IS NOT NULL AND p.longitude IS NOT NULL")
        elif filters.get("gps") == "no":
            clauses.append("(p.latitude IS NULL OR p.longitude IS NULL)")
        if filters.get("date_from"):
            clauses.append("p.taken>=?")
            values.append(filters["date_from"] + "T00:00:00")
        if filters.get("date_to"):
            clauses.append("p.taken<=?")
            values.append(filters["date_to"] + "T23:59:59")
        if filters.get("min_size") is not None:
            clauses.append("p.size>=?")
            values.append(filters["min_size"])
        if filters.get("max_size") is not None:
            clauses.append("p.size<=?")
            values.append(filters["max_size"])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", values

    def query(self, offset: int, limit: int, query: str, kind: str, year: int | None = None,
              month: int | None = None, favorite: bool = False, album: int | None = None,
              filters: dict | None = None, day: int | None = None,
              before_year: int | None = None) -> tuple[list[dict], int]:
        where, values = self._filters(query, kind, year, month, favorite, album, filters, day, before_year)
        select = """
            SELECT p.*,
                EXISTS(SELECT 1 FROM favorites f WHERE f.photo_id=p.id) AS favorite,
                (SELECT GROUP_CONCAT(ap.album_id) FROM album_photos ap WHERE ap.photo_id=p.id) AS album_ids
            FROM photos p
        """
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM photos p" + where, values).fetchone()[0]
            rows = connection.execute(select + where + " ORDER BY p.taken DESC, p.name DESC LIMIT ? OFFSET ?", [*values, limit, offset]).fetchall()
        return [self._photo(row) for row in rows], total

    @staticmethod
    def _photo(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["favorite"] = bool(item.get("favorite"))
        album_ids = item.pop("album_ids", None)
        item["albumIds"] = [int(value) for value in album_ids.split(",")] if album_ids else []
        item["monthNumber"] = item.pop("month")
        item["month"] = item["taken"][:7]
        item["cameraMake"] = item.pop("camera_make")
        item["cameraModel"] = item.pop("camera_model")
        item["dateSource"] = item.pop("date_source")
        item.pop("mtime_ns", None)
        item.pop("indexed_at", None)
        item.pop("content_hash", None)
        item.pop("perceptual_hash", None)
        return item

    def timeline(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT year, month, COUNT(*) count FROM photos GROUP BY year, month ORDER BY year DESC, month DESC"
            ).fetchall()
        years: dict[int, dict] = {}
        for row in rows:
            entry = years.setdefault(row["year"], {"year": row["year"], "count": 0, "months": []})
            entry["count"] += row["count"]
            entry["months"].append({"month": row["month"], "count": row["count"]})
        return list(years.values())

    def stats(self) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) total, COALESCE(SUM(type='image'),0) images, COALESCE(SUM(type='video'),0) videos, COALESCE(SUM(size),0) bytes, COALESCE(SUM(latitude IS NOT NULL AND longitude IS NOT NULL),0) located FROM photos"
            ).fetchone()
            updated = connection.execute("SELECT value FROM app_meta WHERE key='last_scan'").fetchone()
        return {
            **dict(row),
            "updated": updated[0] if updated else self.last_scan,
            "storageAvailable": self.storage_available(),
            "storageMarker": self.marker_file,
        }

    def map_items(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,name,path,taken,latitude,longitude FROM photos WHERE latitude IS NOT NULL AND longitude IS NOT NULL ORDER BY taken DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_favorite(self, photo_id: str, favorite: bool) -> bool:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM photos WHERE id=?", (photo_id,)).fetchone():
                raise KeyError(photo_id)
            if favorite:
                connection.execute("INSERT OR IGNORE INTO favorites(photo_id,created_at) VALUES(?,?)", (photo_id, now))
            else:
                connection.execute("DELETE FROM favorites WHERE photo_id=?", (photo_id,))
        return favorite

    def set_favorites(self, photo_ids: list[str], favorite: bool) -> int:
        valid = list(dict.fromkeys(str(value) for value in photo_ids if PHOTO_ID.fullmatch(str(value))))
        if not valid:
            return 0
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        placeholders = ",".join("?" for _ in valid)
        with self.connect() as connection:
            found = [row[0] for row in connection.execute(
                f"SELECT id FROM photos WHERE id IN ({placeholders})", valid
            )]
            if favorite:
                connection.executemany(
                    "INSERT OR IGNORE INTO favorites(photo_id,created_at) VALUES(?,?)",
                    ((photo_id, now) for photo_id in found),
                )
            elif found:
                found_placeholders = ",".join("?" for _ in found)
                connection.execute(f"DELETE FROM favorites WHERE photo_id IN ({found_placeholders})", found)
        return len(found)

    def import_favorites(self, photo_ids: list[str]) -> int:
        valid = [value for value in photo_ids if PHOTO_ID.fullmatch(str(value))]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO favorites(photo_id,created_at) SELECT id,? FROM photos WHERE id=?",
                ((now, photo_id) for photo_id in valid),
            )
            return connection.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]

    def albums(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.id,a.name,a.created_at,COUNT(ap.photo_id) count,
                    (SELECT p.path FROM album_photos cap JOIN photos p ON p.id=cap.photo_id WHERE cap.album_id=a.id ORDER BY p.taken DESC LIMIT 1) cover_path
                FROM albums a LEFT JOIN album_photos ap ON ap.album_id=a.id
                GROUP BY a.id ORDER BY a.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def create_album(self, name: str) -> dict:
        name = " ".join(name.strip().split())[:80]
        if not name:
            raise ValueError("Название альбома не может быть пустым")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            with self.connect() as connection:
                cursor = connection.execute("INSERT INTO albums(name,created_at) VALUES(?,?)", (name, now))
                album_id = cursor.lastrowid
        except sqlite3.IntegrityError as error:
            raise ValueError("Альбом с таким названием уже существует") from error
        return {"id": album_id, "name": name, "created_at": now, "count": 0, "cover_path": None}

    def delete_album(self, album_id: int) -> bool:
        with self.connect() as connection:
            return connection.execute("DELETE FROM albums WHERE id=?", (album_id,)).rowcount > 0

    def set_album_photo(self, album_id: int, photo_id: str, included: bool) -> bool:
        if not PHOTO_ID.fullmatch(photo_id) or not self.set_album_photos(album_id, [photo_id], included):
            raise KeyError("photo")
        return included

    def set_album_photos(self, album_id: int, photo_ids: list[str], included: bool) -> int:
        valid = list(dict.fromkeys(str(value) for value in photo_ids if PHOTO_ID.fullmatch(str(value))))
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM albums WHERE id=?", (album_id,)).fetchone():
                raise KeyError("album")
            if not valid:
                return 0
            placeholders = ",".join("?" for _ in valid)
            found = [row[0] for row in connection.execute(
                f"SELECT id FROM photos WHERE id IN ({placeholders})", valid
            )]
            if included:
                connection.executemany(
                    "INSERT OR IGNORE INTO album_photos(album_id,photo_id,added_at) VALUES(?,?,?)",
                    ((album_id, photo_id, now) for photo_id in found),
                )
            elif found:
                found_placeholders = ",".join("?" for _ in found)
                connection.execute(
                    f"DELETE FROM album_photos WHERE album_id=? AND photo_id IN ({found_placeholders})",
                    [album_id, *found],
                )
        return len(found)

    def filter_options(self) -> dict:
        with self.connect() as connection:
            cameras = connection.execute(
                "SELECT camera_model value,COUNT(*) count FROM photos WHERE camera_model IS NOT NULL AND camera_model!='' GROUP BY camera_model ORDER BY count DESC,value"
            ).fetchall()
            sizes = connection.execute("SELECT MIN(size) minimum,MAX(size) maximum FROM photos").fetchone()
        return {"cameras": [dict(row) for row in cameras], "sizes": dict(sizes)}

    def hash_progress(self) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) total,COALESCE(SUM(perceptual_hash IS NOT NULL),0) ready FROM photos WHERE type='image'"
            ).fetchone()
        return {"total": row["total"], "ready": row["ready"], "running": self.hash_lock.locked()}

    def photos_by_ids(self, photo_ids: list[str]) -> list[dict]:
        valid = list(dict.fromkeys(str(value) for value in photo_ids if PHOTO_ID.fullmatch(str(value))))
        if not valid:
            return []
        placeholders = ",".join("?" for _ in valid)
        select = """
            SELECT p.*,
                EXISTS(SELECT 1 FROM favorites f WHERE f.photo_id=p.id) AS favorite,
                (SELECT GROUP_CONCAT(ap.album_id) FROM album_photos ap WHERE ap.photo_id=p.id) AS album_ids
            FROM photos p
        """
        with self.connect() as connection:
            rows = connection.execute(f"{select} WHERE p.id IN ({placeholders})", valid).fetchall()
        by_id = {row["id"]: self._photo(row) for row in rows}
        return [by_id[photo_id] for photo_id in valid if photo_id in by_id]

    def export_items(self, photo_ids: list[str]) -> list[dict]:
        result = []
        for item in self.photos_by_ids(photo_ids):
            target = safe_join(self.root, item["path"])
            if target and target.is_file():
                result.append({**item, "target": target})
        return result

    def duplicate_groups(self, mode: str = "exact", limit: int = 100) -> list[dict]:
        if mode == "exact":
            with self.connect() as connection:
                hashes = connection.execute(
                    "SELECT content_hash,COUNT(*) count,SUM(size) bytes FROM photos WHERE content_hash IS NOT NULL AND content_hash!='' GROUP BY content_hash HAVING COUNT(*)>1 ORDER BY bytes DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                groups = []
                for entry in hashes:
                    rows = connection.execute(
                        "SELECT * FROM photos WHERE content_hash=? ORDER BY taken DESC", (entry["content_hash"],)
                    ).fetchall()
                    groups.append({"kind": "exact", "count": entry["count"], "bytes": entry["bytes"], "items": [self._photo(row) for row in rows]})
                return groups

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM photos WHERE type='image' AND perceptual_hash IS NOT NULL AND perceptual_hash!='' ORDER BY taken"
            ).fetchall()
        parents = list(range(len(rows)))

        def find(value):
            while parents[value] != value:
                parents[value] = parents[parents[value]]
                value = parents[value]
            return value

        def union(left, right):
            left, right = find(left), find(right)
            if left != right:
                parents[right] = left

        timestamps = [datetime.fromisoformat(row["taken"]).timestamp() for row in rows]
        for left, row in enumerate(rows):
            for right in range(left + 1, len(rows)):
                if timestamps[right] - timestamps[left] > 600:
                    break
                other = rows[right]
                if row["width"] and other["width"]:
                    ratio = row["width"] / max(1, row["height"] or 1)
                    other_ratio = other["width"] / max(1, other["height"] or 1)
                    if abs(ratio - other_ratio) > 0.08:
                        continue
                distance = (int(row["perceptual_hash"], 16) ^ int(other["perceptual_hash"], 16)).bit_count()
                if distance <= 6:
                    union(left, right)
        grouped: dict[int, list] = {}
        for index, row in enumerate(rows):
            grouped.setdefault(find(index), []).append(row)
        result = [{
            "kind": "similar", "count": len(members), "bytes": sum(row["size"] for row in members),
            "items": [self._photo(row) for row in reversed(members)],
        } for members in grouped.values() if len(members) > 1]
        result.sort(key=lambda group: group["bytes"], reverse=True)
        return result[:limit]

    def create_backup(self, retention: int = 7) -> dict:
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        target = self.backup_dir / f"gallery-{timestamp}.db"
        temporary = self.backup_dir / f".{target.name}.{os.getpid()}.tmp"
        try:
            with self.connect() as source, sqlite3.connect(temporary) as destination:
                source.backup(destination)
                integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise RuntimeError(f"SQLite integrity check: {integrity}")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        backups = sorted(self.backup_dir.glob("gallery-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in backups[max(1, retention):]:
            old.unlink(missing_ok=True)
        return {"name": target.name, "created": datetime.fromtimestamp(target.stat().st_mtime).astimezone().isoformat(timespec="seconds"), "size": target.stat().st_size}

    def backup_status(self) -> dict:
        backups = sorted(self.backup_dir.glob("gallery-*.db"), key=lambda path: path.stat().st_mtime, reverse=True)
        return {"backups": [{"name": path.name, "created": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"), "size": path.stat().st_size} for path in backups]}

    def maybe_backup(self, max_age_hours: int = 24, retention: int = 7) -> dict | None:
        backups = self.backup_status()["backups"]
        if backups:
            latest = datetime.fromisoformat(backups[0]["created"])
            if (datetime.now().astimezone() - latest).total_seconds() < max_age_hours * 3600:
                return None
        return self.create_backup(retention)


class GalleryHandler(BaseHTTPRequestHandler):
    server_version = "PhotoGallery/3.0"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def authenticated(self) -> bool:
        if not self.app.username or not self.app.password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            supplied = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        return secrets.compare_digest(supplied, f"{self.app.username}:{self.app.password}")

    def require_auth(self) -> bool:
        if self.authenticated():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Private photo gallery", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def send_json(self, value, status=HTTPStatus.OK):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise ValueError("Некорректный JSON")

    def send_file(self, path: Path, content_type: str | None = None, download=False, download_name: str | None = None):
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        start, end, partial = 0, size - 1, False
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            try:
                first, last = range_header[6:].split("-", 1)
                start = int(first) if first else max(0, size - int(last))
                end = min(size - 1, int(last)) if last else size - 1
                if start < 0 or start > end:
                    raise ValueError
                partial = True
            except ValueError:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download_name or path.name)}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    def thumbnail(self, relative: str):
        source = safe_join(self.app.photo_root, relative)
        if not source or not source.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if source.suffix.lower() not in IMAGE_EXTENSIONS or Image is None:
            self.send_file(source)
            return
        stat = source.stat()
        key = hashlib.sha1(f"{relative}:{stat.st_mtime_ns}".encode()).hexdigest()
        cached = self.app.cache_dir / key[:2] / f"{key}.jpg"
        if not cached.exists():
            with self.app.thumbnail_slots:
                if not cached.exists():
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    temporary = cached.with_name(f"{cached.name}.{threading.get_ident()}.tmp")
                    try:
                        with Image.open(source) as image:
                            image.draft("RGB", (640, 640))
                            image = ImageOps.exif_transpose(image)
                            image.thumbnail((640, 640), Image.Resampling.LANCZOS)
                            if image.mode != "RGB":
                                image = image.convert("RGB")
                            image.save(temporary, "JPEG", quality=82, optimize=True)
                        temporary.replace(cached)
                    except Exception:
                        temporary.unlink(missing_ok=True)
                        self.send_file(source)
                        return
        self.send_file(cached, "image/jpeg")

    @staticmethod
    def photo_payload(item: dict) -> dict:
        encoded = quote(item["path"], safe="/")
        return {**item, "mediaUrl": "/media/" + encoded, "thumbUrl": "/thumb/" + encoded}

    def api_photos(self, params: dict):
        try:
            offset = max(0, int(params.get("offset", ["0"])[0]))
            limit = min(120, max(1, int(params.get("limit", ["60"])[0])))
            year = int(params["year"][0]) if params.get("year") else None
            month = int(params["month"][0]) if params.get("month") else None
            day = int(params["day"][0]) if params.get("day") else None
            before_year = int(params["before_year"][0]) if params.get("before_year") else None
            album = int(params["album"][0]) if params.get("album") else None
            min_size = int(float(params["min_mb"][0]) * 1024 * 1024) if params.get("min_mb") else None
            max_size = int(float(params["max_mb"][0]) * 1024 * 1024) if params.get("max_mb") else None
        except ValueError:
            self.send_json({"error": "Некорректные параметры"}, HTTPStatus.BAD_REQUEST)
            return
        if month is not None and month not in range(1, 13):
            self.send_json({"error": "Некорректный месяц"}, HTTPStatus.BAD_REQUEST)
            return
        if day is not None and day not in range(1, 32):
            self.send_json({"error": "Некорректный день"}, HTTPStatus.BAD_REQUEST)
            return
        query = params.get("q", [""])[0][:100]
        kind = params.get("type", ["all"])[0]
        kind = kind if kind in {"all", "image", "video"} else "all"
        favorite = params.get("favorite", ["0"])[0] == "1"
        gps = params.get("gps", [""])[0]
        filters = {
            "camera": params.get("camera", [""])[0][:120],
            "gps": gps if gps in {"yes", "no"} else "",
            "date_from": params.get("date_from", [""])[0][:10],
            "date_to": params.get("date_to", [""])[0][:10],
            "min_size": min_size,
            "max_size": max_size,
        }
        items, total = self.app.database.query(
            offset, limit, query, kind, year, month, favorite, album, filters, day, before_year
        )
        payload = [self.photo_payload(item) for item in items]
        self.send_json({"items": payload, "total": total, "offset": offset, "hasMore": offset + len(payload) < total})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            available = self.app.database.storage_available()
            self.send_json(
                {"status": "ok" if available else "storage_unavailable"},
                HTTPStatus.OK if available else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if not self.require_auth():
            return
        path, params = parsed.path, parse_qs(parsed.query)
        if path == "/api/photos":
            self.api_photos(params)
            return
        if path == "/api/timeline":
            self.send_json({"years": self.app.database.timeline()})
            return
        if path == "/api/stats":
            self.send_json(self.app.database.stats())
            return
        if path == "/api/filters":
            self.send_json(self.app.database.filter_options())
            return
        if path == "/api/duplicates":
            mode = params.get("mode", ["exact"])[0]
            if mode not in {"exact", "similar"}:
                self.send_json({"error": "Неизвестный режим"}, HTTPStatus.BAD_REQUEST)
                return
            groups = self.app.database.duplicate_groups(mode)
            for group in groups:
                group["items"] = [self.photo_payload(item) for item in group["items"]]
            self.send_json({"groups": groups, "mode": mode, "index": self.app.database.hash_progress()})
            return
        if path == "/api/backups":
            self.send_json(self.app.database.backup_status())
            return
        if path == "/api/map":
            items = []
            for item in self.app.database.map_items():
                encoded = quote(item["path"], safe="/")
                items.append({**item, "thumbUrl": "/thumb/" + encoded})
            self.send_json({"items": items})
            return
        if path == "/api/albums":
            albums = []
            for album in self.app.database.albums():
                cover = album.pop("cover_path")
                album["coverUrl"] = "/thumb/" + quote(cover, safe="/") if cover else None
                albums.append(album)
            self.send_json({"albums": albums})
            return
        if path.startswith("/media/") or path.startswith("/download/"):
            prefix = "/download/" if path.startswith("/download/") else "/media/"
            target = safe_join(self.app.photo_root, path[len(prefix):])
            if not target or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_file(target, download=prefix == "/download/")
            return
        if path.startswith("/thumb/"):
            self.thumbnail(path[len("/thumb/"):])
            return
        static_name = "index.html" if path == "/" else path.removeprefix("/static/")
        if static_name not in {"index.html", "app.js", "styles.css"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        target = STATIC_DIR / static_name
        self.send_file(target, {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}[target.suffix])

    def do_POST(self):
        if not self.require_auth():
            return
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            favorite_match = re.fullmatch(r"/api/favorites/([0-9a-f]{16})", path)
            album_photo_match = re.fullmatch(r"/api/albums/(\d+)/photos/([0-9a-f]{16})", path)
            if favorite_match:
                value = self.app.database.set_favorite(favorite_match.group(1), bool(body.get("favorite", True)))
                self.send_json({"favorite": value})
                return
            if path == "/api/favorites/bulk":
                count = self.app.database.set_favorites(body.get("photoIds", []), bool(body.get("favorite", True)))
                self.send_json({"count": count, "favorite": bool(body.get("favorite", True))})
                return
            if path == "/api/favorites/import":
                count = self.app.database.import_favorites(body.get("photoIds", []))
                self.send_json({"count": count})
                return
            if path == "/api/albums":
                album = self.app.database.create_album(str(body.get("name", "")))
                self.send_json(album, HTTPStatus.CREATED)
                return
            if album_photo_match:
                included = self.app.database.set_album_photo(int(album_photo_match.group(1)), album_photo_match.group(2), bool(body.get("included", True)))
                self.send_json({"included": included})
                return
            album_photos_match = re.fullmatch(r"/api/albums/(\d+)/photos", path)
            if album_photos_match:
                included = bool(body.get("included", True))
                count = self.app.database.set_album_photos(int(album_photos_match.group(1)), body.get("photoIds", []), included)
                self.send_json({"included": included, "count": count})
                return
            if path == "/api/backups":
                backup = self.app.database.create_backup(self.app.backup_retention)
                self.send_json(backup, HTTPStatus.CREATED)
                return
            if path == "/api/download/zip":
                photo_ids = body.get("photoIds", [])
                if not isinstance(photo_ids, list) or not photo_ids or len(photo_ids) > 500:
                    self.send_json({"error": "Выберите от 1 до 500 файлов"}, HTTPStatus.BAD_REQUEST)
                    return
                items = self.app.database.export_items(photo_ids)
                if not items:
                    self.send_json({"error": "Файлы не найдены"}, HTTPStatus.NOT_FOUND)
                    return
                if sum(item["size"] for item in items) > 1024 ** 3:
                    self.send_json({"error": "Архив ограничен размером 1 ГБ"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                temporary = tempfile.NamedTemporaryFile(prefix="photodom-", suffix=".zip", dir=self.app.export_dir, delete=False)
                temporary.close()
                archive_path = Path(temporary.name)
                try:
                    names: dict[str, int] = {}
                    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                        for item in items:
                            arcname = item["name"]
                            names[arcname] = names.get(arcname, 0) + 1
                            if names[arcname] > 1:
                                stem, suffix = Path(arcname).stem, Path(arcname).suffix
                                arcname = f"{stem}-{names[arcname]}{suffix}"
                            archive.write(item["target"], arcname=arcname)
                    self.send_file(archive_path, "application/zip", download=True, download_name="photodom-selection.zip")
                finally:
                    archive_path.unlink(missing_ok=True)
                return
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except KeyError:
            self.send_json({"error": "Объект не найден"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"error": "Маршрут не найден"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self):
        if not self.require_auth():
            return
        path = urlparse(self.path).path
        album_match = re.fullmatch(r"/api/albums/(\d+)", path)
        if album_match:
            deleted = self.app.database.delete_album(int(album_match.group(1)))
            self.send_json({"deleted": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
            return
        self.send_json({"error": "Маршрут не найден"}, HTTPStatus.NOT_FOUND)


class GalleryServer(ThreadingHTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="Личная фотогалерея")
    parser.add_argument("--host", default=os.getenv("PHOTO_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PHOTO_PORT", "8080")))
    parser.add_argument("--photos", default=os.getenv("PHOTO_ROOT", "/srv/photos/xiaomi-13t/Camera"))
    parser.add_argument("--cache", default=os.getenv("PHOTO_CACHE", str(APP_DIR / ".cache")))
    parser.add_argument("--database", default=os.getenv("PHOTO_DB", str(APP_DIR / "data/gallery.db")))
    parser.add_argument("--backups", default=os.getenv("PHOTO_BACKUP_DIR", str(APP_DIR / "backups")))
    args = parser.parse_args()
    photo_root = Path(args.photos).resolve()
    if not photo_root.is_dir():
        parser.error(f"Каталог не найден: {photo_root}")
    backup_retention = max(1, int(os.getenv("PHOTO_BACKUP_RETENTION", "7")))
    backup_interval = max(1, int(os.getenv("PHOTO_BACKUP_INTERVAL", "24")))
    marker_file = os.getenv("PHOTO_MARKER_FILE", ".photodom-storage")
    database = GalleryDatabase(photo_root, Path(args.database), Path(args.backups), marker_file)
    export_dir = Path(args.database).resolve().parent / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    server = GalleryServer((args.host, args.port), GalleryHandler)
    server.app = argparse.Namespace(
        photo_root=photo_root,
        cache_dir=Path(args.cache).resolve(),
        username=os.getenv("PHOTO_USER", "photos"),
        password=os.getenv("PHOTO_PASSWORD", ""),
        thumbnail_slots=threading.Semaphore(2),
        database=database,
        export_dir=export_dir,
        backup_retention=backup_retention,
    )

    def refresh_periodically():
        interval = max(30, int(os.getenv("PHOTO_SCAN_INTERVAL", "300")))
        while True:
            time.sleep(interval)
            try:
                database.refresh()
            except Exception as error:
                print(f"Index refresh failed: {error}", file=sys.stderr, flush=True)

    def enrich_hashes_periodically():
        interval = max(60, int(os.getenv("PHOTO_SCAN_INTERVAL", "300")))
        while True:
            try:
                database.enrich_hashes()
            except Exception as error:
                print(f"Hash enrichment failed: {error}", file=sys.stderr, flush=True)
            time.sleep(interval)

    def backup_periodically():
        while True:
            try:
                backup = database.maybe_backup(backup_interval, backup_retention)
                if backup:
                    print(f"SQLite backup: {backup['name']}", flush=True)
            except Exception as error:
                print(f"SQLite backup failed: {error}", file=sys.stderr, flush=True)
            time.sleep(3600)

    threading.Thread(target=refresh_periodically, name="photo-index-refresh", daemon=True).start()
    threading.Thread(target=enrich_hashes_periodically, name="photo-hash-enrichment", daemon=True).start()
    threading.Thread(target=backup_periodically, name="photo-db-backup", daemon=True).start()
    print(f"Photo gallery: http://{args.host}:{args.port} ({database.stats()['total']} files)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
