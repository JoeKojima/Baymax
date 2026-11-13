# Activity tracking, Tier 1 (shadow mode)

Records what the person is doing over the day (standing, walking, sitting,
upright with the legs hidden, lying, resting out of sight, not in view) as a
timeline of segments, so the family can later see how the day went. This
first version only **records on the robot** for review in devUI. Nothing goes to the Ember app yet. The plan after this step is Gemini
labels on segment changes (Tier 2), zones drawn in the Ember app, and the
activity section of the shared daily report.

Off unless `BAYMAX_ACTIVITY_ENABLED=1` in the private `.env`. Needs
v10 with pose tracking on (`camera.pose_tracking = true` in
`/etc/ember/device.toml`) and the camera allowed in the Ember app.

## Inputs

- **Pose landmarks from v10's camera loop**, through
  `features/toolkits/fall_detection.py`'s `pose_observers` /
  `notify_pose(landmarks, world_landmarks, frame_shape)`. This is the
  same MediaPipe Pose result fall detection uses; there is **no new
  camera stream and no new model**. Pose tracking off, camera off or no
  camera means no samples, which shows up as a gap, not as "not in view".
- **When the person last spoke**, from `runtime_monitor.transcript_listeners`
  (user transcripts only, no text kept). Speech within 15 s counts as talking.
  A TV can be transcribed as user speech too, so treat "chatting with Ember" as
  a hint.

## Processing

`observe()` runs on the camera thread and only copies the 33 points (at most
5 times per second) into a single slot. The `ember-activity` worker thread does
the rest:

1. `posture.py`: trunk angle (lying when the torso is over 60° from vertical),
   thigh and knee angles (sitting vs. standing), movement in torso lengths per
   second, and a hand within half a shoulder width of the mouth. MediaPipe's
   3D world landmarks are used when present, so a thigh pointing at the camera
   still reads as sitting. Legs hidden = `upright`. One shoulder and the hip
   on the same side are enough when an arm hides the other side; torso
   hidden = no vote.
2. `tracker.py`: a new posture must win at least 70% of the samples for
   8 s (walking), 15 s (standing), 20 s (sitting, lying, upright) or 30 s
   (not in view) before it counts, and the new segment is backdated to when
   the run began. Brief blips stay inside the current segment and lower its
   agreement %. No samples for 10 s closes the segment. Standing with the hips
   travelling more than 0.35 torso lengths/s, net over the last 2 s, is
   walking (net travel, because partly hidden hips jitter in place). A sample
   is "still" below 0.10 torso lengths/s of joint movement. A hand counts as at
   the mouth within 0.5 body-scale units and as gone only beyond 0.8, and only
   while sitting, standing or upright.
   **Policy: record what the visible joints show, never guess at hidden ones.**
   Two attempts to see through occlusion (keeping the last known posture,
   then comparing hip heights) both gave confident wrong labels in the live
   tests. Zones drawn in the Ember app and Gemini labels (Tier 2) add the
   meaning later: "upright" at the table is sitting at the table, "resting,
   out of sight" on the bed is in bed.

   | What is visible | Recorded as |
   |---|---|
   | Full body | `standing`, `sitting`, `lying`, `walking` |
   | Upper body, legs hidden | `upright` (no sitting/standing guess) |
   | Head only, torso hidden | no vote |
   | Nobody, last seen lying in the middle of the view | `out_of_sight` (hint `resting_out_of_sight`) |
   | Nobody, otherwise | `absent` (not in view) |

   `out_of_sight`: when the person was last seen lying with the hips away
   from the frame edges (8%), a blanket or the dark makes pose lose them in
   place; people leave through an edge. It ends when they are seen near an
   edge or walking for 1 s (misread shapes under the blanket don't end it).
   If pose loses someone who gets up and walks straight away from the
   camera, it reads `out_of_sight` until the person is seen again or 20 s of
   another posture wins; the diary comparison should show how often.
3. Provisional labels (`hint`): not in view; resting, out of sight; possibly eating or drinking
   (sitting ≥ 2 min with ≥ 2 hand-to-mouth movements a minute); chatting with
   Ember (talking ≥ 40% of the segment); lying still (≥ 80% still samples);
   otherwise the posture.
4. **Restless nights.** While lying (in view), a *restless spell* is
   sustained movement: at least 70% of the samples over 2 s above 0.5 torso
   lengths/s (turning over, sitting up, fidgeting). Pose jitter (~0.1) and
   single spikes don't count; movement within 60 s of a spell is the same
   spell. Each segment stores its count. Time out of sight can't be measured.

   The **night before a day** (18:00 the day before to 12:00) is summarised
   from the stored segments when devUI asks (`store.night()`): rest is lying
   or resting out of sight; rest stretches less than 45 min apart join one
   block, and the longest block of at least 2 h is the night. Reported:
   bedtime and wake time (start and end of the block), rest time and how much
   of it was out of sight, **get-ups** (breaks inside the block where the
   person was seen up or left the view; a gap without data doesn't count),
   time up, restless spells and spells per hour of visible lying, and a
   provisional **restless night** flag (3 or more get-ups, or 6 or more
   spells an hour). Without zones, dozing on the couch at 21:00 counts as
   rest too; zones (bed vs. couch) will fix that.

All thresholds are first guesses, adjusted by the 2026-11-11 and 2026-11-13
live tests (below). Tune them against a written diary during shadow mode before
trusting them; the restless thresholds have not been checked against a real
night yet.

### Camera orientation

Pose assumes an upright picture. For a robot whose camera is mounted sideways
or upside down, `BAYMAX_CAMERA_ROTATE` (`none`, `90cw`, `90ccw`, `180`) in
`.env` rotates every frame in v10's camera loop, so pose, fall detection, the
MJPEG stream and Gemini all get the upright picture. Gemini's copy keeps its
shape (long side 320 px) instead of being squashed to 320×240. Unit 001's
camera is upright and needs no setting: with a person standing in view, the
hips-to-shoulders direction in the raw image measured 0° (10–90%: -2° to 3°).
A single early reading of a horizontal body had wrongly suggested a sideways
camera; check with a person standing still before setting a rotation.

### Live test, 2026-11-11 (unit 001, GPU pose at 14.9 fps)

The test page started with a 90° rotation applied by mistake (see above) and
had rotation buttons; the steps below appear to have been run with no
rotation, since standing passed, but the active setting wasn't recorded.

| Step | Result |
|---|---|
| Stand | pass (100% standing or upright) |
| Sit facing the robot | pass (72%): legs out of view close to the robot gave "upright"; arms across the body gave "unknown" |
| Sit side-on | segment right (sitting), but only 27% of frames: mostly "upright" with legs out of view |
| Pretend to eat (5 times) | pass, but 8 events counted |
| Walk | pass (100%) |
| Stand still behind a chair | fail: read as walking |
| Lie down | inconclusive (step cut short); lying showed while lying |
| Leave the view | pass (98%) |

Changed afterwards: one-side torso, net-travel walking, hand hysteresis and
posture gating, still threshold 0.04 → 0.10. These changes are not yet
re-tested live. Placement matters: with the legs out of view, sitting vs.
standing can't be told from the legs (see the hip-height rule above, added
after the second test). The person also noted the picture was dark in
moderate room light.

### Live test, 2026-11-13 (unit 001, GPU pose at 15 fps, no rotation)

Run on the code tuned after the first test. Only the steps below were run;
walking and leaving the view were noted as working.

| Step | Result |
|---|---|
| Pretend to eat (5 times) | 4 events counted. Marked a fail because nothing said "eating": the hint needs 2 minutes, so it can't appear in a 40 s step. The 4.4-minute segment around it did get "possibly eating or drinking" (3.0 movements/min), but so did a later 3.7-minute sitting segment with no eating (2.7/min). |
| Lie down under a blanket | fail: pose lost the person and it read "not in view". Fixed (see the re-test below). |
| Stand behind a chair (legs hidden) | fail: 66% "upright", 22% standing; the segment stayed "sitting" from before. Now reads `upright` (policy above). |

Re-test the same day with those fixes (both failed; the visible-joints policy
above replaced them, not yet re-tested):

| Step | Result |
|---|---|
| Sit at a table, legs hidden | fail: became "standing" (56% upright, 19% standing, 19% sitting). Single frames misread as standing moved the hip-height reference to sitting height. Led to the "visible joints only" policy above: this now reads `upright`. |
| Lie down, then a blanket | fail: "lying", then 30 s "not in view", then "lying" (47% nobody found, 30% misread as sitting, upright or standing). Any misread ended the "covered" memory. Now only an edge or walking ends it, and the hidden time reads `out_of_sight`. |

Eating from pose alone is not reliable: real and incidental hand-to-face
movements (phone, face, glasses) came at almost the same rate. The hint stays
in shadow mode so the diary can show how often it is wrong; Gemini's labels
(Tier 2) are the planned way to confirm eating.

Camera exposure was checked the same day: all controls at factory defaults,
mean brightness 110/255 in normal light. Gamma 160 lifted the dark areas
(dark pixels 17% → 2%) without blowing out highlights, and is the setting to
try if the room is dim; gain blew out highlights. No camera setting was
changed.

## Outputs

- `toolkit_activity.sqlite3` next to the code (or under `BAYMAX_DATA_DIR`):
  one row per segment with start, end, posture, hint, agreement, still share,
  mean movement, hand-to-mouth count, talking seconds, sample count and
  restless spells (a column added automatically to older stores). **No
  images, landmarks or transcript text are stored.** The open segment is saved
  every 30 s (a crash or power cut loses at most that), and rows older than
  `BAYMAX_ACTIVITY_RETENTION_DAYS` (default 90) are deleted.
- devUI monitor (`/monitor`): an "Activity (shadow mode)" card with a date
  picker, a "Night before" line (rest times, get-ups, restless spells), time
  per activity, and the segment table, including "No data" gaps.
  It reads the database read-only through `POST /api/activity` (same local
  host, origin and token checks as the rest of devUI).
- Log lines `[ACTIVITY] ...` at start and on errors.

## Dependencies and cost

Standard library only (SQLite). The CPU cost is a few hundred float operations
5 times per second on its own thread; pose itself is already being computed.
This PR also moves v10's pose model to the **iGPU** on Linux x86-64 (see
CHANGELOG), which is what makes 15 fps possible on the LattePanda.

## Data flows

Camera frames and audio still go to Gemini exactly as before; this feature
adds nothing to that. The user decided on 2026-11-11 that activity data may go
to the Ember app. What goes is only the **daily report's activity section**
(below), with the conversation report, through the app link's sync, and only
while the family has reports on and `BAYMAX_ACTIVITY_ENABLED=1`. Segments,
landmarks and images stay on the robot.

## Daily report section

`daily.py` (user request, 2026-11-13: what a nurse would chart on a daily
activity note, or a family would want to know; nothing unreliable; clean and
readable). The report writer (`features/toolkits/family/reports.py`) asks for
it for each day's report and for "Write a report now" previews, and sends it
in the same `day_reports` row as `activity`.

Included, each only when the data backs it:

- **Overnight rest** (the night before, 18:00 to 12:00): from when to when,
  how long, and how often they got up, or "a restless night" at 3 or more
  get-ups. Left out if the camera was off for over 30% of it.
- **Lying down to rest during the day** (20 min or longer): the times, or the
  total if long or frequent.
- **Up and about**: standing or walking with the legs in view, and walking.
  Left out with under 2 h of camera time in the day.
- **Compared with usual**, against the median of up to 14 earlier days (at
  least 5 with enough data), only when clearly different: overnight rest 2 h
  shorter or longer; up at night 3+ times and 2 more than usual; up and about
  under half or over double (and 30 min apart); lying down in the day 2 h+
  and 90 min more than usual.
- A footnote with how long Ember saw them, since other rooms aren't counted.

Left out on purpose: eating, posture shares and agreement scores, "upright,
legs hidden", sitting time, restless spells (not yet checked against a real
night) and falls (not stored; a fall the person mentions already reaches the
family as an urgent alert from the conversation check).

The section is JSON with ready-to-show sentences, for example:

```json
{"version": 1, "seen_min": 630,
 "night": {"from": "...T22:30:00-05:00", "to": "...T06:45:00-05:00", "rest_min": 485, "times_up": 1, "restless": false},
 "day": {"up_min": 90, "walking_min": 20, "rests": [{"from": "...", "to": "...", "min": 60}], "rest_min": 60},
 "lines": ["Rested overnight from 10:30 pm to 6:45 am (about 8 h 5 min). Up once during the night.",
           "Up and about for about 1 h 30 min, including 20 min walking.",
           "Lay down to rest from 2:00 pm to 3:00 pm."],
 "notes": [{"kind": "less_active", "text": "Less active than usual (about 25 min up and about; usually about 2 h)."}],
 "footnote": "Ember saw them for about 10 h 30 min today, from where it sits; time in other rooms isn't counted."}
```

`night` and `day` are absent when the data doesn't back them; the whole
section is absent (no `activity` key) when tracking is off or there is no data.
The site shows `lines`, then `notes`, then the footnote.

## Enabling and disabling

`BAYMAX_ACTIVITY_ENABLED=1` in `.env` and restart Ember. devUI's Save keeps
this line. `0` or absent means the worker never starts and nothing is
written. A store error (disk full, permissions) turns tracking off with a log
line; the conversation and fall detection carry on.

## Tests

`tests/test_toolkit_activity.py` (run by `python check_release.py`): posture
rules on hand-built poses (front and side view, hidden legs or torso, hand to
mouth), segment hysteresis and backdating, blips, gaps, walking, the eating,
chatting and lying-still hints, restless spells against pose jitter, the
night summary (get-ups, gaps, naps, restless flag), the store (day clipping,
updates, crash recovery, pruning, read-only devUI reads, older stores without
the restless column), the worker (disabled, rate
limiting, store failure), hook error isolation, registration, v10's pose
delegate choice and fallback, and the devUI endpoint's date checks.

Live checks still needed on the robot: real postures in front of the camera
(sitting on the couch, at a table, standing, walking past, lying down), the
camera switched off in the app mid-segment, a restart mid-segment, and a
2-3 day diary comparison.
