# Why rebuild the runtime core (and keep its behaviour identical)

**Audience:** James (software lead), Joe, Natey and anyone adding a feature to Ember.
**Status:** `svanik/core-rebuild` (phases 0–5, the default stays on the old core) and `svanik/core-cutover` (phase 6, which flips the default), not merged. Each phase is its own commit, and the old core stays available as a rollback.

## Summary
Ember's behaviour lives in one 1,579-line file, `realtime_gemini_8.py`, plus two copies of it, v9 and v10. The rebuild moves the same behaviour into small parts that can be tested (`ember/`) without changing what Ember does. Tests prove that by running the new core and the **unchanged** old file side by side on the same virtual devices and the same scripted fake Gemini, then comparing what each one sends, plays, records and reports.

The point isn't neater code. It's that every future change gets cheaper and safer: features plug in instead of editing the core, tests exercise Ember's real code instead of copies of it, and one core can eventually serve every robot.

## What it costs today

| Today | Consequence |
|---|---|
| **Importing the runtime starts the hardware.** At import it picks audio devices, queries PortAudio, builds the echo canceller, creates folders and loads `.env`. | It can't run or be tested without a microphone and speaker. Tests cut individual functions out of the source with `ast` and run them in isolation, and the Layer 1 harness had to copy the receive loop. Nothing tested the whole thing together until it reached a robot. |
| **Shared state across the whole file.** The playback buffer, frames, transcripts, timestamps and about 12 locks are touched from threads and async tasks alike. | Reconnects and races are hard to reason about. One real bug hid in this: the mic queue is created once at import. If the outer "critical error" restart ever runs a second event loop, every reconnect after that fails, and Ember stays deaf until the process restarts. (Verified; the new core creates the queue per loop.) |
| **Features attach on import, into lists in several modules.** `processing.input_gates`/`output_sources`, `fall_detection.frame_observers`/`session_tasks`, the single observer slots in `runtime_monitor`, and `gemini_tools.extend_config`. | The order is real but invisible. "Ember disengage" (#21) has to put its mic gate ahead of the audiobook's, and nothing records that. A second feature that takes `monitor.event_observer` silently disconnects the eyes. Every feature PR edits the same 3–4 files, which is why the open PRs conflict with each other. |
| **Three copied runtimes.** v9 and v10 each differ from v8 by about 1,100 lines. | **The fleet core (v10, launched by Docker and `deploy/host/startup.sh`) has none of the MVP's features**: no toolkit, reminders, audiobooks, eye tools, proactive greeting, session resumption or waiting cue, and it has its own prompt. A fix lands in one copy only. |
| **The parity rule protects the file's shape.** `test_pc_hardware.py` requires v8's functions to stay AST-identical to `upstream/`. | It guards behaviour indirectly, by forbidding structure changes, so the file can only grow. |

## What changes

```
ember/
  settings.py      the core's settings, read once (same variable names and defaults)
  devices/         hardware.py: v8's device selection, streams, PortAudio reset and camera probe
                   virtual.py: a sounddevice-compatible virtual mic/speaker and a virtual camera
  session/
    config.py      MODEL, prompt and base config copied verbatim; built through the registry
    engine.py      Runtime: speaker/mic paths, send/receive loops, connect/reconnect, boot, shutdown
    video.py       camera thread, 3-second frame sender, MJPEG preview
    recording.py   day_utterance recording and retention pruning
    metrics.py     the latency profiles
  bus.py           many-subscriber events ("state", "transcript") published by runtime_monitor
  plugins.py       Plugin + Registry: ordered hooks, config steps, problems()
  registry.py      the built-in features, in the order v8's imports register them
  profiles.py      v8 / mvp on the new core; v9 / v10 run their own files until ported
  main.py          python -m ember --profile mvp [--devices virtual] [--plugins]
  testing/         fake Gemini, and the loader that runs the unchanged v8 on virtual devices
```

`realtime_gemini_8.py`, `upstream/`, the features, the prompts, the models, the voice, the audio rates and processing, the env variable names, file locations, ports and the systemd/startup entry points are **not changed**. `run_pc.py` gains `BAYMAX_CORE=ember|legacy`.

## Why it's better for the future
1. **Features plug in; the core stops changing.** A new feature is one `Plugin(...)` line in `ember/registry.py`: its mic gate, speaker source, frame observer, session task, config step or bus subscription. Its position in the list *is* its order, and `python -m ember --plugins` prints it. `Registry.problems()` reports anything registered outside the list or out of order, so ordering bugs show up in tests, not on a robot.
2. **The real code runs in tests and on any laptop.** `--devices virtual` runs the full runtime with no hardware. CI, the Layer 1 harness and the Layer 2 robot rig can all drive Ember's actual receive loop, reconnect logic and audio cleanup, instead of reimplementing them. We used exactly that to replay a 1011 drop and a reconnect offline.
3. **Behaviour is checked, not inferred from the file's shape.** The parity suite (`tests/test_ember_parity.py`) compares the two cores on:
   - the session config across 7 setting combinations;
   - speaker output sample for sample, with the real WebRTC echo cancellation and the waiting cue;
   - the mic path and recording;
   - the send path's stale-audio and half-duplex drop rules;
   - the receive loop's monitor events, playback, transcripts, tool calls and dashboard posts;
   - the frames sent to Gemini and the camera thread;
   - the files written to disk;
   - whole sessions with a dropped connection and a reconnect.

   The same method extends to v9 and v10.
4. **Features don't compete for slots.** `ember.bus` lets any number of features follow state changes and transcripts. Today the eyes hold the only `event_observer`, and #21 had to add its own `transcript_listeners` list to `runtime_monitor`. With the bus that list isn't needed.
5. **One core for every robot becomes possible.** Profiles separate *what a robot runs* (features, defaults) from *the code that runs it*. Porting v10's differences (memory, inline fall detection, initiation, face visibility) into plugins would give fleet robots the MVP features, and give MVP robots the fleet's, from one codebase. See MIGRATION.md for the inventory.
6. **Smaller reviews.** Reviewing a plugin means reviewing what it hooks into and its tests. Combined with CI (#27), Layer 1 (#28) and Layer 2, James reviews behaviour evidence instead of re-deriving what a 1,500-line diff does.

## What it does not do (yet)
- **No v9/v10 port.** They still run their files through profiles `v9`/`v10`, and that port needs decisions only James and Joe can make (see MIGRATION.md §5).
- **`gemini_tools.extend_config` stays one config step.** Splitting it into per-feature steps would edit the file every open feature PR touches, so it waits until those PRs merge.
- **Feature modules still register themselves on import.** The registry then finds their hooks already present and only checks the order. Removing the import-time `register()` calls is a follow-up after the merge train, for the same reason.
- **No robot evidence yet.** Everything is offline. The cutover needs the robot A/B in MIGRATION.md §3.

## v8 bugs found while rebuilding
Running the unchanged v8 next to the new core showed two v8 bugs:
1. **The mic queue is tied to the first event loop** (above). Fixed in the new core; it's the one intentional difference.
2. **One Ctrl+C (or one `.runtime-stop.json` request) doesn't stop Ember: it reconnects.**
   - **Cause:** asyncio turns the first interrupt into a task cancellation, and `run()` catches `asyncio.CancelledError` around its TaskGroup, then loops back to connect.
   - **Verified:** both cores against real Gemini on virtual devices, via the stop file. Both reconnected (two "Connecting to" lines) and kept running.
   - **Effect:** on desktops it takes a second Ctrl+C to stop and run the shutdown summary. Robots stop with systemd's SIGTERM, which this doesn't affect.
   - **Status:** the new core keeps this behaviour for parity. The fix is to re-raise when the task itself is being cancelled (`asyncio.current_task().cancelling()`), as a separate PR after the cutover, with a parity-test update.

## The one intentional behaviour difference
The mic queue is created for each event loop rather than once at import (the bug described above). It only matters after v8's outer "critical error" restart. In every other path the two cores behave the same, as the parity tests show.
