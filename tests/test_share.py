"""Read-only share link: signed, revocable, and limited to the plan and site."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "python"), str(ROOT / "python/app"), str(ROOT / "tests")]
import app as web
from test_slim_intake import slim_package


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_root = web.app.config.get("MENTOR_DATA_ROOT")
        web.app.config.update(TESTING=True, MENTOR_DATA_ROOT=Path(self.temp.name))
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.client = web.app.test_client()
        self.csrf = self.client.get("/mentor/api/session").json["csrf"]
        workspace = self.call("POST", "/mentor/api/workspaces", {"title": "Bio"}, expected=201)
        self.base = f"/mentor/api/workspaces/{workspace['id']}"

    def tearDown(self):
        self.environment.stop()
        if self.old_root is None:
            web.app.config.pop("MENTOR_DATA_ROOT", None)
        else:
            web.app.config["MENTOR_DATA_ROOT"] = self.old_root
        self.temp.cleanup()

    def call(self, method, path, data=None, expected=200):
        response = self.client.open(path, method=method, json=data, headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        return response.json

    def test_share_lifecycle(self):
        self.call("POST", self.base + "/share", {}, expected=404)  # no course yet
        workspace = self.call("GET", self.base)
        self.client.post(self.base + "/intake/export", data={
            "package": (io.BytesIO(slim_package()), "s.zip"), "revision": str(workspace["revision"])},
            content_type="multipart/form-data", headers={"X-Mentor-CSRF": self.csrf})
        self.assertEqual(self.call("GET", self.base + "/share"), {"enabled": False})
        share = self.call("POST", self.base + "/share", {})
        self.assertTrue(share["enabled"])
        self.assertEqual(share["days"], 90)
        path = share["url"].split("localhost", 1)[1]
        self.assertTrue(path.startswith("/mentor/shared/"))
        # Turning sharing on again keeps the same link.
        self.assertEqual(self.call("POST", self.base + "/share", {})["url"], share["url"])

        # A visitor without the owner's cookies can read it.
        visitor = web.app.test_client()
        index = visitor.get(path)
        self.assertEqual(index.status_code, 200)
        self.assertIn("read-only view", index.get_data(as_text=True))
        self.assertEqual(index.headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(index.headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'none'", index.headers["Content-Security-Policy"])
        self.assertEqual(visitor.get(path + "plan").status_code, 200)
        site = visitor.get(path + "site/index.html")
        self.assertEqual(site.status_code, 200)
        self.assertIn("<html", site.get_data(as_text=True).lower())
        self.assertEqual(visitor.get(path + "site/nope.html").status_code, 404)
        self.assertEqual(visitor.get(path + "consultation").status_code, 404)
        # The visitor cannot use the owner's API.
        self.assertNotEqual(visitor.get(self.base + "/plan").status_code, 200)

        # Tampered and revoked links fail.
        self.assertEqual(visitor.get(path.replace("/shared/", "/shared/x")).status_code, 404)
        self.assertEqual(self.call("DELETE", self.base + "/share"), {"enabled": False})
        self.assertEqual(visitor.get(path).status_code, 404)
        self.assertIn("turned off", visitor.get(path + "plan").get_data(as_text=True))
        # Turning it on again makes a new link; the old one stays dead.
        again = self.call("POST", self.base + "/share", {})
        self.assertNotEqual(again["url"], share["url"])
        self.assertEqual(visitor.get(path).status_code, 404)
        self.assertEqual(visitor.get(again["url"].split("localhost", 1)[1]).status_code, 200)


if __name__ == "__main__":
    unittest.main()
