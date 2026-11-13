"""The activity section of the family's daily report (user request, 2026-11-13).

What a nurse would chart on a daily activity note, or a family would want to
know, and only what pose measures reliably:

- overnight rest: from when to when, how long, and how often they got up;
- lying down to rest during the day;
- time up and about (standing or walking, legs in view), and walking;
- "compared with usual" notes, only when today clearly differs from the last
  two weeks;
- how long Ember saw them, so the numbers read in context.

Left out on purpose: eating (not reliable from pose), posture shares and
agreement scores, "upright, legs hidden", sitting time (ambiguous with the legs
hidden), restless spells (not yet checked against a real night) and falls
(not stored; a fall the person talks about reaches the family through the
conversation report's urgent alerts). No numbers are given without enough
camera time to back them.

Reads the activity store read-only; never writes. Returns None when there is
nothing to say (activity tracking off, or no data at all), so the report shows
no activity section.
"""
from datetime import datetime, time as day_time, timedelta
import statistics

from .store import RESTING, _read, night_summary

UP = ("standing", "walking")
NIGHT_FROM_H, NIGHT_TO_H = 18, 12
DAY_FROM_H = 6
MIN_SEEN_S = 2 * 3600          # Less time in view than this: no daytime numbers.
MIN_NIGHT_DATA = 0.7           # Share of the night's rest block that must have data.
NAP_BREAK_S = 10 * 60          # Daytime rest less than this apart is one rest.
NAP_MIN_S = 20 * 60            # Shorter lie-downs aren't mentioned.
RESTLESS_GET_UPS = 3           # "A restless night" (get-ups only; restless spells aren't validated).
HISTORY_DAYS = 14
MIN_HISTORY = 5                # Days with enough data before "than usual" is said.
LESS, MORE = 0.5, 2.0          # Up and about below half or above double the usual...
MIN_DIFF_S = 30 * 60           # ...and at least this different.
REST_DIFF_S = 2 * 3600         # Overnight rest this much shorter or longer than usual.
DAY_REST_MORE_S = 90 * 60      # Daytime lying down this much above usual...
DAY_REST_MIN_S = 2 * 3600      # ...and at least this long in total.


def _clip(segments, start, end):
    rows = []
    for s in sorted(segments, key=lambda r: r["start"]):
        a, b = max(s["start"], start), min(s["end"], end)
        if b > a:
            rows.append({**s, "start": a, "end": b})
    return rows


def _total(rows, postures):
    return sum(r["end"] - r["start"] for r in rows if r["posture"] in postures)


def _rests(rows):
    """Daytime lie-downs of at least NAP_MIN_S: [(start, end, resting seconds)]."""
    blocks = []
    for r in rows:
        if r["posture"] not in RESTING:
            continue
        if blocks and r["start"] - blocks[-1][1] <= NAP_BREAK_S:
            blocks[-1][1] = r["end"]
            blocks[-1][2] += r["end"] - r["start"]
        else:
            blocks.append([r["start"], r["end"], r["end"] - r["start"]])
    return [tuple(b) for b in blocks if b[2] >= NAP_MIN_S]


def _at(day, hour, tz, days=0):
    return datetime.combine(day + timedelta(days=days), day_time(hour), tz).timestamp()


def _measure(day, until, tz, path):
    """Numbers for one day, or parts None where the data doesn't back them."""
    n_start, n_end = _at(day, NIGHT_FROM_H, tz, -1), _at(day, NIGHT_TO_H, tz)
    night_rows = _clip(_read(path, n_start, n_end), n_start, n_end)
    night = night_summary(night_rows, n_start, n_end)
    if night:
        block = [r for r in night_rows if r["start"] < night["wake"] and r["end"] > night["bedtime"]]
        covered = sum(min(r["end"], night["wake"]) - max(r["start"], night["bedtime"]) for r in block)
        if covered < MIN_NIGHT_DATA * (night["wake"] - night["bedtime"]):
            night = None   # Camera off for much of the night: say nothing about it.
    start = night["wake"] if night and night["wake"] < until else _at(day, DAY_FROM_H, tz)
    rows = _clip(_read(path, start, until), start, until) if until > start else []
    seen = sum(r["end"] - r["start"] for r in rows if r["posture"] != "absent")
    rests = _rests(rows)
    return {
        "night": night,
        "seen_s": seen,
        "up_s": _total(rows, UP) if seen >= MIN_SEEN_S else None,
        "walk_s": _total(rows, ("walking",)) if seen >= MIN_SEEN_S else None,
        "rests": rests if seen >= MIN_SEEN_S else None,
        "day_rest_s": sum(r[2] for r in rests) if seen >= MIN_SEEN_S else None,
    }


def _usual(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if len(values) >= MIN_HISTORY else None


def _length(seconds):
    minutes = int(round(seconds / 60 / 5) * 5) if seconds >= 600 else int(round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


def _clock(epoch, tz):
    return datetime.fromtimestamp(epoch, tz).strftime("%I:%M %p").lstrip("0").lower()


def _times(count):
    return {0: "", 1: "once", 2: "twice"}.get(count, f"{count} times")


def summary(day, until, tz, path=None):
    """The activity section for the report of `day` (a date) ending at `until` (epoch), or None."""
    today = _measure(day, until, tz, path)
    night = today["night"]
    if not night and today["seen_s"] <= 0:
        return None
    history = [_measure(day - timedelta(days=k), until - k * 86400, tz, path)
               for k in range(1, HISTORY_DAYS + 1)]
    usual_rest = _usual([h["night"]["rest_s"] if h["night"] else None for h in history])
    usual_get_ups = _usual([h["night"]["get_ups"] if h["night"] else None for h in history])
    usual_up = _usual([h["up_s"] for h in history])
    usual_day_rest = _usual([h["day_rest_s"] for h in history])

    lines, notes, out = [], [], {"version": 1, "seen_min": round(today["seen_s"] / 60)}
    if night:
        get_ups = night["get_ups"]
        out["night"] = {"from": datetime.fromtimestamp(night["bedtime"], tz).isoformat(),
                        "to": datetime.fromtimestamp(night["wake"], tz).isoformat(),
                        "rest_min": round(night["rest_s"] / 60), "times_up": get_ups,
                        "restless": get_ups >= RESTLESS_GET_UPS}
        line = (f"Rested overnight from {_clock(night['bedtime'], tz)} to {_clock(night['wake'], tz)} "
                f"(about {_length(night['rest_s'])}).")
        if get_ups >= RESTLESS_GET_UPS:
            line += f" A restless night: up {_times(get_ups)}."
        elif get_ups:
            line += f" Up {_times(get_ups)} during the night."
        else:
            line += " No get-ups seen."
        lines.append(line)
        if usual_rest is not None and abs(night["rest_s"] - usual_rest) >= REST_DIFF_S:
            more = night["rest_s"] > usual_rest
            notes.append({"kind": "more_rest" if more else "less_rest",
                          "text": f"Rested about {_length(abs(night['rest_s'] - usual_rest))} "
                                  f"{'longer' if more else 'less'} than usual overnight."})
        if usual_get_ups is not None and get_ups >= RESTLESS_GET_UPS and get_ups >= usual_get_ups + 2:
            notes.append({"kind": "up_more_at_night",
                          "text": f"Up more than usual during the night ({_times(get_ups)}; "
                                  f"usually {_times(round(usual_get_ups)) or 'not at all'})."})

    if today["up_s"] is None:
        if today["seen_s"] > 0:
            lines.append("Ember only saw them briefly today, so there is no daytime summary.")
    else:
        up, walk = today["up_s"], today["walk_s"]
        out["day"] = {"up_min": round(up / 60), "walking_min": round(walk / 60),
                      "rests": [{"from": datetime.fromtimestamp(a, tz).isoformat(),
                                 "to": datetime.fromtimestamp(b, tz).isoformat(), "min": round(s / 60)}
                                for a, b, s in today["rests"]],
                      "rest_min": round(today["day_rest_s"] / 60)}
        if up < 600:
            lines.append("Up and about only briefly (under 10 min seen).")
        else:
            lines.append(f"Up and about for about {_length(up)}"
                         + (f", including {_length(walk)} walking." if walk >= 300 else "."))
        rests = today["rests"]
        if len(rests) > 3 or today["day_rest_s"] >= 3 * 3600:
            lines.append(f"Lay down to rest for about {_length(today['day_rest_s'])} during the day.")
        elif rests:
            spans = [f"{_clock(a, tz)} to {_clock(b, tz)}" for a, b, _ in rests]
            lines.append("Lay down to rest " + ("from " + spans[0] if len(spans) == 1
                         else _times(len(spans)) + ": " + ", ".join(spans[:-1]) + " and " + spans[-1]) + ".")
        if usual_up is not None:
            if up < LESS * usual_up and usual_up - up >= MIN_DIFF_S:
                notes.append({"kind": "less_active", "text": f"Less active than usual (about {_length(up)} "
                              f"up and about; usually about {_length(usual_up)})."})
            elif up > MORE * usual_up and up - usual_up >= MIN_DIFF_S:
                notes.append({"kind": "more_active", "text": f"More active than usual (about {_length(up)} "
                              f"up and about; usually about {_length(usual_up)})."})
        if (usual_day_rest is not None and today["day_rest_s"] >= DAY_REST_MIN_S
                and today["day_rest_s"] - usual_day_rest >= DAY_REST_MORE_S):
            notes.append({"kind": "more_day_rest",
                          "text": f"Spent more of the day lying down than usual (about "
                                  f"{_length(today['day_rest_s'])}; usually about {_length(usual_day_rest)})."})

    if not lines:
        return None
    out["lines"] = lines
    out["notes"] = notes
    out["footnote"] = (f"Ember saw them for about {_length(today['seen_s'])} today, from where it sits; "
                       "time in other rooms isn't counted." if today["seen_s"] >= 60 else
                       "Ember didn't see them during the day; time in other rooms isn't counted.")
    return out
