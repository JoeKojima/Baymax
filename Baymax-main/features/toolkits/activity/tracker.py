"""Turns posture samples into activity segments, with hysteresis. Pure logic, no I/O.

A new posture must win most samples for ENTER_S seconds before it counts; the
new segment is then backdated to when that run began, so the timeline stays
accurate even though the switch is decided late. A gap in samples (camera off,
pose off, Ember stopped) closes the open segment instead of inventing data.
"""
from collections import deque
import math
import uuid

from .posture import HAND_AWAY_FROM_MOUTH, HAND_NEAR_MOUTH, NEUTRAL

ENTER_S = {"absent": 30.0, "walking": 8.0, "standing": 15.0, "sitting": 20.0,
           "lying": 20.0, "upright": 20.0}
START_S = 5.0          # First posture after a start or gap.
GAP_S = 10.0           # No samples for this long = no data.
MIN_SEGMENT_S = 5.0    # Shorter segments (only possible around gaps) are dropped.
SWITCH_SHARE = 0.7     # The challenger must win this share of samples...
DROP_SHARE = 0.5       # ...and is forgotten when it falls below this.
WALK_HIP_SPEED = 0.35  # Net hip travel over WALK_WINDOW_S, torso lengths/s, that counts as walking.
WALK_WINDOW_S = 2.0    # Net travel, not per-sample speed: hidden hips jitter in place (live test:
WALK_MIN_SPAN_S = 1.5  # standing still behind a chair read as walking).
STILL_SPEED = 0.10     # Body movement, torso lengths/s, below which a sample is "still" (0.04 never
                       # triggered live: pose jitter alone is ~0.1).
HAND_POSTURES = ("sitting", "upright", "standing")  # Not walking, lying or out of view.
HAND_AWAY_S = 1.0      # Hand must leave the mouth this long before the next event counts.
TALK_RECENT_S = 15.0   # Speech within this long counts as talking with Ember.
UPRIGHT_FAMILY = ("sitting", "standing", "walking")

EATING_MIN_S = 120.0
EATING_PER_MIN = 2.0
CHAT_SHARE = 0.4
STILL_SHARE = 0.8


STAT_KEYS = ("samples", "still", "moving_samples", "movement", "hand_to_mouth", "talking_s")


def _apply(segment, record, sign=1):
    """Add (or remove) one sample record to a segment's running totals."""
    for key in STAT_KEYS:
        segment[key] += sign * record[key]
    segment["agree"] += sign * int(record["vote"] == segment["posture"])


def hint(segment):
    """Provisional Tier-1 label for a segment (Tier 2 will replace it with Gemini's)."""
    duration = max(segment["end"] - segment["start"], 1e-6)
    posture = segment["posture"]
    if posture == "absent":
        return "not_in_view"
    if (posture in ("sitting", "upright") and duration >= EATING_MIN_S
            and segment["hand_to_mouth"] / (duration / 60) >= EATING_PER_MIN):
        return "possibly_eating_or_drinking"
    if segment["talking_s"] >= CHAT_SHARE * duration:
        return "chatting_with_ember"
    if posture == "lying" and segment["samples"] and segment["still"] / segment["samples"] >= STILL_SHARE:
        return "lying_still"
    return posture


def summary(segment):
    """The stored row: times, posture, hint, confidence and counts."""
    samples = segment["samples"]
    return {
        "id": segment["id"], "start": segment["start"], "end": segment["end"],
        "posture": segment["posture"], "hint": hint(segment),
        "confidence": round(segment["agree"] / samples, 3) if samples else 0.0,
        "still_share": round(segment["still"] / samples, 3) if samples else 0.0,
        "movement": round(segment["movement"] / segment["moving_samples"], 4) if segment["moving_samples"] else None,
        "hand_to_mouth": segment["hand_to_mouth"], "talking_s": round(segment["talking_s"], 1),
        "samples": samples,
    }


class Tracker:
    def __init__(self, on_close):
        self.on_close = on_close   # on_close(summary) for each finished segment.
        self.current = None
        self.last_t = None
        self.walk_speed = 0.0
        self.hips = deque()      # (t, hip, torso) over the last WALK_WINDOW_S.
        self.hand_near = False
        self.hand_away_since = None
        self._reset_challenger()

    def _reset_challenger(self, posture=None, since=None):
        self.challenger, self.challenger_since = posture, since
        self.challenger_yes = self.challenger_total = 0
        self.pending = []   # Sample records since the challenger's run began.

    def _share(self):
        return self.challenger_yes / self.challenger_total if self.challenger_total else 0.0

    def _close(self, end):
        segment, self.current = self.current, None
        if segment is None:
            return
        segment["end"] = end
        if end - segment["start"] >= MIN_SEGMENT_S:
            self.on_close(summary(segment))

    def gap(self):
        """No data from here on (camera off, Ember stopping)."""
        if self.last_t is not None:
            self._close(self.last_t)
        self.last_t = None
        self.walk_speed = 0.0
        self.hips.clear()
        self.hand_near = False
        self.hand_away_since = None
        self._reset_challenger()

    def open_segment(self):
        return summary(self.current) if self.current else None

    def sample(self, t, posture, body_speed=None, hip=None, torso=None, hand_mouth=None, talking=False):
        """One posture sample: posture from posture.classify, or 'absent' when nobody is seen.

        hip is the hip midpoint (x, y) and torso the torso length in the same units;
        hand_mouth is posture.features' hand-to-mouth distance (None when not seen)."""
        if self.last_t is not None and t - self.last_t > GAP_S:
            self.gap()
        dt = 0.0 if self.last_t is None else min(max(t - self.last_t, 0.0), 1.0)
        self.last_t = t

        self.walk_speed = self._walk_speed(t, None if posture == "absent" else hip, torso)
        if posture in ("standing", "upright") and self.walk_speed > WALK_HIP_SPEED:
            posture = "walking"

        state = self.current["posture"] if self.current else None
        vote = state if posture == "upright" and state in UPRIGHT_FAMILY else posture  # Legs hidden.
        moving = body_speed is not None
        record = {"vote": vote, "samples": 1, "moving_samples": int(moving),
                  "movement": body_speed if moving else 0.0,
                  "still": int(moving and body_speed < STILL_SPEED),
                  "hand_to_mouth": self._hand_event(t, hand_mouth, posture in HAND_POSTURES),
                  "talking_s": dt if talking else 0.0}
        if self.current is not None:
            _apply(self.current, record)
            self.current["end"] = t

        if vote not in (NEUTRAL, state) and (
                self.challenger is None or (vote != self.challenger and self._share() < DROP_SHARE)):
            self._reset_challenger(vote, t)
        if self.challenger is None:
            return
        self.pending.append(record)
        self.challenger_total += 1
        self.challenger_yes += int(vote == self.challenger)
        needed = START_S if self.current is None else ENTER_S.get(self.challenger, 20.0)
        held = t - self.challenger_since
        if self._share() < DROP_SHARE or held > 3 * needed:
            self._reset_challenger()
        elif held >= needed and self._share() >= SWITCH_SHARE:
            self._switch(t)

    def _switch(self, t):
        start, pending = self.challenger_since, self.pending
        if self.current is not None:
            for record in pending:
                _apply(self.current, record, -1)
            self._close(start)
        segment = {"id": uuid.uuid4().hex, "posture": self.challenger, "start": start, "end": t,
                   "agree": 0, **{key: 0 for key in STAT_KEYS}}
        for record in pending:
            _apply(segment, record)
        self.current = segment
        self._reset_challenger()

    def _walk_speed(self, t, hip, torso):
        """Net hip travel over the last couple of seconds, in torso lengths per second."""
        if hip is None or not torso:
            self.hips.clear()
            return 0.0
        self.hips.append((t, hip, torso))
        while t - self.hips[0][0] > WALK_WINDOW_S:
            self.hips.popleft()
        span = t - self.hips[0][0]
        if span < WALK_MIN_SPAN_S:
            return 0.0
        mean_torso = sum(entry[2] for entry in self.hips) / len(self.hips)
        return math.dist(self.hips[0][1], hip) / mean_torso / span

    def _hand_event(self, t, distance, counts=True):
        """1 on a new hand-to-mouth movement, else 0. Near below HAND_NEAR_MOUTH,
        away only beyond HAND_AWAY_FROM_MOUTH, so a hand hovering at the line counts once."""
        if distance is not None and distance < HAND_NEAR_MOUTH:
            near = True
        elif distance is None or distance > HAND_AWAY_FROM_MOUTH:
            near = False
        else:
            near = self.hand_near
        event = 0
        if (near and counts and not self.hand_near
                and (self.hand_away_since is None or t - self.hand_away_since >= HAND_AWAY_S)):
            event = 1
        if not near and (self.hand_near or self.hand_away_since is None):
            self.hand_away_since = t
        self.hand_near = near
        return event
