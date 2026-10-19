import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from features.person_recognition.diagnose_identity import diagnose, memory_summary, profile_summary, running_cores, source_summary


class IdentityDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_source_counts_only_active_literals_not_comments(self):
        source = self.root / "realtime_gemini_10.py"
        source.write_text('# James is an example in a comment\nPROMPT="neutral"\n', encoding="utf-8")
        self.assertEqual(source_summary(source, "James")["probe_in_source_string_count"], 0)

    def test_source_flags_unfiltered_startup_read(self):
        source = self.root / "realtime_gemini_10.py"
        source.write_text('def _build_config():\n    return store._collection.get(limit=50)\n', encoding="utf-8")
        self.assertTrue(source_summary(source, "James")["startup_unfiltered_collection_get_present"])
        source.write_text('def _build_config():\n    return store._collection.get(where={"person_id":"p1"})\n', encoding="utf-8")
        self.assertFalse(source_summary(source, "James")["startup_unfiltered_collection_get_present"])

    def test_profile_reports_match_without_exposing_other_values(self):
        path = self.root / "ember_profile.json"
        path.write_text(json.dumps({"user_name": "James", "about": "private detail", "secret": "private token"}))
        result = profile_summary(path, "James")
        self.assertTrue(result["primary_name_matches_probe"])
        self.assertNotIn("private", json.dumps(result))

    def test_people_summary_never_returns_face_embeddings(self):
        path = self.root / "ember_people.json"
        path.write_text(json.dumps({"people": [{"name": "James", "embeddings": [[123.456]]}]}))
        result = profile_summary(path, "James")
        self.assertEqual(result["stored_people_matching_probe"], 1)
        self.assertNotIn("embeddings", result)
        self.assertNotIn("123.456", json.dumps(result))

    def test_missing_and_invalid_profiles_do_not_modify_files(self):
        path = self.root / "ember_profile.json"
        self.assertFalse(profile_summary(path, "James")["present"])
        path.write_bytes(b"invalid")
        self.assertIn("error", profile_summary(path, "James"))
        self.assertEqual(path.read_bytes(), b"invalid")

    def test_sqlite_counts_without_returning_content_or_creating_sidecars(self):
        path = self.root / "chroma.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript('CREATE TABLE embeddings(id INTEGER); CREATE TABLE embedding_metadata(id INTEGER, key TEXT, string_value TEXT);')
        connection.executemany("INSERT INTO embeddings VALUES(?)", [(1,), (2,)])
        connection.executemany("INSERT INTO embedding_metadata VALUES(?,?,?)", [(1, "chroma:document", "James likes a private topic"), (1, "people", "James"), (2, "person_id", "p_test")])
        connection.commit()
        connection.close()
        before, files = path.read_bytes(), sorted(p.name for p in self.root.iterdir())
        result = memory_summary(path, "James")
        self.assertEqual(result["embedding_records"], 2)
        self.assertEqual(result["probe_substring_records"], 1)
        self.assertEqual(result["person_id_tagged_records"], 1)
        self.assertNotIn("private topic", json.dumps(result))
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(files, sorted(p.name for p in self.root.iterdir()))

    def test_sql_wildcards_are_literal(self):
        path = self.root / "chroma.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute('CREATE TABLE embedding_metadata(id INTEGER, key TEXT, string_value TEXT)')
        connection.execute('INSERT INTO embedding_metadata VALUES(1,"chroma:document","James")')
        connection.commit()
        connection.close()
        self.assertEqual(memory_summary(path, "%")["probe_substring_records"], 0)

    def test_unsupported_schema_and_missing_database_are_safe(self):
        path = self.root / "missing.sqlite3"
        self.assertFalse(memory_summary(path, "James")["present"])
        connection = sqlite3.connect(path)
        connection.close()
        self.assertFalse(memory_summary(path, "James")["metadata_schema_supported"])

    def test_proc_detection_redacts_command_arguments(self):
        process = self.root / "123"
        process.mkdir()
        (process / "cmdline").write_bytes(b"/usr/bin/python3\0realtime_gemini_10.py\0--secret=private-token\0")
        # A real cwd link isn't needed for a deterministic fake /proc tree.
        (process / "cwd").mkdir()
        result = running_cores(self.root)
        self.assertEqual(result[0]["runtime"], "realtime_gemini_10.py")
        self.assertNotIn("private-token", json.dumps(result))

    def test_full_report_does_not_import_or_execute_runtime(self):
        (self.root / "realtime_gemini_10.py").write_text('raise RuntimeError("never execute this")\n')
        report = diagnose(self.root, self.root, processes=[])
        self.assertTrue(report["runtime_sources"][2]["available"])
        self.assertFalse((self.root / "chroma_store").exists())


if __name__ == "__main__":
    unittest.main()
