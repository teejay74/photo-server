#!/usr/bin/env python3
"""Small, dependency-light LAN photo gallery."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
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
except ImportError:  # The app still works; it simply serves originals as thumbnails.
    Image = ImageOps = None


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
DATE_IN_NAME = re.compile(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})?(\d{2})?(\d{2})?")
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


def safe_join(root: Path, relative: str) -> Path | None:
    try:
        target = (root / unquote(relative)).resolve()
        target.relative_to(root)
        return target
    except (ValueError, OSError):
        return None


def photo_date(path: Path, stat: os.stat_result) -> datetime:
    match = DATE_IN_NAME.search(path.stem)
    if match:
        parts = [int(value or default) for value, default in zip(match.groups(), ("2000", "01", "01", "00", "00", "00"))]
        try:
            return datetime(*parts)
        except ValueError:
            pass
    return datetime.fromtimestamp(stat.st_mtime)


class GalleryIndex:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.items: list[dict] = []
        self.timeline: list[dict] = []
        self.last_scan = 0.0
        self.lock = threading.Lock()
        self.refresh()

    def refresh(self) -> None:
        items = []
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
            taken = photo_date(path, stat)
            items.append(
                {
                    "id": hashlib.sha1(relative.encode()).hexdigest()[:16],
                    "name": path.name,
                    "path": relative,
                    "type": "video" if suffix in VIDEO_EXTENSIONS else "image",
                    "size": stat.st_size,
                    "taken": taken.isoformat(timespec="seconds"),
                    "month": taken.strftime("%Y-%m"),
                    "year": taken.year,
                    "monthNumber": taken.month,
                }
            )
        items.sort(key=lambda item: (item["taken"], item["name"]), reverse=True)
        buckets: dict[int, dict[int, int]] = {}
        for item in items:
            months = buckets.setdefault(item["year"], {})
            months[item["monthNumber"]] = months.get(item["monthNumber"], 0) + 1
        timeline = [
            {
                "year": year,
                "count": sum(months.values()),
                "months": [{"month": month, "count": count} for month, count in sorted(months.items(), reverse=True)],
            }
            for year, months in sorted(buckets.items(), reverse=True)
        ]
        with self.lock:
            self.items = items
            self.timeline = timeline
            self.last_scan = time.time()

    def query(self, offset: int, limit: int, query: str, kind: str, year: int | None = None, month: int | None = None) -> tuple[list[dict], int]:
        needle = query.casefold().strip()
        with self.lock:
            filtered = [
                item for item in self.items
                if (not needle or needle in item["name"].casefold() or needle in item["taken"])
                and (kind == "all" or item["type"] == kind)
                and (year is None or item["year"] == year)
                and (month is None or item["monthNumber"] == month)
            ]
            return filtered[offset: offset + limit], len(filtered)


class GalleryHandler(BaseHTTPRequestHandler):
    server_version = "PhotoGallery/1.0"

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
        expected = f"{self.app.username}:{self.app.password}"
        return secrets.compare_digest(supplied, expected)

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
        self.wfile.write(data)

    def send_file(self, path: Path, content_type: str | None = None, download=False):
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        start, end = 0, size - 1
        partial = False
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

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if path == "/api/photos":
            try:
                offset = max(0, int(params.get("offset", ["0"])[0]))
                limit = min(120, max(1, int(params.get("limit", ["60"])[0])))
                year = int(params["year"][0]) if params.get("year") else None
                month = int(params["month"][0]) if params.get("month") else None
            except ValueError:
                self.send_json({"error": "Некорректная пагинация"}, HTTPStatus.BAD_REQUEST)
                return
            if month is not None and month not in range(1, 13):
                self.send_json({"error": "Некорректный месяц"}, HTTPStatus.BAD_REQUEST)
                return
            query = params.get("q", [""])[0][:100]
            kind = params.get("type", ["all"])[0]
            kind = kind if kind in {"all", "image", "video"} else "all"
            items, total = self.app.index.query(offset, limit, query, kind, year, month)
            payload = []
            for item in items:
                encoded = quote(item["path"], safe="/")
                payload.append({**item, "mediaUrl": "/media/" + encoded, "thumbUrl": "/thumb/" + encoded})
            self.send_json({"items": payload, "total": total, "offset": offset, "hasMore": offset + len(payload) < total})
            return
        if path == "/api/timeline":
            with self.app.index.lock:
                timeline = list(self.app.index.timeline)
            self.send_json({"years": timeline})
            return
        if path == "/api/stats":
            items, total = self.app.index.query(0, 10**9, "", "all")
            self.send_json({
                "total": total,
                "images": sum(x["type"] == "image" for x in items),
                "videos": sum(x["type"] == "video" for x in items),
                "bytes": sum(x["size"] for x in items),
                "updated": datetime.fromtimestamp(self.app.index.last_scan).isoformat(timespec="seconds"),
            })
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


class GalleryServer(ThreadingHTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="Личная фотогалерея")
    parser.add_argument("--host", default=os.getenv("PHOTO_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PHOTO_PORT", "8080")))
    parser.add_argument("--photos", default=os.getenv("PHOTO_ROOT", "/srv/photos/xiaomi-13t/Camera"))
    parser.add_argument("--cache", default=os.getenv("PHOTO_CACHE", str(APP_DIR / ".cache")))
    args = parser.parse_args()
    photo_root = Path(args.photos).resolve()
    if not photo_root.is_dir():
        parser.error(f"Каталог не найден: {photo_root}")
    server = GalleryServer((args.host, args.port), GalleryHandler)
    server.app = argparse.Namespace(
        photo_root=photo_root,
        cache_dir=Path(args.cache).resolve(),
        username=os.getenv("PHOTO_USER", "photos"),
        password=os.getenv("PHOTO_PASSWORD", ""),
        thumbnail_slots=threading.Semaphore(2),
        index=GalleryIndex(photo_root),
    )
    def refresh_periodically():
        while True:
            time.sleep(300)
            try:
                server.app.index.refresh()
            except Exception as error:
                print(f"Index refresh failed: {error}", file=sys.stderr, flush=True)
    threading.Thread(target=refresh_periodically, name="photo-index-refresh", daemon=True).start()
    print(f"Photo gallery: http://{args.host}:{args.port} ({len(server.app.index.items)} files)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
