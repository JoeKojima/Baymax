# Live test: people-awareness and memory (on the robot)

The automated tests fake Gemini's replies. This ~30-minute checklist confirms
that Ember actually *behaves* well with live Gemini. Two people are needed:
**A** (plays the person Ember cares for) and **B** (a family member).

Watch the AI core's console while testing — every step logs what Ember was
told: `[SELF]`, `[FACE]`, `[ARRIVED]`/`Greeting`, `[MEMORY]`, `[TOOL]`, `[INIT]`.

## Setup (once)
1. Run `realtime_gemini_10.py` and `baymax_app.py` as usual. On first boot the
   face models (~39 MB) download into `face_models/`; the boot screen shows
   "Loading face recognition".
2. In the dashboard → **Profile**: set A's name, a line about them, and add B
   with relationship (e.g. "Sarah — daughter"). Save.
3. Still in Profile → **People Ember recognizes**: add 3–5 clear photos of A,
   tick the permission box. A appears as "the person Ember cares for".
4. Optional, to save waiting: in `face_id.py` temporarily set
   `AWAY_OTHERS = 60` and `AWAY_PRIMARY = 120`, and in `person_memory.py`
   `CONVERSATION_GAP = 30`. **Put them back afterwards.**

## Checklist
| # | Do this | Expect | ✓ |
|---|---|---|---|
| 1 | A sits in front of Ember | Greets A **by name** within a few seconds. `[SELF]` says "A is in front of you". | |
| 2 | A asks "who are you?" and "what can you do?" | Says it's Ember, a companion robot; lists only real capabilities; says it can't call 911 or read small print. | |
| 3 | B joins and stays ~5 s | Ember introduces itself to B, asks B's name **and** whether it may remember B's face. Nothing is saved until B says yes (`[TOOL] remember_person` only after the yes). | |
| 4 | B says yes, gives their name | Ember uses B's name; `[SELF]` says "A and B (A's daughter) are in front of you". | |
| 5 | B mentions a plan ("I have a job interview Friday"); A mentions something about themself | Natural conversation. After ~3 min of quiet (or the shortened gap), `[MEMORY] +` lines appear, each listing the right people. | |
| 6 | B leaves, comes back after the away time | Ember greets B by name and **asks about the plan** ("How did the interview go?") — doesn't recite a list. | |
| 7 | B changes something visible (glasses off, a hat, a bright scarf) and returns | Greeting or a later opener may mention it kindly. Never comments on weight, body, skin or age. | |
| 8 | Everyone stays quiet ~2 min while in view | Ember starts a conversation **with the person by name** — about their look today or something they told it. Not "the user". | |
| 9 | A third person (not family) is introduced; then ask Ember about A's health | Ember is friendly but keeps A's personal matters general. | |
| 10 | Someone new declines ("no, don't remember me") | Ember accepts; doesn't ask again during the visit; nothing saved (check the dashboard). | |
| 11 | B says "please forget me" | Ember confirms; B disappears from the dashboard; `[MEMORY] Forgot B` shows memories deleted/rewritten. | |
| 12 | Restart the AI core mid-conversation (or `systemctl restart`) | On restart: "[CONVO] N unsaved lines from last time will be remembered", and they're saved. | |
| 13 | A and B talk at the same time to Ember | Known limitation until voice recognition: Ember may not know who spoke — it should ask or answer both, not guess wrongly with confidence. | |

## Also note
- Response delay when someone walks in (greeting latency), and CPU use
  (`top`) with face recognition running — the 2 fps face thread plus pose
  tracking on the LattePanda.
- Any wrong name, wrong person linked to a memory, or awkward phrasing —
  copy the console lines around it into the PR.
