"""
Tests for the dashboard's /api/people endpoints (baymax_app.py). Face models
are faked so this runs anywhere; real-model photo checks are in
test_face_models.py.

    python3 -m unittest test/test_people_api.py -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Baymax-main"))
import baymax_app  # noqa: E402
import ember_self  # noqa: E402
import face_id  # noqa: E402


def fake_embedding(image_bytes):
    """Deterministic 'embedding' per photo; b'bad' → rejected like a no-face photo."""
    if image_bytes.startswith(b"bad"):
        return None, "No face found. Use a clear, well-lit photo of their face."
    rng = np.random.default_rng(len(image_bytes))
    v = rng.normal(size=128).astype(np.float32)
    return v / np.linalg.norm(v), None


class PeopleApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        users = os.path.join(d, "users.json")
        with open(users, "w") as f:
            json.dump([{"id": "u1", "username": "sarah", "devices": [baymax_app.DEVICE_SERIAL]},
                       {"id": "u2", "username": "stranger", "devices": []}], f)
        ember_self.save_profile({"user_name": "Margaret",
                                 "household": [{"name": "Sarah", "relationship": "daughter"}]},
                                os.path.join(d, "profile.json"))
        self.patches = [
            mock.patch.object(baymax_app, "USERS_PATH", users),
            mock.patch.object(ember_self, "PROFILE_PATH", os.path.join(d, "profile.json")),
            mock.patch.object(baymax_app, "_people_store",
                              face_id.PeopleStore(os.path.join(d, "people.json"))),
            mock.patch.object(baymax_app, "_get_face_models", return_value=object()),
            mock.patch.object(face_id, "embeddings_from_photo",
                              side_effect=lambda models, data: fake_embedding(data)),
        ]
        for p in self.patches:
            p.start()
        self.client = baymax_app.app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def login(self, uid="u1"):
        with self.client.session_transaction() as s:
            s["user_id"] = uid

    def upload(self, name="Margaret", photos=(b"photo-one", b"photo-two!"), consent="yes", **extra):
        data = {"name": name, "photos": [(io.BytesIO(p), f"p{i}.jpg") for i, p in enumerate(photos)],
                **extra}
        if consent is not None:
            data["consent"] = consent
        return self.client.post("/api/people/photos", data=data, content_type="multipart/form-data")

    def test_requires_login_and_pairing(self):
        for call in (lambda: self.client.get("/api/people"),
                     lambda: self.upload(),
                     lambda: self.client.delete("/api/people/p_x")):
            self.assertEqual(call().status_code, 401)
        self.login("u2")
        self.assertEqual(self.client.get("/api/people").status_code, 403)
        self.assertEqual(self.upload().status_code, 403)

    def test_upload_list_delete(self):
        self.login()
        res = self.upload(photos=(b"photo-one", b"bad-photo", b"photo-three"))
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual([p["ok"] for p in body["photos"]], [True, False, True])
        self.assertIn("No face found", body["photos"][1]["reason"])
        people = self.client.get("/api/people").get_json()["people"]
        self.assertEqual(len(people), 1)
        p = people[0]
        self.assertEqual((p["name"], p["samples"], p["source"]), ("Margaret", 2, "app"))
        self.assertEqual(p["relationship"], "the person Ember cares for")
        self.assertNotIn("embeddings", p)
        # adding more photos of the same person adds samples, not a duplicate
        self.upload(name="margaret", photos=(b"another photo",))
        self.assertEqual(self.client.get("/api/people").get_json()["people"][0]["samples"], 3)
        # the robot's AI core sees the same file
        self.assertEqual(face_id.PeopleStore(baymax_app._people_store.path).people()[0]["name"],
                         "Margaret")
        res = self.client.delete(f"/api/people/{p['id']}")
        self.assertEqual(res.get_json()["people"], [])
        self.assertEqual(self.client.delete(f"/api/people/{p['id']}").status_code, 404)

    def test_relationship_from_profile_and_explicit(self):
        self.login()
        self.upload(name="Sarah")
        self.upload(name="Dave", relationship="neighbor")
        rel = {p["name"]: p["relationship"] for p in self.client.get("/api/people").get_json()["people"]}
        self.assertEqual(rel, {"Sarah": "daughter", "Dave": "neighbor"})

    def test_validation(self):
        self.login()
        self.assertIn("permission", self.upload(consent=None).get_json()["error"])
        self.assertIn("permission", self.upload(consent="no").get_json()["error"])
        self.assertEqual(self.upload(name="  ").status_code, 400)
        self.assertEqual(self.upload(photos=()).status_code, 400)
        self.assertEqual(self.upload(photos=[b"x%d" % i for i in range(11)]).status_code, 400)
        res = self.upload(photos=(b"bad1", b"bad2"))
        self.assertEqual(res.status_code, 422)
        self.assertEqual(self.client.get("/api/people").get_json()["people"], [])

    def test_too_large(self):
        self.login()
        with mock.patch.object(baymax_app, "MAX_UPLOAD_BYTES", 10):
            self.assertEqual(self.upload().status_code, 413)

    def test_models_unavailable(self):
        self.login()
        with mock.patch.object(baymax_app, "_get_face_models", side_effect=RuntimeError("offline")):
            res = self.upload()
        self.assertEqual(res.status_code, 503)
        self.assertIn("offline", res.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
