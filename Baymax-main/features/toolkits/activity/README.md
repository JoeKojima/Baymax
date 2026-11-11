# Activity tracking, Tier 1 (shadow mode)

Records what the person is doing over the day (standing, walking, sitting,
lying, not in view) as a timeline of segments, so the family can later see how
the day went. This first version only **records on the robot** for review in
devUI. Nothing goes to the Ember app yet. The plan after this step is Gemini
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
   still reads as sitting. Legs hidden = `upright`, which keeps sitting or
   standing if that was already known. Torso hidden = no vote.
2. `tracker.py`: a new posture must win at least 70% of the samples for
   8 s (walking), 15 s (standing), 20 s (sitting, lying, upright) or 30 s
   (not in view) before it counts, and the new segment is backdated to when
   the run began. Brief blips stay inside the current segment and lower its
   agreement %. No samples for 10 s closes the segment. Standing with the hips
   moving more than 0.35 torso lengths/s (smoothed) is walking.
3. Provisional labels (`hint`): not in view; possibly eating or drinking
   (sitting ≥ 2 min with ≥ 2 hand-to-mouth movements a minute); chatting with
   Ember (talking ≥ 40% of the segment); lying still (≥ 80% still samples);
   otherwise the posture.

All thresholds are first guesses. Tune them against a written diary during
shadow mode before trusting them.

## Outputs

- `toolkit_activity.sqlite3` next to the code (or under `BAYMAX_DATA_DIR`):
  one row per segment with start, end, posture, hint, agreement, still share,
  mean movement, hand-to-mouth count, talking seconds and sample count. **No
  images, landmarks or transcript text are stored.** The open segment is saved
  every 30 s (a crash or power cut loses at most that), and rows older than
  `BAYMAX_ACTIVITY_RETENTION_DAYS` (default 90) are deleted.
- devUI monitor (`/monitor`): an "Activity (shadow mode)" card with a date
  picker, time per activity, and the segment table, including "No data" gaps.
  It reads the database read-only through `POST /api/activity` (same local
  host, origin and token checks as the rest of devUI).
- Log lines `[ACTIVITY] ...` at start and on errors.

## Dependencies and cost

Standard library only (SQLite). The CPU cost is a few hundred float operations
5 times per second on its own thread; pose itself is already being computed.
This PR also moves v10's pose model to the **iGPU** on Linux x86-64 (see
CHANGELOG), which is what makes 15 fps possible on the LattePanda.

## Data flows

None outside the robot. Camera frames and audio still go to Gemini exactly as
before; this feature adds nothing to that. The user decided on 2026-11-11 that
activity data may later go to the Ember app; that is a later PR.

## Enabling and disabling

`BAYMAX_ACTIVITY_ENABLED=1` in `.env` and restart Ember. devUI's Save keeps
this line. `0` or absent means the worker never starts and nothing is
written. A store error (disk full, permissions) turns tracking off with a log
line; the conversation and fall detection carry on.

## Tests

`tests/test_toolkit_activity.py` (run by `python check_release.py`): posture
rules on hand-built poses (front and side view, hidden legs or torso, hand to
mouth), segment hysteresis and backdating, blips, gaps, walking, the eating,
chatting and lying-still hints, the store (day clipping, updates, crash
recovery, pruning, read-only devUI reads), the worker (disabled, rate
limiting, store failure), hook error isolation, registration, v10's pose
delegate choice and fallback, and the devUI endpoint's date checks.

Live checks still needed on the robot: real postures in front of the camera
(sitting on the couch, at a table, standing, walking past, lying down), the
camera switched off in the app mid-segment, a restart mid-segment, and a
2-3 day diary comparison.
