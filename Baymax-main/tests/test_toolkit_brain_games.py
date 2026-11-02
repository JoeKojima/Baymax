"""Brain games: local rounds and score through the Discovery toolkit; no network or devices."""
import os
import random
import unittest
from unittest.mock import patch

from features.toolkits import experimental, gemini_tools
from features.toolkits.general_media_entertainment.games import brain
from features.toolkits.general_media_entertainment.games.brain import BrainGames, GAMES


class BrainGameTests(unittest.TestCase):
    def setUp(self):
        self.games = BrainGames(rng=random.Random(7))

    def play(self, **args):
        return self.games.brain_game(**args)

    def test_every_game_starts_with_content_and_counts_rounds(self):
        for game in GAMES:
            first = self.play(action="start", game=game)
            self.assertEqual((first["game"], first["round"], first["score"]), (game, 1, 0))
            self.assertGreater(len(first), 4, game)  # State plus the round's content.
            self.assertEqual(self.play(action="next")["round"], 2)

    def test_rounds_do_not_repeat_until_the_list_is_used_up(self):
        riddles = [self.play(action="start", game="riddles")["riddle"]]
        riddles += [self.play(action="next")["riddle"] for _ in range(len(brain.RIDDLES) - 2)]
        riddles.append(self.play(action="start", game="riddles")["riddle"])  # Restarting keeps history.
        self.assertEqual(len(set(riddles)), len(brain.RIDDLES))
        again = self.play(action="next")["riddle"]
        self.assertNotEqual(again, riddles[-1])  # Starting over never repeats the last one.

    def test_list_recall_length_follows_difficulty_and_never_reuses_words(self):
        seen = []
        for level, count in (("easy", 4), ("medium", 5), ("hard", 6)):
            words = self.play(action="start", game="list_recall", difficulty=level)["words"]
            self.assertEqual(len(words), count)
            seen += words
        self.assertEqual(len(seen), len(set(seen)))
        for _ in range(30):  # Runs through the list and starts over without failing.
            self.assertEqual(len(self.play(action="next")["words"]), 6)

    def test_answers_are_returned_hidden_with_a_notice(self):
        riddle = self.play(action="start", game="riddles")
        self.assertIn("answer", riddle)
        self.assertIn("hidden", riddle["notice"])
        self.play(action="start", game="word_puzzles")
        for _ in range(9):
            puzzle = self.play(action="next")
            self.assertIn("puzzle", puzzle)
            if puzzle["puzzle"].startswith("Unscramble"):
                letters = puzzle["puzzle"].split(": ")[1].rstrip(".").replace(", ", "").lower()
                self.assertEqual(sorted(letters), sorted(puzzle["answer"]))
                self.assertNotEqual(letters, puzzle["answer"])

    def test_twenty_questions_gives_ember_a_secret_and_both_directions(self):
        result = self.play(action="start", game="twenty_questions")
        self.assertTrue(result["your_secret"])
        self.assertTrue(result["tell_them"].startswith("I'm thinking of"))
        self.assertIn("you ask", result["how_to_play"])

    def test_score_result_and_end(self):
        self.play(action="start", game="riddles", difficulty="easy")
        self.play(action="result", correct=True)
        self.play(action="result", correct=False)
        self.play(action="result", correct=True)
        self.assertEqual(self.play(action="score")["score"], 2)
        ended = self.play(action="end")
        self.assertEqual((ended["status"], ended["score"], ended["difficulty"]), ("ended", 2, "easy"))
        self.assertEqual(self.play(action="start", game="riddles")["score"], 0)

    def test_invalid_requests(self):
        with self.assertRaises(ValueError):
            self.play(action="next")  # Nothing running.
        for args in ({"action": "start"}, {"action": "start", "game": "chess"},
                     {"action": "start", "game": "riddles", "difficulty": "expert"}, {"action": "dance"}):
            with self.assertRaises(ValueError):
                self.play(**args)
        self.play(action="start", game="riddles")
        with self.assertRaises(ValueError):
            self.play(action="result", correct="yes")


class BrainGameToolkitTests(unittest.TestCase):
    def test_on_by_default_with_discovery_and_routed_through_gemini_tools(self):
        with patch.dict(os.environ, {"BAYMAX_TOOLKIT_ENABLED": "1", "EMBER_PUBLIC_TOOLKIT": "0"}):
            os.environ.pop("EMBER_DISCOVERY_ENABLED", None)
            self.assertIn("brain_game", experimental.enabled_names())
            with patch.object(experimental.toolkit, "brain", BrainGames(rng=random.Random(1))):
                result = gemini_tools.execute("brain_game", {"action": "start", "game": "categories"})
                self.assertIn("challenge", result)
                self.assertIn("error", gemini_tools.execute("brain_game", {"action": "start", "game": "chess"}))
                self.assertIn("error", gemini_tools.execute("brain_game", {"action": "start", "level": 3}))
                self.assertIn("error", gemini_tools.execute("brain_game", {}))
            config = experimental.extend_config({"system_instruction": "base", "tools": []})
        self.assertIn("[BRAIN GAMES]", config["system_instruction"])
        self.assertEqual(experimental.extend_config(config), config)  # Added once.
        names = [d["name"] for tool in config["tools"] for d in tool["function_declarations"]]
        self.assertEqual(names.count("brain_game"), 1)

    def test_only_when_asked_in_the_prompt(self):
        text = experimental.GAMES_INSTRUCTION
        self.assertIn("only when someone asks", text)
        self.assertIn("Never start or offer one unprompted", text)
        self.assertIn("never suggest a medical or cognitive problem", text)

    def test_disabled_with_discovery_or_the_toolkit(self):
        for env in ({"EMBER_DISCOVERY_ENABLED": "0", "EMBER_PUBLIC_TOOLKIT": "0"}, {"BAYMAX_TOOLKIT_ENABLED": "0"}):
            with patch.dict(os.environ, env):
                self.assertNotIn("brain_game", experimental.enabled_names())
                self.assertIn("error", experimental.toolkit.execute("brain_game", {"action": "score"}))
                config = experimental.extend_config({"system_instruction": "base", "tools": []})
                self.assertNotIn("[BRAIN GAMES]", config["system_instruction"])

    def test_no_network(self):
        toolkit = experimental.Toolkit(client=object())  # Any HTTP use would fail on this client.
        with patch.dict(os.environ, {"BAYMAX_TOOLKIT_ENABLED": "1", "EMBER_DISCOVERY_ENABLED": "1"}):
            self.assertIn("story", toolkit.execute("brain_game", {"action": "start", "game": "story_recall"}))


if __name__ == "__main__":
    unittest.main()
