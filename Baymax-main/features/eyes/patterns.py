"""Exact expression bitmaps from the user's full MAX7219 Python demo."""
HAPPY = (0, 24, 36, 66, 66, 0, 0, 0)
SAD = (0, 0, 0, 66, 66, 36, 24, 0)
EXCITED_LEFT = (0, 24, 4, 2, 2, 4, 24, 0)
EXCITED_RIGHT = (0, 24, 32, 64, 64, 32, 24, 0)
CONFUSED_FRAMES = ((28, 32, 76, 82, 74, 34, 28, 0), (0, 28, 34, 73, 85, 69, 56, 0), (0, 56, 68, 82, 74, 50, 4, 56), (0, 28, 162, 170, 146, 68, 56, 0))
SURPRISED = (24, 24, 24, 24, 24, 0, 24, 24)
CURIOUS = (0, 56, 68, 8, 16, 0, 16, 0)
LOVE = (0, 102, 153, 129, 129, 66, 36, 24)
SLEEP_BASE = (0, 0, 0, 0, 0, 0, 126, 0)
SLEEP_1 = (0, 2, 0, 0, 0, 0, 126, 0)
SLEEP_2 = (0, 0, 8, 0, 0, 0, 126, 0)
SLEEP_3 = (0, 0, 0, 0, 32, 0, 126, 0)
DEAD = (0, 66, 36, 24, 24, 36, 66, 0)

# Paired frames, seconds per frame; left device 1, right device 0.
EXPRESSIONS = {
    'happy': (((HAPPY, HAPPY),), 2.0),
    'sad': (((SAD, SAD),), 2.0),
    'excited': (((EXCITED_LEFT, EXCITED_RIGHT),), 2.0),
    'confused': (tuple((frame, frame) for frame in CONFUSED_FRAMES), 0.10),
    'surprised': (((SURPRISED, SURPRISED),), 2.8),
    'curious': (((CURIOUS, CURIOUS),), 3.0),
    'love': (((LOVE, LOVE),), 3.0),
    'sleep': (tuple((SLEEP_BASE, frame) for frame in
                    (SLEEP_BASE, SLEEP_3, SLEEP_2, SLEEP_1, SLEEP_BASE)), 0.25),
    'dead': (((DEAD, DEAD),), 2.5),
}
