import argparse
from array import array
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

    def test_smart_catalog_incremental_analysis_and_search(self):
        progress = self.database.smart_progress()
        self.assertEqual((progress["total"], progress["ready"], progress["pending"]), (1, 0, 1))
        result = self.database.analyze_next(ocr_enabled=False)
        self.assertEqual(result["status"], "complete")
        progress = self.database.smart_progress()
        self.assertEqual((progress["ready"], progress["pending"]), (1, 0))

        matches, total = self.database.query(0, 10, "зеленое", "image")
        self.assertEqual(total, 1)
        self.assertIn("color:green", matches[0]["smartTags"])
        catalog = self.database.smart_catalog()
        self.assertEqual(catalog["progress"]["ready"], 1)

        Image.new("RGB", (240, 80), "#111111").save(self.root / "wide.jpg")
        self.database.refresh()
        self.assertEqual(self.database.smart_progress()["pending"], 1)
        self.database.analyze_next(ocr_enabled=False)
        panoramas, count = self.database.query(0, 10, "", "image", smart="panoramas")
        self.assertEqual(count, 1)
        self.assertEqual(panoramas[0]["smartCategory"], "panorama")

    def test_people_groups_rename_merge_and_split(self):
        Image.new("RGB", (50, 70), "#8d735f").save(self.root / "person-two.jpg")
        self.database.refresh()
        photos = self.database.query(0, 10, "", "image")[0]
        now = "2026-10-14T12:00:00+00:00"
        embedding = array("f", [0.0] * 127 + [1.0]).tobytes()
        with self.database.connect() as connection:
            first_person = connection.execute(
                "INSERT INTO people(name,created_at,updated_at) VALUES(NULL,?,?)", (now, now)
            ).lastrowid
            second_person = connection.execute(
                "INSERT INTO people(name,created_at,updated_at) VALUES(NULL,?,?)", (now, now)
            ).lastrowid
            first_face = connection.execute(
                "INSERT INTO faces(photo_id,person_id,model_version,x,y,width,height,confidence,embedding,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (photos[0]["id"], first_person, 1, 5, 5, 20, 20, 0.95, embedding, now),
            ).lastrowid
            second_face = connection.execute(
                "INSERT INTO faces(photo_id,person_id,model_version,x,y,width,height,confidence,embedding,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (photos[1]["id"], second_person, 1, 6, 6, 21, 21, 0.94, embedding, now),
            ).lastrowid
            shared_face = connection.execute(
                "INSERT INTO faces(photo_id,person_id,model_version,x,y,width,height,confidence,embedding,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (photos[0]["id"], second_person, 1, 30, 5, 18, 18, 0.93, embedding, now),
            ).lastrowid

        together, total = self.database.query(
            0, 10, "", "image", people=[first_person, second_person], people_mode="all"
        )
        self.assertEqual((len(together), total), (1, 1))
        either, total = self.database.query(
            0, 10, "", "image", people=[first_person, second_person], people_mode="any"
        )
        self.assertEqual((len(either), total), (2, 2))
        renamed = self.database.rename_person(first_person, "Артём")
        self.assertEqual(renamed["name"], "Артём")
        self.assertEqual(self.database.merge_people(second_person, first_person), 2)
        items, total = self.database.query(0, 10, "", "image", person=first_person)
        self.assertEqual((len(items), total), (2, 2))
        self.assertEqual(len(self.database.person_faces(first_person)), 3)
        new_person = self.database.move_face(shared_face, None)
        self.assertNotEqual(new_person, first_person)
        self.assertEqual(self.database.move_face(first_face, new_person), new_person)
        self.assertEqual(self.database.face_record(first_face)["person_id"], new_person)
        self.assertEqual(self.database.face_record(shared_face)["person_id"], new_person)
        self.assertEqual(self.database.face_record(second_face)["person_id"], first_person)

    def test_gps_conversion(self):
        self.assertEqual(gps_coordinate((55, 45, 0), "N"), 55.75)
        self.assertEqual(gps_coordinate((37, 37, 0), "W"), -37.6166667)

    def test_files_without_reliable_date_are_separated(self):
        Image.new("RGB", (40, 30), "#334455").save(self.root / "old-scan.jpg")
        self.database.refresh()
        undated, total = self.database.query(0, 10, "", "all", undated=True)
        self.assertEqual(total, 1)
        self.assertEqual(undated[0]["name"], "old-scan.jpg")
        self.assertEqual(undated[0]["dateSource"], "modified")
        self.assertEqual(self.database.undated_count(), 1)
        self.assertEqual(sum(entry["count"] for entry in self.database.timeline()), 2)

    def test_missing_storage_marker_preserves_index(self):
        (self.root / ".photodom-storage").unlink()
        (self.root / "holiday.jpg").unlink()
        result = self.database.refresh()
        self.assertEqual(result["status"], "storage_unavailable")
        _, total = self.database.query(0, 10, "", "all")
        self.assertEqual(total, 2)
        self.assertFalse(self.database.stats()["storageAvailable"])

    def test_users_sessions_and_last_admin_protection(self):
        self.assertTrue(self.database.bootstrap_admin("photos", "secret-password"))
        self.assertFalse(self.database.bootstrap_admin("ignored", "another-password"))
        self.assertIsNone(self.database.authenticate_user("photos", "wrong-password"))
        admin = self.database.authenticate_user("photos", "secret-password")
        self.assertEqual(admin["role"], "admin")
        session = self.database.create_session(admin["id"], 30)
        self.assertEqual(self.database.session_user(session["token"])["username"], "photos")
        viewer = self.database.create_user("guest", "viewer-password", "viewer")
        self.assertEqual(viewer["role"], "viewer")
        with self.assertRaisesRegex(ValueError, "последнего администратора"):
            self.database.update_user(admin["id"], role="viewer")
        self.database.delete_session(session["token"])
        self.assertIsNone(self.database.session_user(session["token"]))

    def test_http_api_auth_favorites_and_albums(self):
        self.database.bootstrap_admin("photos", "secret-password")
        server = GalleryServer(("127.0.0.1", 0), GalleryHandler)
        server.app = argparse.Namespace(
            photo_root=self.root,
            cache_dir=Path(self.temporary.name) / "cache",
            thumbnail_slots=threading.Semaphore(1),
            database=self.database,
            export_dir=Path(self.temporary.name) / "exports",
            backup_retention=2,
            session_days=30,
            cookie_secure=False,
            login_attempts={},
            login_lock=threading.Lock(),
        )
        server.app.export_dir.mkdir()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        session_cookie, csrf = "", ""

        def request(method, path, body=None, authenticated=True, csrf_protected=True, cookie_override=None, csrf_override=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": "application/json"}
            if authenticated:
                selected_cookie = session_cookie if cookie_override is None else cookie_override
                if selected_cookie:
                    headers["Cookie"] = selected_cookie
                selected_csrf = csrf if csrf_override is None else csrf_override
                if method not in {"GET", "HEAD"} and csrf_protected and selected_csrf:
                    headers["X-CSRF-Token"] = selected_csrf
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read()) if response.getheader("Content-Type", "").startswith("application/json") else None
            response_headers = dict(response.getheaders())
            connection.close()
            return response.status, data, response_headers

        try:
            status, _, _ = request("GET", "/api/stats", authenticated=False)
            self.assertEqual(status, 401)
            status, data, _ = request("GET", "/health", authenticated=False)
            self.assertEqual((status, data["status"]), (200, "ok"))
            status, data, headers = request("POST", "/api/auth/login", {"username": "photos", "password": "secret-password"}, authenticated=False)
            self.assertEqual(status, 200)
            self.assertIn("HttpOnly", headers["Set-Cookie"])
            self.assertIn("SameSite=Lax", headers["Set-Cookie"])
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            session_cookie = headers["Set-Cookie"].split(";", 1)[0]
            csrf = data["csrf"]
            status, data, _ = request("GET", "/api/session")
            self.assertEqual((status, data["user"]["role"]), (200, "admin"))
            status, _, headers = request("GET", "/static/vendor/leaflet/leaflet.js")
            self.assertEqual(status, 200)
            self.assertNotIn("unpkg.com", headers["Content-Security-Policy"])
            self.assertEqual(headers["Referrer-Policy"], "strict-origin-when-cross-origin")
            self.assertIn("https://tile.openstreetmap.org", headers["Content-Security-Policy"])
            self.assertNotIn("https://*.tile.openstreetmap.org", headers["Content-Security-Policy"])
            status, _, _ = request("GET", "/static/vendor/leaflet/images/marker-icon.png")
            self.assertEqual(status, 200)
            status, data, _ = request("GET", "/api/photos?limit=10&type=image")
            self.assertEqual(status, 200)
            photo_id = data["items"][0]["id"]
            status, data, _ = request("GET", "/api/photos?people_mode=invalid")
            self.assertEqual((status, data["error"]), (400, "Некорректный режим поиска людей"))
            status, data, _ = request("GET", "/api/timeline")
            self.assertEqual((status, data["undated"]), (200, 0))

            status, _, _ = request("POST", f"/api/favorites/{photo_id}", {"favorite": True}, csrf_protected=False)
            self.assertEqual(status, 403)
            status, data, _ = request("POST", f"/api/favorites/{photo_id}", {"favorite": True})
            self.assertEqual((status, data["favorite"]), (200, True))
            status, data, _ = request("POST", "/api/albums", {"name": "Поездка"})
            self.assertEqual(status, 201)
            album_id = data["id"]
            status, data, _ = request("POST", f"/api/albums/{album_id}/photos/{photo_id}", {"included": True})
            self.assertEqual((status, data["included"]), (200, True))
            status, data, _ = request("GET", f"/api/photos?album={album_id}")
            self.assertEqual((status, data["total"]), (200, 1))
            status, data, _ = request("GET", "/api/albums")
            self.assertEqual((status, data["albums"][0]["count"]), (200, 1))
            status, data, _ = request("POST", "/api/favorites/bulk", {"photoIds": [photo_id], "favorite": True})
            self.assertEqual((status, data["count"]), (200, 1))
            status, data, _ = request("POST", f"/api/albums/{album_id}/photos", {"photoIds": [photo_id], "included": True})
            self.assertEqual((status, data["count"]), (200, 1))
            status, data, _ = request("GET", "/api/filters")
            self.assertEqual((status, data["cameras"][0]["value"]), (200, "Test Camera"))
            status, data, _ = request("GET", "/api/smart/catalog")
            self.assertEqual(status, 200)
            self.assertIn("progress", data)
            self.assertIn("faces", data)
            status, data, _ = request("GET", "/api/people")
            self.assertEqual((status, data["people"]), (200, []))
            status, data, _ = request("POST", "/api/smart/reindex", {})
            self.assertEqual((status, data["queued"]), (200, 1))
            status, data, _ = request("POST", "/api/faces/reindex", {})
            self.assertEqual((status, data["queued"]), (200, 1))
            status, data, _ = request("GET", "/api/duplicates?mode=exact")
            self.assertEqual((status, data["groups"]), (200, []))
            status, data, _ = request("POST", "/api/backups", {})
            self.assertEqual(status, 201)
            status, _, _ = request("POST", "/api/download/zip", {"photoIds": [photo_id]})
            self.assertEqual(status, 200)

            status, _, _ = request("POST", "/api/users", {"username": "guest", "password": "viewer-password", "role": "viewer"})
            self.assertEqual(status, 201)
            status, viewer_data, viewer_headers = request("POST", "/api/auth/login", {"username": "guest", "password": "viewer-password"}, authenticated=False)
            viewer_cookie = viewer_headers["Set-Cookie"].split(";", 1)[0]
            status, _, _ = request("POST", f"/api/favorites/{photo_id}", {"favorite": False}, cookie_override=viewer_cookie, csrf_override=viewer_data["csrf"])
            self.assertEqual(status, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
