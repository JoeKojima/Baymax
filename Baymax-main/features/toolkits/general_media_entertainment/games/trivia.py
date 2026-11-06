"""One quiz question at a time from a small local batch."""
import threading
import time

from ...public_sources import PublicSources
from ...source_helpers import text, rows


class Trivia(PublicSources):
    def __init__(self, client):
        super().__init__(client)
        self.trivia_lock = threading.Lock()
        self.trivia_after = 0
        self.questions = []

    def trivia(self):
        with self.trivia_lock:
            if not self.questions:
                if time.monotonic() < self.trivia_after:
                    raise ValueError("Trivia is cooling down; try again in five seconds")
                self.trivia_after = time.monotonic() + 5
                data, info = self._json("https://opentdb.com/api.php", {"amount": 5}, 0)
                if not isinstance(data, dict) or data.get("response_code") != 0:
                    raise ValueError("Trivia is unavailable; try again later")
                questions = []
                for q in rows(data.get("results"))[:5]:
                    wrong = q.get("incorrect_answers")
                    if not isinstance(wrong, list) or not 1 <= len(wrong) <= 3:
                        raise ValueError("Invalid trivia answers")
                    questions.append({**info, "source": "Open Trivia DB (CC BY-SA 4.0)",
                        "question": text(q.get("question")), "answer": text(q.get("correct_answer")),
                        "alternatives": [text(a) for a in wrong],
                        "notice": "Ask the question first; keep the answer hidden until the user responds."})
                self.questions = questions
            if not self.questions:
                raise ValueError("No trivia questions available")
            return self.questions.pop(0)
