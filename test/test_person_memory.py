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

    def search(self, query, n_results=5, where=None):
        raw = self._collection.query(query_embeddings=[self._vec(query)], n_results=n_results,
                                     where=where)
        return [{"id": i, "document": d, "distance": dist, "metadata": m} for i, d, dist, m in
                zip(raw["ids"][0], raw["documents"][0], raw["distances"][0], raw["metadatas"][0])]

    def save(self, texts, ids=None, metadatas=None):          # upsert, like SemanticEmbedder
        texts = [texts] if isinstance(texts, str) else texts
        self._collection.upsert(documents=texts, ids=ids, metadatas=metadatas,
                                embeddings=[self._vec(t) for t in texts])


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

    def test_separate_conversations_are_summarised_separately(self):
        # Yesterday's unsaved lines (e.g. recovered after a power cut) and
        # today's must not become one conversation dated yesterday.
        self.say("user", "yesterday one")
        self.say("user", "yesterday two")
        self.clock.t += 86400
        self.say("user", "today one", dt_s=0)
        first = self.log.finished()
        self.assertEqual([t["text"] for t in first], ["yesterday one", "yesterday two"])
        self.log.mark_saved(first)
        self.assertEqual(self.log.finished(), [])            # today's is still going
        self.assertEqual([t["text"] for t in self.log.finished(ending=True)], ["today one"])

    def test_forget_before_saving_survives_a_restart(self):
        self.say("user", "I'm Sarah", present=["Margaret", "Sarah"])
        self.log.forget("Sarah")
        self.assertEqual(self.log.pending()[0]["present"], ["Margaret"])
        self.say("gemini", "Of course, Sarah.", present=["Margaret"])
        reborn = pm.ConversationLog(self.path, clock=self.clock)
        self.assertEqual(reborn.forgotten_for(reborn.pending()), {"Sarah"})
        self.assertEqual([t["text"] for t in reborn.pending()], ["I'm Sarah", "Of course, Sarah."])
        reborn.mark_saved(reborn.pending())
        self.assertEqual(pm.ConversationLog(self.path).forgotten, {})       # cleared once saved

    def test_clock_set_back_splits_conversations(self):
        self.say("user", "before")
        self.clock.t -= 2 * 3600
        self.say("user", "after one")
        self.say("user", "after two")
        self.assertEqual([t["text"] for t in self.log.finished()], ["before"])

    def test_line_after_a_crash_is_not_glued_to_a_broken_one(self):
        self.say("user", "old")
        with open(self.path, "a") as f:
            f.write('{"t": 1, "speaker": "us')                 # half-written by a power cut
        log = pm.ConversationLog(self.path, clock=self.clock)
        log.add("user", "first new line")
        log.add("user", "second new line")
        self.assertEqual([t["text"] for t in pm.ConversationLog(self.path).pending()],
                         ["old", "first new line", "second new line"])

    def test_forget_request_survives_a_clock_set_back(self):
        self.say("user", "conversation A")                    # pending, not saved yet
        self.clock.t -= 2 * 3600                             # the clock is corrected backwards
        self.say("user", "I'm Sarah", present=["Sarah"])
        self.log.forget("Sarah")
        self.say("gemini", "Of course.")
        first = self.log.finished()
        self.assertEqual([t["text"] for t in first], ["conversation A"])
        self.log.mark_saved(first)
        second = self.log.finished(ending=True)
        self.assertEqual(self.log.forgotten_for(second), {"Sarah"})

    def test_forget_request_waits_for_its_conversation_even_after_a_day(self):
        self.say("user", "conversation A")
        self.clock.t += pm.CONVERSATION_GAP + 10
        self.say("user", "I'm Sarah, conversation B")
        self.log.forget("Sarah")
        self.clock.t += 2 * 86400                            # robot off for two days
        first = self.log.finished()
        self.log.mark_saved(first)                           # saving A
        self.assertEqual(self.log.forgotten_for(self.log.finished()), {"Sarah"})

    def test_forget_me_as_the_first_thing_said(self):
        # The tool runs before their line is logged, with nothing pending yet
        self.log.forget("Sarah")
        self.clock.t += 2
        self.say("user", "Ember, please forget me", present=["Sarah"])
        self.assertEqual(self.log.forgotten_for(self.log.pending()), {"Sarah"})

    def test_forget_applies_to_its_conversation_not_later_ones(self):
        self.say("user", "I'm Sarah")
        self.log.forget("Sarah")
        self.clock.t += pm.CONVERSATION_GAP + 5
        self.say("user", "hello again", dt_s=0)              # next conversation, unsaved
        first = self.log.finished()
        self.assertEqual(self.log.forgotten_for(first), {"Sarah"})
        self.log.mark_saved(first)
        self.clock.t += 3600                                  # much later, Sarah is back and agrees
        self.say("user", "Sarah says hi")
        self.assertEqual(self.log.forgotten_for(self.log.pending()[-1:]), set())

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


# ─── Names ───────────────────────────────────────────────────────────────────
class NameTests(unittest.TestCase):
    def test_canonical_names(self):
        known = pm.known_names(PROFILE, ["Dave", "sarah"])
        self.assertEqual(known, ["Margaret", "Sarah", "Tom Jones", "Dave"])
        c = lambda n: pm.canonical_name(n, known)
        self.assertEqual([c("sarah"), c("Tom"), c("tom jones"), c("Dave"), c("Frank")],
                         ["Sarah", "Tom Jones", "Tom Jones", "Dave", "Frank"])
        for generic in ("the user", "User", "someone", "Ember", "ember", "", None, "everyone"):
            self.assertEqual(c(generic), "", generic)
        both = pm.known_names({"user_name": "Tom Smith", "household": [{"name": "Tom Jones"}]})
        self.assertEqual(pm.canonical_name("Tom", both), "Tom")       # ambiguous: keep as said

    def test_strict_names_for_forgetting_are_exact(self):
        known = ["Margaret Jones", "Sarah", "Aunt Mary"]
        strict = lambda n: pm.canonical_name(n, known, strict=True)
        self.assertEqual([strict("margaret jones"), strict("SARAH")], ["Margaret Jones", "Sarah"])
        for said in ("Margaret", "Sarah Smith", "Aunt Sue"):     # never guessed
            self.assertEqual(strict(said), said)
        self.assertEqual(pm.canonical_name("Aunt Sue", known), "Aunt Sue")   # not "Aunt Mary"


# ─── Summaries → memories linked to people ───────────────────────────────────
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
        prompt = pm.build_summary_prompt(self.turns(), PROFILE, extra_names=["Dave"])
        for expected in ("Margaret (the configured primary profile, not necessarily the current speaker)", "Sarah (daughter)",
                         "Dave", "Wednesday, October 7", "in front of Ember: Margaret, Sarah",
                         "Person: I have a job interview on Friday", "EVERYONE it involves",
                         "never make separate copies", "even people who weren't there",
                         '"kind": "conversation"',
                         '"people": ["<name>", ...]'):
            self.assertIn(expected, prompt)

    def test_parse_people_lists(self):
        reply = '```json\n{"memories": [' \
                '{"memory": "Sarah told Margaret about her job interview on Friday.", ' \
                '"people": ["sarah", "Margaret", "Sarah", "Ember", "the user"]},' \
                '{"memory": "Margaret is worried about Tom\'s surgery.", "people": ["Margaret", "Tom"]},' \
                '{"memory": "On Wednesday, Sarah and Margaret talked with Ember about the interview.", ' \
                '"people": ["Sarah", "Margaret"], "kind": "conversation"},' \
                '{"memory": "It rained all day.", "people": []},' \
                '{"memory": "Dave fixed the fence.", "person": "dave"},' \
                '{"memory": "Odd", "people": "Sarah"}, {"memory": "x", "people": 5},' \
                '"junk", {"memory": "  ", "people": ["Sarah"]}]}\n```'
        F = "fact"
        self.assertEqual(pm.parse_memories(reply, PROFILE, extra_names=["Dave"]), [
            {"memory": "Sarah told Margaret about her job interview on Friday.",
             "people": ["Sarah", "Margaret"], "kind": F},
            {"memory": "Margaret is worried about Tom's surgery.", "people": ["Margaret", "Tom Jones"], "kind": F},
            {"memory": "On Wednesday, Sarah and Margaret talked with Ember about the interview.",
             "people": ["Sarah", "Margaret"], "kind": "conversation"},
            {"memory": "It rained all day.", "people": [], "kind": F},
            {"memory": "Dave fixed the fence.", "people": ["Dave"], "kind": F},
            {"memory": "Odd", "people": ["Sarah"], "kind": F},
            {"memory": "x", "people": [], "kind": F}])

    def test_summary_leaves_out_forgotten_people(self):
        reply = json.dumps({"memories": [
            {"memory": "Sarah has an interview on Friday.", "people": ["Sarah"]},
            {"memory": "Margaret told Ember that Sarah moved.", "people": ["Margaret"]},
            {"memory": "Margaret likes tea.", "people": ["Margaret"]}]})
        prompts = []
        def generate(prompt):
            prompts.append(prompt)
            return reply
        items = pm.summarise(self.turns(), PROFILE, generate, forgotten={"Sarah"})
        self.assertEqual([m["memory"] for m in items], ["Margaret likes tea."])
        self.assertIn("asked to be forgotten", prompts[0])

    def test_summary_forgetting_spares_people_sharing_a_first_name(self):
        profile = {"user_name": "Margaret Jones", "household": []}
        reply = json.dumps({"memories": [
            {"memory": "Margaret has a doctor's appointment on Friday.", "people": ["Margaret Jones"]},
            {"memory": "Margaret Lee moved to Ohio.", "people": ["Margaret Lee"]},
            {"memory": "Margaret wants to plant tulips in the garden.", "people": ["Margaret Jones"]}]})
        items = pm.summarise(self.turns(), profile, lambda p: reply, extra_names=["Margaret Lee"],
                             forgotten={"Margaret Lee"})
        self.assertEqual([m["memory"] for m in items],
                         ["Margaret has a doctor's appointment on Friday.",
                          "Margaret wants to plant tulips in the garden."])
        items = pm.summarise(self.turns(), profile, lambda p: reply, forgotten={"the visitor"})
        self.assertEqual(len(items), 3)

    def test_parse_other_shapes(self):
        one = [{"memory": "Sarah got the job.", "people": ["Sarah"]}]
        self.assertEqual([m["memory"] for m in pm.parse_memories(json.dumps(one), PROFILE)],
                         ["Sarah got the job."])
        self.assertEqual([m["memory"] for m in pm.parse_memories(json.dumps(one * 2), PROFILE)],
                         ["Sarah got the job."] * 2)
        cut_off = '{"memories": [\n  {"memory": "Sarah got the job at the bank.",\n   "people": ["Sar'
        with self.assertRaises(ValueError):                  # retried, never stored as fragments
            pm.parse_memories(cut_off, PROFILE)
        as_dicts = '{"memories": [{"memory": "Sarah got the job.", "people": [{"name": "Sarah"}]}]}'
        self.assertEqual([m["memory"] for m in pm.parse_memories('["Margaret likes tea", "Sarah got a dog"]', PROFILE)],
                         ["Margaret likes tea", "Sarah got a dog"])
        for nothing in ("null", '"NOTHING_TO_REMEMBER"', "0", '""'):
            self.assertEqual(pm.parse_memories(nothing, PROFILE), [], nothing)
        self.assertEqual(pm.parse_memories(as_dicts, PROFILE)[0]["people"], ["Sarah"])

    def test_parse_null_memories_is_nothing_not_an_error(self):
        for reply in ('{"memories": null}', '{"memories": "none"}', '{"memories": {}}'):
            self.assertEqual(pm.parse_memories(reply, PROFILE), [], reply)

    def test_parse_fallbacks(self):
        self.assertEqual(pm.parse_memories('{"memories": []}', PROFILE), [])
        self.assertEqual(pm.parse_memories("NOTHING_TO_REMEMBER", PROFILE), [])
        self.assertEqual(pm.parse_memories("- Likes tea\n- Has a cat", PROFILE),
                         [{"memory": "Likes tea", "people": [], "kind": "fact"},
                          {"memory": "Has a cat", "people": [], "kind": "fact"}])

    def test_summarise_only_when_someone_spoke(self):
        calls = []
        def generate(prompt, image=None):
            calls.append(prompt)
            return '{"memories": [{"memory": "Sarah has an interview.", "people": ["Sarah"]}]}'
        self.assertEqual(pm.summarise(self.turns(), PROFILE, generate),
                         [{"memory": "Sarah has an interview.", "people": ["Sarah"], "kind": "fact"}])
        self.assertEqual(pm.summarise([t for t in self.turns() if t["speaker"] == "ember"],
                                      PROFILE, generate), [])
        self.assertEqual(len(calls), 1)


class StoreTests(unittest.TestCase):
    """Saving, recall, prompt block and forgetting, in a real ChromaDB."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = ChromaMemory(self.dir.name)
        self.now = time.time()

    def tearDown(self):
        self.dir.cleanup()

    def save(self, memory, people, days_ago=0):
        return pm.save_memories(self.db, [{"memory": memory, "people": people}],
                                when=self.now - days_ago * 86400)

    def recall(self, name, **kw):
        return [m["memory"] for m in pm.memories_involving(self.db, name, PROFILE, **kw)]

    def test_shared_memory_recalled_for_everyone_involved(self):
        self.save("Sarah told Margaret about her job interview on Friday.", ["Sarah", "Margaret"], 2)
        self.save("Margaret is worried about Tom's knee surgery.", ["Margaret", "Tom Jones"], 1)
        self.save("Sarah adopted a dog.", ["Sarah"], 3)
        self.assertEqual(self.db._collection.count(), 3)                 # stored once each
        self.assertEqual(self.recall("Sarah"), ["Sarah told Margaret about her job interview on Friday.",
                                                "Sarah adopted a dog."])
        self.assertEqual(self.recall("margaret"), ["Margaret is worried about Tom's knee surgery.",
                                                   "Sarah told Margaret about her job interview on Friday."])
        self.assertEqual(self.recall("Tom Jones"), ["Margaret is worried about Tom's knee surgery."])
        self.assertEqual(self.recall("Dave"), [])
        self.assertEqual(len(self.recall("Sarah", limit=1)), 1)
        meta = self.db._collection.get(include=["metadatas"])["metadatas"]
        self.assertTrue(all(set(m) >= {"people", "time", "timestamp", "mentions", "kind"} for m in meta))

    def test_kind_is_stored_and_survives_replace_and_forget(self):
        pm.save_memories(self.db, [{"memory": "On Monday, Sarah and Margaret talked with Ember about tea.",
                                    "people": ["Sarah", "Margaret"], "kind": "conversation"}])
        pm.save_memories(self.db, [{"memory": "On Monday, Sarah and Margaret talked with Ember about tea.",
                                    "people": ["Sarah", "Margaret"], "kind": "conversation"}])
        meta = self.db._collection.get(include=["metadatas"])["metadatas"]
        self.assertEqual([(m["kind"], m["mentions"]) for m in meta], [("conversation", 2)])
        pm.forget_person(self.db, "Sarah", PROFILE, lambda p: "On Monday, Margaret talked with Ember about tea.")
        self.assertEqual(self.db._collection.get(include=["metadatas"])["metadatas"][0]["kind"], "conversation")
        self.assertEqual(pm.memories_involving(self.db, "Margaret", PROFILE)[0]["kind"], "conversation")

    def test_older_formats(self):
        self.db.save(["Margaret was a teacher."], ids=["old_1"],
                     metadatas=[{"source": "conversation_summary", "timestamp": "20260101_120000",
                                 "line_index": "0"}])
        self.db.save(["Sarah likes jazz."], ids=["old_2"],
                     metadatas=[{"source": "conversation_summary", "timestamp": "20260102_120000",
                                 "person": "Sarah", "time": 1767355200.0}])
        self.assertEqual(self.recall("Margaret"), ["Margaret was a teacher."])
        self.assertEqual(self.recall("Sarah"), ["Sarah likes jazz."])

    def test_store_without_metadata_still_recalls(self):
        class Bare:
            class _collection:
                @staticmethod
                def get(include=None):
                    return {"documents": ["Likes tea."]}           # no ids/metadatas keys
        self.assertEqual([m["memory"] for m in pm.memories_involving(Bare(), "Margaret", PROFILE)],
                         ["Likes tea."])
        self.assertIn("- Likes tea.", pm.memory_block(Bare(), PROFILE))

    def test_repeat_replaces_instead_of_piling_up(self):
        self.save("Margaret's hip has been hurting.", ["Margaret"], days_ago=5)
        added, replaced = self.save("Margaret's hip has been hurting.", ["Margaret"])
        self.assertEqual((added, replaced), (0, 1))
        got = self.db._collection.get(include=["metadatas", "documents"])
        self.assertEqual(len(got["ids"]), 1)
        self.assertEqual(got["metadatas"][0]["mentions"], 2)
        self.assertAlmostEqual(got["metadatas"][0]["time"], self.now, delta=1)   # moved to now
        # same words, different people → a different memory
        self.assertEqual(self.save("Margaret's hip has been hurting.", ["Margaret", "Sarah"]), (1, 0))
        # a repeat inside one batch is caught too
        added, replaced = pm.save_memories(self.db, [{"memory": "Milo is a cat.", "people": []}] * 2)
        self.assertEqual((added, replaced), (1, 1))

    def test_memory_block_lists_each_memory_once_with_when_and_who(self):
        self.save("Sarah told Margaret about her interview.", ["Sarah", "Margaret"])
        self.save("Margaret likes tea.", ["Margaret"], days_ago=1)
        block = pm.memory_block(self.db, PROFILE, now=self.now)
        self.assertEqual(block.count("Sarah told Margaret about her interview."), 1)
        self.assertIn("- Sarah told Margaret about her interview. (today; Sarah, Margaret)", block)
        self.assertIn("- Margaret likes tea. (yesterday; Margaret)", block)
        self.assertLess(block.index("interview"), block.index("tea"))     # newest first
        self.assertEqual(pm.memory_block(ChromaMemory(tempfile.mkdtemp()), PROFILE), "")

    def test_memory_block_keeps_newest(self):
        for i in range(60):
            pm.save_memories(self.db, [{"memory": f"Fact number {i} is true.", "people": []}],
                             when=self.now - 1000 + i)
        block = pm.memory_block(self.db, PROFILE, limit=50)
        self.assertIn("Fact number 59 is", block)
        self.assertNotIn("Fact number 9 is", block)
        self.assertEqual(block.count("\n- "), 50)

    def test_memory_block_always_includes_whoever_is_here(self):
        self.save("Ruth's grandson plays the violin.", ["Ruth"], days_ago=90)
        for i in range(55):
            self.save(f"Margaret did crossword number {i}.", ["Margaret"], days_ago=i / 100)
        self.assertNotIn("violin", pm.memory_block(self.db, PROFILE, limit=50))
        block = pm.memory_block(self.db, PROFILE, limit=50, focus=["Ruth"])
        self.assertIn("- Ruth's grandson plays the violin.", block)
        self.assertEqual(block.count("\n- "), 51)

    def test_memory_line_from_someones_point_of_view(self):
        m = {"memory": "Sarah told Margaret about her interview.", "people": ["Sarah", "Margaret"],
             "time": self.now - 2 * 86400}
        when = dt.datetime.fromtimestamp(m["time"]).strftime("%A")
        self.assertEqual(pm.memory_line(m, self.now, about="sarah"),
                         f"- Sarah told Margaret about her interview. ({when}; with Margaret)")
        self.assertEqual(pm.memory_line({"memory": "x", "people": ["Sarah"], "time": 0}, about="Sarah"),
                         "- x")

    def test_forget_person_everywhere(self):
        self.save("Sarah adopted a dog.", ["Sarah"])
        self.save("Sarah told Margaret about her job interview.", ["Sarah", "Margaret"])
        self.save("Margaret likes tea.", ["Margaret"])
        self.db.save(["Margaret said Sarah calls every Sunday."], ids=["legacy"],
                     metadatas=[{"timestamp": "20260101_120000"}])          # old, unlinked
        prompts = []
        def rewrite(prompt):
            prompts.append(prompt)
            if "job interview" in prompt:
                return "Margaret heard some exciting news from family."
            return "Margaret enjoys weekly calls."
        result = pm.forget_person(self.db, "sarah", PROFILE, rewrite)
        self.assertEqual(result, {"deleted": 1, "rewritten": 2})
        docs = sorted(self.db._collection.get()["documents"])
        self.assertEqual(docs, ["Margaret enjoys weekly calls.",
                                "Margaret heard some exciting news from family.", "Margaret likes tea."])
        self.assertTrue(all("Rewrite this memory so it contains nothing about Sarah" in p for p in prompts))
        self.assertEqual(self.recall("Sarah"), [])
        self.assertEqual(len(self.recall("Margaret")), 3)

    def test_forget_deletes_when_rewrite_is_impossible(self):
        for text in ("Sarah told Margaret a secret.", "Sarah and Margaret baked."):
            self.save(text, ["Sarah", "Margaret"])
        replies = iter(["DELETE", "Sarah's mom baked."])
        self.assertEqual(pm.forget_person(self.db, "Sarah", PROFILE, lambda p: next(replies)),
                         {"deleted": 2, "rewritten": 0})
        self.assertEqual(self.db._collection.count(), 0)

    def test_forget_offline_keeps_memory_and_raises_for_retry(self):
        # A network blip must not delete other people's memories
        self.save("Sarah only.", ["Sarah"])
        self.save("Sarah and Margaret sang.", ["Sarah", "Margaret"])
        def offline(prompt):
            raise RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            pm.forget_person(self.db, "Sarah", PROFILE, offline)
        self.assertIn("Sarah and Margaret sang.", self.db._collection.get()["documents"])
        replies = iter(["Margaret sang."])                         # retried once back online
        self.assertEqual(pm.forget_person(self.db, "Sarah", PROFILE, lambda p: next(replies))
                         ["rewritten"], 1)
        self.assertEqual(self.db._collection.get()["documents"], ["Margaret sang."])

    def test_forgetting_one_person_spares_others_with_the_same_first_name(self):
        profile = {"user_name": "Margaret Jones", "about": "",
                   "household": [{"name": "Aunt Sue"}, {"name": "Aunt Mary"}]}
        self.save("Margaret Jones baked bread with Margaret Lee.", ["Margaret Jones", "Margaret Lee"])
        self.save("Margaret Jones has a checkup on Friday.", ["Margaret Jones"])
        self.save("Aunt Sue is coming for Thanksgiving.", ["Aunt Sue"])
        self.save("Aunt Mary sent a card.", ["Aunt Mary"])
        rewrites = iter(["Margaret Jones baked bread with a friend."])
        result = pm.forget_person(self.db, "Margaret Lee", profile, lambda p: next(rewrites),
                                  extra_names=["Margaret Lee"])
        self.assertEqual(result, {"deleted": 0, "rewritten": 1})
        self.assertEqual(pm.forget_person(self.db, "Aunt Mary", profile, lambda p: ""),
                         {"deleted": 1, "rewritten": 0})
        self.assertEqual(sorted(self.db._collection.get()["documents"]),
                         ["Aunt Sue is coming for Thanksgiving.",
                          "Margaret Jones baked bread with a friend.",
                          "Margaret Jones has a checkup on Friday."])

    def test_prompt_block_leaves_out_people_being_forgotten(self):
        self.save("Sarah is in debt.", ["Sarah"])
        self.save("Margaret likes tea.", ["Margaret"])
        block = pm.memory_block(self.db, PROFILE, exclude={"Sarah"})
        self.assertIn("Margaret likes tea.", block)
        self.assertNotIn("Sarah", block)

    def test_forgetting_a_fuller_name_never_hits_someone_else(self):
        # "Margaret Lee" isn't known: it must not be taken to mean Margaret (the user)
        self.save("Margaret likes tea.", ["Margaret"])
        self.assertEqual(pm.forget_person(self.db, "Margaret Lee", PROFILE, lambda p: "DELETE"),
                         {"deleted": 0, "rewritten": 0})
        self.assertEqual(self.db._collection.count(), 1)

    def test_forget_by_any_spelling_of_someone_in_memories(self):
        # Met only in conversation (not in the profile), asked in lowercase
        self.save("Margaret heard about Linda's new job.", ["Margaret", "Linda"])
        rewrites = iter(["Margaret heard about a friend's new job."])
        self.assertEqual(pm.forget_person(self.db, "linda", PROFILE, lambda p: next(rewrites)),
                         {"deleted": 0, "rewritten": 1})

    def test_forget_leaves_common_words_alone(self):
        # Forgetting "Will" or "May" must not touch "she will" / "in May"
        profile = {**PROFILE, "household": PROFILE["household"] + [
            {"name": "Will", "relationship": "neighbor"}, {"name": "May", "relationship": "friend"}]}
        self.save("Margaret will visit her sister on Sunday.", ["Margaret"])
        self.save("Margaret's checkup is in May.", ["Margaret"])
        self.save("Will fixed Margaret's gate.", ["Will", "Margaret"])
        calls = []
        def rewrite(prompt):
            calls.append(prompt)
            return "Someone fixed Margaret's gate."
        self.assertEqual(pm.forget_person(self.db, "Will", profile, rewrite),
                         {"deleted": 0, "rewritten": 1})
        self.assertEqual(pm.forget_person(self.db, "May", profile, rewrite),
                         {"deleted": 0, "rewritten": 0})
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(self.db._collection.get()["documents"]),
                         ["Margaret will visit her sister on Sunday.", "Margaret's checkup is in May.",
                          "Someone fixed Margaret's gate."])

    def test_forget_nobody(self):
        self.save("Margaret likes tea.", ["Margaret"])
        self.assertEqual(pm.forget_person(self.db, "the user", PROFILE, lambda p: ""),
                         {"deleted": 0, "rewritten": 0})
        self.assertEqual(pm.forget_person(self.db, "Zed", PROFILE, lambda p: ""),
                         {"deleted": 0, "rewritten": 0})
        self.assertEqual(self.db._collection.count(), 1)


@unittest.skipUnless(os.path.exists(os.path.join(os.path.dirname(__file__), "..", "Baymax-main",
                                                 "all-MiniLM-L6-v2-onnx", "model.onnx")),
                     "real embedding model not present (run convert_model.py)")
class RealEmbedderTests(unittest.TestCase):
    """The robot's own SemanticEmbedder (all-MiniLM-L6-v2 ONNX + ChromaDB)."""

    @classmethod
    def setUpClass(cls):
        from semantic_embedder import SemanticEmbedder
        cls.SemanticEmbedder = SemanticEmbedder
        cls.model_dir = os.path.join(os.path.dirname(__file__), "..", "Baymax-main",
                                     "all-MiniLM-L6-v2-onnx")

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = self.SemanticEmbedder(self.model_dir, verbose=False, chroma_dir=self.dir.name,
                                        collection_name="user_memories")

    def tearDown(self):
        self.dir.cleanup()

    def test_repeats_merge_but_changes_are_kept(self):
        pm.save_memories(self.db, [{"memory": "Margaret's hip has been hurting.", "people": ["Margaret"]}])
        self.assertEqual(pm.save_memories(self.db, [{"memory": "Margaret's hip has been hurting again.",
                                                     "people": ["Margaret"]}]), (0, 1))
        self.assertEqual(self.db._collection.get()["documents"], ["Margaret's hip has been hurting again."])
        # a real change is a new memory, not merged away
        self.assertEqual(pm.save_memories(self.db, [{"memory": "Margaret's hip is feeling much better.",
                                                     "people": ["Margaret"]}]), (1, 0))
        self.assertEqual(self.db._collection.count(), 2)
        self.assertEqual([m["memory"] for m in pm.memories_involving(self.db, "Margaret", PROFILE)][0],
                         "Margaret's hip is feeling much better.")

    def test_forget_with_real_store(self):
        pm.save_memories(self.db, [
            {"memory": "Sarah told Margaret about her job interview.", "people": ["Sarah", "Margaret"]},
            {"memory": "Sarah adopted a dog.", "people": ["Sarah"]}])
        result = pm.forget_person(self.db, "Sarah", PROFILE,
                                  lambda p: "Margaret heard some good news from family.")
        self.assertEqual(result, {"deleted": 1, "rewritten": 1})
        hits = self.db.search("good news from family", n_results=1)
        self.assertEqual(hits[0]["document"], "Margaret heard some good news from family.")
        self.assertEqual(hits[0]["metadata"]["people"], "|Margaret|")


# ─── Appearance ──────────────────────────────────────────────────────────────
class AppearanceTests(unittest.TestCase):
    def reply(self, obj):
        def generate(prompt, image=None):
            self.prompt, self.image = prompt, image
            if isinstance(obj, Exception):
                raise obj
            return obj if isinstance(obj, str) else json.dumps(obj)
        return generate

    def test_first_time_describes_look_and_outfit(self):
        r = pm.check_appearance(self.reply({"look": "Long brown hair, glasses",
                                            "today": "a bright red cardigan",
                                            "changed": True, "change": "x"}), b"jpg")
        self.assertEqual(r, {"description": "Long brown hair, glasses",
                             "today": "a bright red cardigan", "changed": False, "change": ""})
        self.assertEqual(self.image, b"jpg")
        self.assertIn("Never mention body shape, weight, skin, age", self.prompt)
        self.assertIn("kindly compliment", self.prompt)
        self.assertNotIn("Their look last time", self.prompt)

    def test_compares_look_not_clothes(self):
        r = pm.check_appearance(self.reply({"look": "Short brown hair, glasses", "today": "",
                                            "changed": True, "change": "shorter hair"}),
                                b"jpg", previous="Long brown hair, glasses")
        self.assertEqual((r["changed"], r["change"], r["today"]), (True, "shorter hair", ""))
        self.assertIn('Their look last time: "Long brown hair, glasses"', self.prompt)
        self.assertIn("Ignore lighting, camera angle and clothing", self.prompt)

    def test_older_reply_shape(self):
        r = pm.check_appearance(self.reply({"description": "Grey bob", "changed": False}), b"j")
        self.assertEqual(r, {"description": "Grey bob", "today": "", "changed": False, "change": ""})

    def test_unconvincing_or_broken_replies(self):
        self.assertFalse(pm.check_appearance(self.reply({"look": "d", "changed": True, "change": ""}),
                                             b"j", previous="p")["changed"])
        self.assertFalse(pm.check_appearance(self.reply({"look": "d", "changed": "yes", "change": "c"}),
                                             b"j", previous="p")["changed"])
        self.assertIsNone(pm.check_appearance(self.reply({"look": "", "today": "hat"}), b"j"))
        self.assertIsNone(pm.check_appearance(self.reply("not json"), b"j"))
        self.assertIsNone(pm.check_appearance(self.reply(RuntimeError("offline")), b"j"))


# ─── Starting a conversation ────────────────────────────────────────────────
class InitiationPromptTests(unittest.TestCase):
    def test_a_stranger_present_is_mentioned_and_discretion_asked(self):
        p = pm.initiation_prompt(
            [{"name": "Margaret", "label": "Margaret", "here_for": 3 * 3600, "appearance": None}],
            [{"memory": "Margaret's biopsy results come back Tuesday.", "people": ["Margaret"], "time": 0}],
            unknown=1)
        self.assertIn("With you now: Margaret (here for about 3 hours); someone you don't recognize.", p)
        self.assertIn("don't bring up anything private", p)

    def test_named_with_looks_and_memories(self):
        p = pm.initiation_prompt(
            [{"name": "Margaret", "label": "Margaret", "here_for": 3 * 3600,
              "appearance": {"changed": True, "change": "shorter hair",
                             "today": "a bright red cardigan"}}],
            [{"memory": "Margaret's roses are blooming.", "people": ["Margaret"], "time": 0}])
        self.assertTrue(p.startswith("[INITIATE CONVERSATION] With you now: Margaret (here for about 3 hours)."))
        self.assertIn("Gently start a conversation with Margaret", p)
        self.assertIn("Margaret's look has changed since last time: shorter hair", p)
        self.assertIn("today Margaret has on a bright red cardigan", p)
        self.assertIn("pick ONE", p)
        self.assertIn("- Margaret's roses are blooming. (Margaret)", p)
        self.assertIn("Never comment on weight, body, skin or age", p)
        self.assertNotIn("The user", p)

    def test_two_people(self):
        p = pm.initiation_prompt(
            [{"name": "Margaret", "label": "Margaret", "here_for": 7200, "appearance": None},
             {"name": "Sarah", "label": "Sarah (Margaret's daughter)", "here_for": 120,
              "appearance": {"changed": False, "today": ""}}], [])
        self.assertIn("With you now: Margaret (here for about 2 hours); Sarah (Margaret's daughter, "
                      "just arrived).", p)
        self.assertIn("conversation with Margaret and Sarah", p)
        self.assertIn("keep private things between you and the person they're about", p)
        self.assertNotIn("how they look", p)                  # nothing to go on
        self.assertNotIn("Things you remember", p)

    def test_nobody_recognised(self):
        p = pm.initiation_prompt([], [], unknown=1)
        self.assertIn("Someone you don't recognize is here", p)
        self.assertIn("follow up on something you remember", p)


# ─── Greeting ────────────────────────────────────────────────────────────────
class ArrivalPromptTests(unittest.TestCase):
    def test_describe_away(self):
        now = time.time()
        self.assertEqual(pm.describe_away(25 * 60), "about 25 minutes ago")
        self.assertEqual(pm.describe_away(3 * 3600), "about 3 hours ago")
        noon = dt.datetime(2026, 10, 7, 12, 0).timestamp()
        self.assertEqual(pm.describe_away(26 * 3600, noon - 26 * 3600, noon), "yesterday")
        three = now - 3 * 86400
        self.assertEqual(pm.describe_away(3 * 86400, three),
                         f"3 days ago ({dt.datetime.fromtimestamp(three).strftime('%A')})")
        self.assertEqual(pm.describe_away(30 * 86400, now - 30 * 86400), "about 30 days ago")
        # By calendar day, not rounded hours
        morning = dt.datetime(2026, 10, 7, 8, 0).timestamp()             # Wednesday 8 AM
        self.assertEqual(pm.describe_away(35 * 3600, morning - 35 * 3600, morning),
                         "2 days ago (Monday)")                          # Monday 9 PM
        night = dt.datetime(2026, 10, 7, 23, 30).timestamp()
        self.assertEqual(pm.describe_away(21 * 3600, night - 21 * 3600, night), "earlier today")
        self.assertIsNone(pm.describe_away(None))

    def test_full_greeting(self):
        p = pm.arrival_prompt("Sarah (Margaret's daughter)", "Sarah", 2 * 86400, time.time() - 2 * 86400,
                              [{"memory": "Sarah told Margaret about her job interview on Friday.",
                                "people": ["Sarah", "Margaret"], "time": time.time() - 2 * 86400}],
                              {"changed": True, "change": "shorter hair"})
        self.assertTrue(p.startswith("[ARRIVED] Sarah (Margaret's daughter) just came into view."))
        self.assertIn("You last saw Sarah 2 days ago", p)
        when = dt.datetime.fromtimestamp(time.time() - 2 * 86400).strftime("%A")
        self.assertIn(f"- Sarah told Margaret about her job interview on Friday. ({when}; with Margaret)", p)
        self.assertIn("Things you remember that involve Sarah, newest first:", p)
        self.assertIn("Sarah looks a little different from last time: shorter hair.", p)
        self.assertIn("Did you get a haircut?", p)
        self.assertIn("Never comment on weight, body, skin or age", p)
        self.assertIn("Greet Sarah warmly by name", p)

    def test_outfit_alone_is_a_conversation_starter(self):
        p = pm.arrival_prompt("Sarah", "Sarah", 3600, time.time() - 3600, [],
                              {"changed": False, "change": "", "today": "a yellow raincoat"})
        self.assertIn("Today Sarah has on: a yellow raincoat.", p)
        self.assertIn("What a lovely sweater!", p)
        self.assertNotIn("looks a little different", p)

    def test_plain_greeting_has_no_appearance_talk(self):
        p = pm.arrival_prompt("Margaret", "Margaret", None, None, [], {"changed": False},
                              is_primary=True)
        self.assertIn("first time you've seen Margaret since you learned their face", p)
        self.assertIn("Welcome Margaret back warmly by name", p)
        self.assertNotIn("haircut", p)
        self.assertNotIn("remember that involve", p)


if __name__ == "__main__":
    unittest.main()
