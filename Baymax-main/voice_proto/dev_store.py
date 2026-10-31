"""A minimal stand-in for face_id.PeopleStore, for trying voice_id.py on
Windows (face_id.py needs fcntl, which only exists on Linux/Mac).

Same methods voice_id uses, same JSON format, but NO file locking — for
development only. On the robot, use face_id.PeopleStore.
"""
import json
import os


class DevPeopleStore:
    def __init__(self, path):
        self.path = path
        self._data = self._read()

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {"version": 1, "people": []}

    def reload_if_changed(self, force=False):
        self._data = self._read()
        return True

    def people(self):
        return [dict(p) for p in self._data["people"]]

    def get(self, person_id):
        return next((dict(p) for p in self._data["people"] if p["id"] == person_id), None)

    def find_by_name(self, name):
        key = " ".join(str(name or "").lower().split())
        return next((dict(p) for p in self._data["people"]
                     if " ".join(p["name"].lower().split()) == key), None)

    def _update(self, change):
        data = self._read()
        result = change(data)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)
        self._data = data
        return result

    def remove(self, person_id):
        def change(data):
            before = len(data["people"])
            data["people"] = [p for p in data["people"] if p["id"] != person_id]
            return len(data["people"]) < before
        return self._update(change)
