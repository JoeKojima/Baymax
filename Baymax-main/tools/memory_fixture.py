"""Labelled fictional replay data; replace with approved representative data.

No real patient data. Labels apply to the *first-fragment 30-word snapshot*,
including its deliberately imperfect split words, not a cleaned final question.
"""


def synthetic_fixture():
    memories, conversations = [], []
    for patient, name in (("synthetic_a", "Sarah"), ("synthetic_b", "Sara")):
        facts = {
            "family": [f"{name} is the patient's daughter.", f"{name} has two children.",
                       f"The patient calls {name} every Sunday."],
            "garden": ["The patient grows roses in the garden.", "Gardening is a favourite hobby.",
                       "The patient gardens with a neighbour."],
            "music": ["The patient plays piano music.", "The patient enjoys jazz music.",
                      "A music club meets on Fridays."],
            "appointment": ["The patient has a doctor appointment on Monday.",
                            "A daughter accompanies the patient to doctor appointments.",
                            "The appointment clinic is near the library."],
        }
        for topic, texts in facts.items():
            memories.extend({"id": f"{patient}-{topic}-{i}", "patient_id": patient, "text": text}
                            for i, text in enumerate(texts))
        for split in ("development", "heldout"):
            for topic, question in (("family", f"{name} is not visiting today. "),
                                    ("garden", "My garden roses need attention. "),
                                    ("music", "The music club meets soon. "),
                                    ("appointment", "My doctor appointment was cancelled. ")):
                if split == "heldout":
                    question = {"family": f"{name} cancelled a family visit this week. ",
                                "garden": "Gardening with my neighbour brings me joy. ",
                                "music": "I enjoy playing jazz piano at the music club. ",
                                "appointment": "My daughter will join my Monday clinic appointment. "}[topic]
                turns = []
                # Repetition, follow-up, wrong prediction, topic change, unknown
                # topic, similar names, and split-token first fragments.
                long = ("Could you help me remember the details of what we discussed together "
                        "in our earlier conversation because I would like to talk about it again today? " + question)
                variants = [long, long, question + "Tell me more. ",
                            "I was wondering about " + question, "xyzzy ", question]
                for i, text in enumerate(variants):
                    boundary = text.find(" ") + 1
                    fragments = [{"offset_ms": 0, "text": text[:boundary]},
                                 {"offset_ms": 120, "text": text[boundary:boundary+3]},
                                 {"offset_ms": 250, "text": text[boundary+3:]}]
                    if i < 2:
                        # A repeated long first fragment replaces the entire
                        # rolling window, exercising genuine exact-query hits.
                        fragments = [{"offset_ms": 0, "text": text},
                                     {"offset_ms": 120, "text": "Please continue. "}]
                    elif i == 5:
                        fragments = [{"offset_ms": 0, "text": text[:2]},
                                     {"offset_ms": 120, "text": text[2:]}]
                    turns.append({"fragments": fragments,
                                  "expected_ids": [] if i == 4 else [f"{patient}-{topic}-{j}" for j in range(3)],
                                  "predictions": ["music club", question, "garden roses", "doctor appointment", name]
                                      if i == 2 else [question, "garden roses", "music club", "doctor appointment", name],
                                  "reply_offset_ms": 900, "gap_ms": 15_000})
                conversations.append({"id": f"{split}-{patient}-{topic}", "patient_id": patient,
                                      "split": split, "turns": turns})
    return {"version": 1, "description": "Fictional software smoke fixture; not deployment evidence",
            "memories": memories, "conversations": conversations}


def query_snapshots(conversation):
    """Match v10's original strip/split rolling-window behaviour exactly."""
    words = []
    for turn in conversation["turns"]:
        query = None
        for fragment in turn["fragments"]:
            if fragment["text"].strip():
                words.extend(fragment["text"].strip().split())
                words[:] = words[-30:]
                if query is None:
                    query = " ".join(words)
        yield turn, query or ""
