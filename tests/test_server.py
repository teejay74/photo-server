import argparse
import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

from server import GalleryDatabase, GalleryHandler, GalleryServer, gps_coordinate


class GalleryDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "photos"
        self.root.mkdir()
        (self.root / ".photodom-storage").write_text("PhotoDom storage\n")
        image = Image.new("RGB", (80, 60), "#829c45")
        exif = Image.Exif()
        exif[36867] = "2022:07:08 09:10:11"
        exif[271] = "Xiaomi"
        exif[272] = "Test Camera"
        image.save(self.root / "holiday.jpg", exif=exif)
        (self.root / "VID_20240102_030405.mp4").write_bytes(b"video")
        self.database = GalleryDatabase(self.root, Path(self.temporary.name) / "data" / "gallery.db")

    def tearDown(self):
        self.temporary.cleanup()

    def test_exif_and_incremental_refresh(self):
        items, total = self.database.query(0, 10, "", "all")
        photo = next(item for item in items if item["type"] == "image")
        self.assertEqual(total, 2)
        self.assertEqual(photo["taken"], "2022-07-08T09:10:11")
        self.assertEqual(photo["dateSource"], "exif")
        self.assertEqual(photo["cameraMake"], "Xiaomi")
        self.assertEqual(photo["cameraModel"], "Test Camera")
        self.assertEqual((photo["width"], photo["height"]), (80, 60))
        self.assertEqual(self.database.refresh()["updated"], 0)

    def test_server_favorites_and_albums(self):
        items, _ = self.database.query(0, 10, "", "image")
        photo = items[0]
        self.database.set_favorite(photo["id"], True)
        favorites, total = self.database.query(0, 10, "", "all", favorite=True)
        self.assertEqual(total, 1)
        self.assertTrue(favorites[0]["favorite"])

        album = self.database.create_album("Лето")
        self.database.set_album_photo(album["id"], photo["id"], True)
        album_items, total = self.database.query(0, 10, "", "all", album=album["id"])
        self.assertEqual(total, 1)
        self.assertEqual(album_items[0]["albumIds"], [album["id"]])
        self.assertEqual(self.database.albums()[0]["count"], 1)

        video = next(item for item in self.database.query(0, 10, "", "video")[0])
        self.assertEqual(self.database.set_favorites([photo["id"], video["id"]], True), 2)
        self.assertEqual(self.database.set_album_photos(album["id"], [photo["id"], video["id"]], True), 2)

    def test_filters_duplicates_and_backup(self):
        items, total = self.database.query(0, 10, "", "image", filters={"camera": "Test Camera", "gps": "no"})
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["cameraModel"], "Test Camera")
        options = self.database.filter_options()
        self.assertEqual(options["cameras"][0]["value"], "Test Camera")

        (self.root / "holiday-copy.jpg").write_bytes((self.root / "holiday.jpg").read_bytes())
        self.database.refresh()
        self.database.enrich_hashes()
        groups = self.database.duplicate_groups("exact")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)

        backup = self.database.create_backup(2)
        self.assertTrue((self.database.backup_dir / backup["name"]).is_file())
        self.assertEqual(len(self.database.backup_status()["backups"]), 1)

    def test_gps_conversion(self):
        self.assertEqual(gps_coordinate((55, 45, 0), "N"), 55.75)
        self.assertEqual(gps_coordinate((37, 37, 0), "W"), -37.6166667)

    def test_missing_storage_marker_preserves_index(self):
        (self.root / ".photodom-storage").unlink()
        (self.root / "holiday.jpg").unlink()
        result = self.database.refresh()
        self.assertEqual(result["status"], "storage_unavailable")
        _, total = self.database.query(0, 10, "", "all")
        self.assertEqual(total, 2)
        self.assertFalse(self.database.stats()["storageAvailable"])

    def test_http_api_auth_favorites_and_albums(self):
        server = GalleryServer(("127.0.0.1", 0), GalleryHandler)
        server.app = argparse.Namespace(
            photo_root=self.root,
            cache_dir=Path(self.temporary.name) / "cache",
            username="photos",
            password="secret",
            thumbnail_slots=threading.Semaphore(1),
            database=self.database,
            export_dir=Path(self.temporary.name) / "exports",
            backup_retention=2,
        )
        server.app.export_dir.mkdir()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        authorization = "Basic " + base64.b64encode(b"photos:secret").decode()

        def request(method, path, body=None, authenticated=True):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["Authorization"] = authorization
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read()) if response.getheader("Content-Type", "").startswith("application/json") else None
            connection.close()
            return response.status, data

        try:
            status, _ = request("GET", "/api/stats", authenticated=False)
            self.assertEqual(status, 401)
            status, data = request("GET", "/health", authenticated=False)
            self.assertEqual((status, data["status"]), (200, "ok"))
            status, data = request("GET", "/api/photos?limit=10&type=image")
            self.assertEqual(status, 200)
            photo_id = data["items"][0]["id"]

            status, data = request("POST", f"/api/favorites/{photo_id}", {"favorite": True})
            self.assertEqual((status, data["favorite"]), (200, True))
            status, data = request("POST", "/api/albums", {"name": "Поездка"})
            self.assertEqual(status, 201)
            album_id = data["id"]
            status, data = request("POST", f"/api/albums/{album_id}/photos/{photo_id}", {"included": True})
            self.assertEqual((status, data["included"]), (200, True))
            status, data = request("GET", f"/api/photos?album={album_id}")
            self.assertEqual((status, data["total"]), (200, 1))
            status, data = request("GET", "/api/albums")
            self.assertEqual((status, data["albums"][0]["count"]), (200, 1))
            status, data = request("POST", "/api/favorites/bulk", {"photoIds": [photo_id], "favorite": True})
            self.assertEqual((status, data["count"]), (200, 1))
            status, data = request("POST", f"/api/albums/{album_id}/photos", {"photoIds": [photo_id], "included": True})
            self.assertEqual((status, data["count"]), (200, 1))
            status, data = request("GET", "/api/filters")
            self.assertEqual((status, data["cameras"][0]["value"]), (200, "Test Camera"))
            status, data = request("GET", "/api/duplicates?mode=exact")
            self.assertEqual((status, data["groups"]), (200, []))
            status, data = request("POST", "/api/backups", {})
            self.assertEqual(status, 201)
            status, _ = request("POST", "/api/download/zip", {"photoIds": [photo_id]})
            self.assertEqual(status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
