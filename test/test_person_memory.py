"""
Tests for Baymax-main/person_memory.py — conversation log, per-person
memories (in a real ChromaDB collection), appearance checks, and the
[ARRIVED] greeting. Gemini is replaced by scripted replies.

    python3 -m unittest test/test_person_memory.py -v
"""
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Baymax-main"))
import person_memory as pm  # noqa: E402

PROFILE = {"user_name": "Margaret", "about": "",
           "household": [{"name": "Sarah", "relationship": "daughter", "notes": ""},
                         {"name": "Tom Jones", "relationship": "son", "notes": ""}]}


class ChromaMemory:
    """Same interface as SemanticEmbedder (save / search / _collection), backed
    by a real ChromaDB collection; embeddings are a stable hash of the text
    because the robot's ONNX embedding model isn't available in tests."""

    def __init__(self, path):
        import chromadb
        self._client = chromadb.PersistentClient(path=path)
        self._collection = self._client.get_or_create_collection(
            "user_memories", metadata={"hnsw:space": "cosine"})

    @staticmethod
    def _vec(text):
        rng = np.random.default_rng(int(hashlib.md5(text.encode()).hexdigest()[:8], 16))
        v = rng.normal(size=32)
        return (v / np.linalg.norm(v)).tolist()

    def save(self, texts, ids=None, metadatas=None):
        texts = [texts] if isinstance(texts, str) else texts
        self._collection.add(documents=texts, ids=ids, metadatas=metadatas,
                             embeddings=[self._vec(t) for t in texts])

    def search(self, query, n_results=5, where=None):
        raw = self._collection.query(query_embeddings=[self._vec(query)], n_results=n_results,
                                     where=where)
        return [{"document": d, "distance": dist, "metadata": m} for d, dist, m in
                zip(raw["documents"][0], raw["distances"][0], raw["metadatas"][0])]


class Clock:
    def __init__(self, t=None):
        self.t = t or dt.datetime(2026, 10, 7, 15, 0).timestamp()   # Wednesday 3 PM

    def __call__(self):
        return self.t


# ─── Conversation log ────────────────────────────────────────────────────────
class ConversationLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "pending.jsonl")
        self.clock = Clock()
        self.log = pm.ConversationLog(self.path, clock=self.clock)

    def tearDown(self):
        self.dir.cleanup()

    def say(self, speaker, text, present=("Margaret",), dt_s=5):
        self.log.add(speaker, text, list(present))
        self.clock.t += dt_s

    def test_order_speakers_and_presence(self):
        self.say("user", "Hi Ember")
        self.say("gemini", "Hi Margaret!")
        self.say("user", "  ")                              # ignored
        turns = self.log.pending()
        self.assertEqual([(t["speaker"], t["text"]) for t in turns],
                         [("user", "Hi Ember"), ("ember", "Hi Margaret!")])
        self.assertEqual(turns[0]["present"], ["Margaret"])

    def test_survives_a_crash(self):
        self.say("user", "My hip hurts today")
        with open(self.path, "a") as f:
            f.write('{"t": 1, "speaker": "us')               # half-written line
        reborn = pm.ConversationLog(self.path, clock=self.clock)
        self.assertEqual([t["text"] for t in reborn.pending()], ["My hip hurts today"])

    def test_finished_only_after_quiet_gap(self):
        self.say("user", "Hello", dt_s=0)
        self.clock.t += pm.CONVERSATION_GAP - 1
        self.assertEqual(self.log.finished(), [])
        self.clock.t += 1
        self.assertEqual(len(self.log.finished()), 1)

    def test_mark_saved_keeps_newer_lines(self):
        self.say("user", "one")
        turns = self.log.pending()
        self.say("user", "two")                              # arrives while summarising
        self.log.mark_saved(turns)
        self.assertEqual([t["text"] for t in self.log.pending()], ["two"])
        self.assertEqual([t["text"] for t in pm.ConversationLog(self.path).pending()], ["two"])

    def test_transcript_notes_who_was_there(self):
        self.say("user", "Hi", present=["Margaret"])
        self.say("gemini", "Hello!", present=["Margaret"])
        self.say("user", "Mom, I'm here", present=["Margaret", "Sarah"])
        text = pm.format_transcript(self.log.pending())
        self.assertEqual(text.count("in front of Ember"), 2)
        self.assertIn("in front of Ember: Margaret, Sarah]", text)
        self.assertIn("Person: Mom, I'm here", text)
        self.assertIn("Ember: Hello!", text)


# ─── Summaries → per-person memories ─────────────────────────────────────────
class SummaryTests(unittest.TestCase):
    def turns(self):
        clock = Clock()
        log = pm.ConversationLog(os.path.join(tempfile.mkdtemp(), "p.jsonl"), clock=clock)
        for speaker, text, present in [
                ("user", "Hi Ember, it's Sarah", ["Margaret", "Sarah"]),
                ("gemini", "Hi Sarah!", ["Margaret", "Sarah"]),
                ("user", "I have a job interview on Friday", ["Margaret", "Sarah"])]:
            log.add(speaker, text, present)
            clock.t += 10
        return log.pending()

    def test_prompt_contents(self):
        prompt = pm.build_summary_prompt(self.turns(), PROFILE)
        self.assertIn("Margaret (the person Ember lives with and cares for)", prompt)
        self.assertIn("Sarah (daughter)", prompt)
        self.assertIn("Wednesday, October 7", prompt)
        self.assertIn("in front of Ember: Margaret, Sarah", prompt)
        self.assertIn("Person: I have a job interview on Friday", prompt)
        self.assertIn('"person"', prompt)

    def test_parse_json_fenced_and_names(self):
        reply = '```json\n{"memories": [' \
                '{"person": "sarah", "memory": "Sarah has a job interview on Friday."},' \
                '{"person": "Tom", "memory": "Tom is moving."},' \
                '{"person": "the user", "memory": "Likes tea."},' \
                '{"person": "Dave", "memory": "Dave is a neighbor."},' \
                '"junk", {"person": "Sarah", "memory": "  "}]}\n```'
        self.assertEqual(pm.parse_memories(reply, PROFILE), [
            {"person": "Sarah", "memory": "Sarah has a job interview on Friday."},
            {"person": "Tom Jones", "memory": "Tom is moving."},
            {"person": "", "memory": "Likes tea."},
            {"person": "Dave", "memory": "Dave is a neighbor."}])

    def test_parse_fallbacks(self):
        self.assertEqual(pm.parse_memories('{"memories": []}', PROFILE), [])
        self.assertEqual(pm.parse_memories("NOTHING_TO_REMEMBER", PROFILE), [])
        self.assertEqual(pm.parse_memories("- Likes tea\n- Has a cat", PROFILE),
                         [{"person": "", "memory": "Likes tea"},
                          {"person": "", "memory": "Has a cat"}])

    def test_summarise_calls_gemini_only_when_someone_spoke(self):
        calls = []
        def generate(prompt, image=None):
            calls.append(prompt)
            return '{"memories": [{"person": "Sarah", "memory": "Sarah has an interview."}]}'
        self.assertEqual(pm.summarise(self.turns(), PROFILE, generate),
                         [{"person": "Sarah", "memory": "Sarah has an interview."}])
        ember_only = [t for t in self.turns() if t["speaker"] == "ember"]
        self.assertEqual(pm.summarise(ember_only, PROFILE, generate), [])
        self.assertEqual(len(calls), 1)


class ChromaMemoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = ChromaMemory(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_save_and_recall_per_person_newest_first(self):
        t0 = time.time() - 86400 * 3
        pm.save_memories(self.db, [{"person": "Sarah", "memory": "Sarah adopted a dog."},
                                   {"person": "Margaret", "memory": "Margaret's hip hurts."}], when=t0)
        pm.save_memories(self.db, [{"person": "Sarah", "memory": "Sarah has an interview Friday."},
                                   {"person": "", "memory": "The garden needs weeding."}],
                         when=t0 + 86400)
        self.assertEqual(pm.memories_about(self.db, "sarah", PROFILE),
                         ["Sarah has an interview Friday.", "Sarah adopted a dog."])
        # unattributed memories count as being about the person Ember cares for
        self.assertEqual(pm.memories_about(self.db, "Margaret", PROFILE),
                         ["The garden needs weeding.", "Margaret's hip hurts."])
        self.assertEqual(pm.memories_about(self.db, "Tom Jones", PROFILE), [])
        self.assertEqual(len(pm.memories_about(self.db, "Sarah", PROFILE, limit=1)), 1)
        got = self.db._collection.get(include=["metadatas"])["metadatas"]
        self.assertTrue(all("person" in m and "time" in m for m in got))

    def test_memories_from_before_people_were_tracked(self):
        self.db.save(["Margaret was a teacher."], ids=["memory_20260101_120000_0"],
                     metadatas=[{"source": "conversation_summary", "timestamp": "20260101_120000",
                                 "line_index": "0"}])
        self.assertEqual(pm.memories_about(self.db, "Margaret", PROFILE), ["Margaret was a teacher."])
        self.assertEqual(pm.memories_about(self.db, "Sarah", PROFILE), [])

    def test_store_without_metadata_still_recalls(self):
        class Bare:
            class _collection:
                @staticmethod
                def get(include=None):
                    return {"documents": ["Likes tea."]}          # no metadatas key
        self.assertEqual(pm.memories_about(Bare(), "Margaret", PROFILE), ["Likes tea."])
        self.assertIn("About Margaret:\n- Likes tea.", pm.memory_block(Bare(), PROFILE))

    def test_memory_block_groups_by_person(self):
        pm.save_memories(self.db, [{"person": "Sarah", "memory": "Sarah has an interview."},
                                   {"person": "", "memory": "Likes tea."}])
        block = pm.memory_block(self.db, PROFILE)
        self.assertIn("About Margaret:\n- Likes tea.", block)
        self.assertIn("About Sarah:\n- Sarah has an interview.", block)
        self.assertLess(block.index("About Margaret"), block.index("About Sarah"))
        self.assertEqual(pm.memory_block(ChromaMemory(tempfile.mkdtemp()), PROFILE), "")

    def test_memory_block_keeps_newest(self):
        base = time.time() - 1000
        for i in range(60):
            pm.save_memories(self.db, [{"person": "", "memory": f"fact {i}"}], when=base + i)
        block = pm.memory_block(self.db, PROFILE, limit=50)
        self.assertIn("fact 59", block)
        self.assertNotIn("fact 9\n", block + "\n")       # the 10 oldest are left out
        self.assertEqual(block.count("\n- "), 50)


# ─── Appearance ──────────────────────────────────────────────────────────────
class AppearanceTests(unittest.TestCase):
    def reply(self, obj):
        def generate(prompt, image=None):
            self.prompt, self.image = prompt, image
            if isinstance(obj, Exception):
                raise obj
            return obj if isinstance(obj, str) else json.dumps(obj)
        return generate

    def test_first_time_just_describes(self):
        r = pm.check_appearance(self.reply({"description": "Long brown hair, glasses",
                                            "changed": True, "change": "x"}), b"jpg")
        self.assertEqual(r, {"description": "Long brown hair, glasses", "changed": False, "change": ""})
        self.assertEqual(self.image, b"jpg")
        self.assertIn("Never mention body shape, weight, skin, age", self.prompt)
        self.assertNotIn("last time", self.prompt)

    def test_compares_with_last_time(self):
        r = pm.check_appearance(self.reply({"description": "Short brown hair, glasses",
                                            "changed": True, "change": "shorter hair"}),
                                b"jpg", previous="Long brown hair, glasses")
        self.assertEqual(r["change"], "shorter hair")
        self.assertTrue(r["changed"])
        self.assertIn('How they looked last time: "Long brown hair, glasses"', self.prompt)
        self.assertIn("Ignore lighting, camera angle", self.prompt)

    def test_unconvincing_or_broken_replies(self):
        self.assertFalse(pm.check_appearance(self.reply({"description": "d", "changed": True, "change": ""}),
                                             b"j", previous="p")["changed"])
        self.assertFalse(pm.check_appearance(self.reply({"description": "d", "changed": "yes", "change": "c"}),
                                             b"j", previous="p")["changed"])
        self.assertIsNone(pm.check_appearance(self.reply({"description": ""}), b"j"))
        self.assertIsNone(pm.check_appearance(self.reply("not json"), b"j"))
        self.assertIsNone(pm.check_appearance(self.reply(RuntimeError("offline")), b"j"))


# ─── Greeting ────────────────────────────────────────────────────────────────
class ArrivalPromptTests(unittest.TestCase):
    def test_describe_away(self):
        now = time.time()
        self.assertEqual(pm.describe_away(25 * 60), "about 25 minutes ago")
        self.assertEqual(pm.describe_away(3 * 3600), "about 3 hours ago")
        self.assertEqual(pm.describe_away(26 * 3600, now - 26 * 3600), "yesterday")
        three = now - 3 * 86400
        self.assertEqual(pm.describe_away(3 * 86400, three),
                         f"3 days ago ({dt.datetime.fromtimestamp(three).strftime('%A')})")
        self.assertEqual(pm.describe_away(30 * 86400, now - 30 * 86400), "about 30 days ago")
        self.assertIsNone(pm.describe_away(None))

    def test_full_greeting(self):
        p = pm.arrival_prompt("Sarah (Margaret's daughter)", "Sarah", 2 * 86400, time.time() - 2 * 86400,
                              ["Sarah has a job interview on Friday."],
                              {"changed": True, "change": "shorter hair"})
        self.assertTrue(p.startswith("[ARRIVED] Sarah (Margaret's daughter) just came into view."))
        self.assertIn("You last saw Sarah 2 days ago", p)
        self.assertIn("- Sarah has a job interview on Friday.", p)
        self.assertIn("Sarah looks a little different from last time: shorter hair.", p)
        self.assertIn("Did you get a haircut?", p)
        self.assertIn("Never comment on weight, body, skin or age", p)
        self.assertIn("Greet Sarah warmly by name", p)

    def test_plain_greeting_has_no_appearance_talk(self):
        p = pm.arrival_prompt("Margaret", "Margaret", None, None, [], {"changed": False},
                              is_primary=True)
        self.assertIn("first time you've seen Margaret since you learned their face", p)
        self.assertIn("Welcome Margaret back warmly by name", p)
        self.assertNotIn("haircut", p)
        self.assertNotIn("remember about", p)


if __name__ == "__main__":
    unittest.main()
