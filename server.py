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
import threading
import time
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
    def __init__(self, root: Path, database: Path):
        self.root = root.resolve()
        self.database = database.resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.refresh_lock = threading.Lock()
        self.last_scan = ""
        self._initialize()
        self.refresh()

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

    def refresh(self) -> dict:
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
                            latitude, longitude, indexed_at
                        ) VALUES (
                            :id, :path, :name, :type, :size, :mtime_ns, :taken, :date_source,
                            :year, :month, :width, :height, :camera_make, :camera_model,
                            :latitude, :longitude, :indexed_at
                        )
                        ON CONFLICT(path) DO UPDATE SET
                            name=excluded.name, type=excluded.type, size=excluded.size,
                            mtime_ns=excluded.mtime_ns, taken=excluded.taken,
                            date_source=excluded.date_source, year=excluded.year,
                            month=excluded.month, width=excluded.width, height=excluded.height,
                            camera_make=excluded.camera_make, camera_model=excluded.camera_model,
                            latitude=excluded.latitude, longitude=excluded.longitude,
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

    @staticmethod
    def _filters(query: str, kind: str, year: int | None, month: int | None, favorite: bool, album: int | None):
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
        if favorite:
            clauses.append("EXISTS(SELECT 1 FROM favorites f WHERE f.photo_id=p.id)")
        if album is not None:
            clauses.append("EXISTS(SELECT 1 FROM album_photos ap WHERE ap.photo_id=p.id AND ap.album_id=?)")
            values.append(album)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", values

    def query(self, offset: int, limit: int, query: str, kind: str, year: int | None = None,
              month: int | None = None, favorite: bool = False, album: int | None = None) -> tuple[list[dict], int]:
        where, values = self._filters(query, kind, year, month, favorite, album)
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
        return {**dict(row), "updated": updated[0] if updated else self.last_scan}

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
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM albums WHERE id=?", (album_id,)).fetchone():
                raise KeyError("album")
            if not connection.execute("SELECT 1 FROM photos WHERE id=?", (photo_id,)).fetchone():
                raise KeyError("photo")
            if included:
                connection.execute("INSERT OR IGNORE INTO album_photos(album_id,photo_id,added_at) VALUES(?,?,?)", (album_id, photo_id, now))
            else:
                connection.execute("DELETE FROM album_photos WHERE album_id=? AND photo_id=?", (album_id, photo_id))
        return included


class GalleryHandler(BaseHTTPRequestHandler):
    server_version = "PhotoGallery/2.0"

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

    def send_file(self, path: Path, content_type: str | None = None, download=False):
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
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
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
            album = int(params["album"][0]) if params.get("album") else None
        except ValueError:
            self.send_json({"error": "Некорректные параметры"}, HTTPStatus.BAD_REQUEST)
            return
        if month is not None and month not in range(1, 13):
            self.send_json({"error": "Некорректный месяц"}, HTTPStatus.BAD_REQUEST)
            return
        query = params.get("q", [""])[0][:100]
        kind = params.get("type", ["all"])[0]
        kind = kind if kind in {"all", "image", "video"} else "all"
        favorite = params.get("favorite", ["0"])[0] == "1"
        items, total = self.app.database.query(offset, limit, query, kind, year, month, favorite, album)
        payload = [self.photo_payload(item) for item in items]
        self.send_json({"items": payload, "total": total, "offset": offset, "hasMore": offset + len(payload) < total})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
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
    args = parser.parse_args()
    photo_root = Path(args.photos).resolve()
    if not photo_root.is_dir():
        parser.error(f"Каталог не найден: {photo_root}")
    database = GalleryDatabase(photo_root, Path(args.database))
    server = GalleryServer((args.host, args.port), GalleryHandler)
    server.app = argparse.Namespace(
        photo_root=photo_root,
        cache_dir=Path(args.cache).resolve(),
        username=os.getenv("PHOTO_USER", "photos"),
        password=os.getenv("PHOTO_PASSWORD", ""),
        thumbnail_slots=threading.Semaphore(2),
        database=database,
    )

    def refresh_periodically():
        interval = max(30, int(os.getenv("PHOTO_SCAN_INTERVAL", "300")))
        while True:
            time.sleep(interval)
            try:
                database.refresh()
            except Exception as error:
                print(f"Index refresh failed: {error}", file=sys.stderr, flush=True)

    threading.Thread(target=refresh_periodically, name="photo-index-refresh", daemon=True).start()
    print(f"Photo gallery: http://{args.host}:{args.port} ({database.stats()['total']} files)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
