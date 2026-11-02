"""Spoken brain games from local word lists: fresh rounds and a running score, no network."""
import random
import threading

GAMES = ("word_association", "twenty_questions", "categories", "list_recall",
         "story_recall", "riddles", "word_puzzles")
LEVELS = ("easy", "medium", "hard")

WORDS = [
    "apple", "river", "garden", "candle", "bicycle", "window", "piano", "bread", "ocean", "kite",
    "blanket", "lighthouse", "orange", "teacup", "forest", "train", "button", "honey", "mountain",
    "umbrella", "violin", "pillow", "lantern", "carrot", "feather", "bridge", "clock", "meadow",
    "basket", "snowman", "pencil", "harbor", "cookie", "ladder", "sunflower", "mirror", "wagon",
    "cherry", "castle", "scarf", "rocket", "pebble", "kettle", "rainbow", "turtle", "anchor",
    "tulip", "compass", "muffin", "volcano", "drum", "puzzle", "postcard", "acorn", "marble",
]
SECRETS = [  # Ember thinks of one in twenty questions.
    ("an animal", ["elephant", "penguin", "giraffe", "owl", "dolphin", "rabbit", "squirrel", "camel"]),
    ("something in the kitchen", ["toaster", "teapot", "rolling pin", "refrigerator", "whisk", "frying pan"]),
    ("a food", ["pizza", "banana", "pancake", "watermelon", "sandwich", "popcorn", "carrot cake"]),
    ("something outdoors", ["rainbow", "oak tree", "park bench", "mailbox", "lighthouse", "waterfall"]),
    ("something in the house", ["grandfather clock", "armchair", "bookshelf", "doorbell", "piano", "photo album"]),
    ("a way to travel", ["bicycle", "sailboat", "hot air balloon", "train", "canoe", "horse and cart"]),
]
CATEGORIES = [
    "fruits", "vegetables", "animals", "birds", "flowers", "countries", "cities", "things in a kitchen",
    "things you wear", "musical instruments", "sports", "jobs", "things that are round", "things at the beach",
    "colors", "kinds of weather", "things in a garden", "breakfast foods", "boys' names", "girls' names",
    "desserts", "things that fly", "tools", "board games",
]
LETTERS = {"easy": "BCDFGHLMPRST", "medium": "ABCDEGHJKLMNOPRSTW", "hard": "AEIJKNOQUVWY"}
STORIES = [
    ("Maria baked twelve lemon muffins on Saturday morning. She gave four to her neighbor Tom, "
     "who had just fixed her blue garden gate. Tom said they were the best muffins he had ever tasted.",
     ["What did Maria bake?", "How many did she bake?", "Who were they for?", "What had he fixed?",
      "What color was it?"]),
    ("On a rainy Tuesday, Sam took the number nine bus to the library. He borrowed a book about "
     "lighthouses and a cookbook for his sister Ana, then stopped for hot chocolate on the way home.",
     ["What day was it?", "Which bus did Sam take?", "What were the two books about?",
      "Who was the cookbook for?", "What did he drink?"]),
    ("Grandpa Joe planted three apple trees by the red barn when his daughter was born. Forty years "
     "later, her children climb them every autumn and fill a wicker basket with apples.",
     ["How many trees did Joe plant?", "What kind of trees?", "Where did he plant them?",
      "How long ago?", "What do the children fill?"]),
    ("Priya's cat, Biscuit, went missing on Friday evening. On Sunday he was found asleep in the "
     "laundry basket at the house next door, curled up on a yellow towel.",
     ["What is the cat's name?", "When did he go missing?", "Where was he found?",
      "Whose house was it?", "What color was the towel?"]),
    ("At the summer fair, Linda won second prize for her strawberry jam. The first prize went to a "
     "man named Frank for his peach pie, and the judges ate two slices each.",
     ["What did Linda make?", "Which prize did she win?", "Who won first prize?",
      "For what?", "How many slices did each judge eat?"]),
    ("Every Thursday, Helen walks her dog Pepper to the park by the pond. Last week they saw "
     "five ducks, an old man feeding pigeons, and a little boy with a green kite.",
     ["Which day does Helen walk?", "What is the dog called?", "How many ducks were there?",
      "What was the old man doing?", "What color was the kite?"]),
]
RIDDLES = [
    ("What has keys but can't open locks?", "a piano"),
    ("What has hands but can't clap?", "a clock"),
    ("What gets wetter the more it dries?", "a towel"),
    ("What has a neck but no head?", "a bottle"),
    ("What can you catch but not throw?", "a cold"),
    ("What has many teeth but can't bite?", "a comb"),
    ("What goes up but never comes down?", "your age"),
    ("What has one eye but can't see?", "a needle"),
    ("What is full of holes but still holds water?", "a sponge"),
    ("What can travel around the world while staying in a corner?", "a stamp"),
    ("What has a face and two hands but no arms or legs?", "a clock"),
    ("The more you take, the more you leave behind. What are they?", "footsteps"),
    ("What building has the most stories?", "a library"),
    ("What runs but never walks, and has a bed but never sleeps?", "a river"),
    ("What belongs to you, but other people use it more than you do?", "your name"),
    ("What has words but never speaks?", "a book"),
    ("I'm tall when I'm young and short when I'm old. What am I?", "a candle"),
    ("What comes down but never goes up?", "rain"),
    ("What kind of room has no doors or windows?", "a mushroom"),
    ("What has legs but doesn't walk?", "a table"),
]
RHYMES = ["cat", "bake", "light", "rain", "bell", "moon", "chair", "boat", "sing", "tree", "cake", "hat"]
OPPOSITES = [("hot", "cold"), ("early", "late"), ("empty", "full"), ("whisper", "shout"), ("ancient", "modern"),
             ("generous", "selfish"), ("shallow", "deep"), ("borrow", "lend"), ("arrive", "depart"),
             ("rough", "smooth"), ("heavy", "light"), ("victory", "defeat")]
ANAGRAMS = {"easy": ["cat", "dog", "sun", "map", "pen", "cup", "hat", "bus"],
            "medium": ["bread", "house", "apple", "chair", "plant", "clock", "smile", "train"],
            "hard": ["garden", "orange", "pencil", "basket", "kitten", "window", "candle", "pirate"]}
COUNTS = {"easy": 4, "medium": 5, "hard": 6}

NOTICE = "Keep any answer hidden until they respond; give hints if they want them."


class BrainGames:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.lock = threading.Lock()
        self.used = {}           # Game -> items already used this run, so rounds do not repeat.
        self.game = None
        self.level = "medium"
        self.round = 0
        self.score = 0

    def _pick(self, game, items):
        """A random unused item; once all are used, start over (avoiding the last one)."""
        used = self.used.setdefault(game, [])
        fresh = [i for i in items if i not in used]
        if not fresh:
            del used[:-1]
            fresh = [i for i in items if i not in used]
        choice = self.rng.choice(fresh)
        used.append(choice)
        return choice

    def _round(self):
        game, level = self.game, self.level
        if game == "word_association":
            return {"start_word": self._pick(game, WORDS),
                    "how_to_play": "Take turns saying the first word the last one brings to mind."}
        if game == "twenty_questions":
            hint, things = self._pick(game, SECRETS)
            return {"your_secret": self.rng.choice(things), "tell_them": f"I'm thinking of {hint}.",
                    "how_to_play": "They ask up to 20 yes/no questions; count them. Do not reveal the secret "
                                   "unless they guess it or give up. If they want to think of something "
                                   "instead, you ask the yes/no questions."}
        if game == "categories":
            count = COUNTS[level] - 1
            return {"challenge": f"Name {count} {self._pick(game, CATEGORIES)} starting with "
                                 f"{self.rng.choice(LETTERS[level])}.",
                    "how_to_play": "Accept any reasonable answers, help if they get stuck, and add one of your own."}
        if game == "list_recall":
            used = self.used.setdefault(game, [])
            fresh = [w for w in WORDS if w not in used]
            if len(fresh) < COUNTS[level]:
                used.clear()
                fresh = list(WORDS)
            words = self.rng.sample(fresh, COUNTS[level])
            used.extend(words)
            return {"words": words,
                    "how_to_play": "Say the words slowly once, chat about something else for a minute, "
                                   "then ask them to recall the words in any order. Count the ones they get."}
        if game == "story_recall":
            story, questions = self._pick(game, STORIES)
            return {"story": story, "questions": questions[:COUNTS[level] - 1],
                    "how_to_play": "Tell the story warmly, then ask the questions one at a time."}
        if game == "riddles":
            question, answer = self._pick(game, RIDDLES)
            return {"riddle": question, "answer": answer, "notice": NOTICE}
        kind = self._pick("puzzle_kind", ["rhyme", "opposite", "anagram"])
        if kind == "rhyme":
            return {"puzzle": f"Name as many words as you can that rhyme with '{self._pick('rhyme', RHYMES)}'."}
        if kind == "opposite":
            word, answer = self._pick("opposite", OPPOSITES)
            return {"puzzle": f"What is the opposite of '{word}'?", "answer": answer, "notice": NOTICE}
        word = self._pick("anagram", ANAGRAMS[level])
        letters = list(word)
        while "".join(letters) == word:
            self.rng.shuffle(letters)
        return {"puzzle": "Unscramble these letters: " + ", ".join(l.upper() for l in letters) + ".",
                "answer": word, "notice": NOTICE}

    def _state(self):
        return {"game": self.game, "difficulty": self.level, "round": self.round, "score": self.score}

    def brain_game(self, action, game=None, difficulty=None, correct=None):
        with self.lock:
            if difficulty is not None:
                if difficulty not in LEVELS:
                    raise ValueError("difficulty must be easy, medium or hard")
                self.level = difficulty
            if action == "start":
                if game not in GAMES:
                    raise ValueError("Choose a game: " + ", ".join(GAMES))
                self.game, self.round, self.score = game, 0, 0
            elif action in ("next", "result", "score") and self.game is None:
                raise ValueError("No game is running; start one first")
            if action in ("start", "next"):
                self.round += 1
                return {**self._state(), **self._round()}
            if action == "result":
                if not isinstance(correct, bool):
                    raise ValueError("result needs correct: true or false")
                self.score += correct
                return self._state()
            if action == "score":
                return self._state()
            if action == "end":
                state, self.game, self.round, self.score = self._state(), None, 0, 0
                return {**state, "status": "ended"}
            raise ValueError("action must be start, next, result, score or end")
