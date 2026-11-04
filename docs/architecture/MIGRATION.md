# Migrating to the ember core

Phases 0–5 are on `svanik/core-rebuild`, one commit per phase. Phase 6, the cutover, is its own branch, `svanik/core-cutover`, stacked on top, so merging the rebuild never changes which core robots run. The old core, `realtime_gemini_8.py`, is unchanged and stays the rollback at every step.

## 1. Order of operations (recommended)
1. **Merge or close the open feature PRs first** (#21, #23–#26, #20/#22, #27, #28). The rebuild touches only `run_pc.py` and `runtime_monitor.py` outside `ember/`, so conflicts are small. But the follow-ups in §4 touch the files those PRs edit.
2. **Merge phases 0–5 with the default unchanged** (`BAYMAX_CORE=legacy`). Robots behave exactly as today. CI runs the parity suite on every PR.
3. **Robot A/B** (§3) with `BAYMAX_CORE=ember` on one robot.
4. **Cutover:** merge `svanik/core-cutover` (phase 6), which flips the default to `ember`. Rollback is `BAYMAX_CORE=legacy` in `.env`, no redeploy.
5. **After a release cycle on ember with no regressions:**
   - change the parity rule (§6);
   - do the follow-ups (§4);
   - start the v9/v10 port (§5).

## 2. Running it
```bash
cd Baymax-main
python -m ember --list                    # profiles
python -m ember --plugins --profile mvp   # the feature registry, in hook order
BAYMAX_CORE=ember python run_mvp.py       # the MVP launcher on the new core (setup, eyes, preflight unchanged)
python -m ember --profile mvp --devices virtual   # no hardware; Gemini still receives the virtual audio/video
python check_release.py                   # includes tests/test_ember_*.py
```
`--devices virtual` sends room-tone audio and a grey frame to Gemini, so it needs a key and uses quota. It's for development and the harnesses, not a privacy-free mode.

## 3. Robot A/B before the cutover

Already done offline: the ember core against real Gemini on virtual devices. It connected, transcribed a synthetic "Hi Ember, how are you doing today?" and replied, then reconnected on a stop request exactly as v8 does. That checks the API, not the hardware.

On one robot, run 30 minutes on each core in the same room, plus #28's harness on both. The cutover needs all of these to hold:

| Check | Pass |
|---|---|
| `[PROFILE] first_audio_latency` p50/p95 | ember within ±10% of legacy |
| `mic_queue_wait`, `send_audio_to_gemini` p95 | within ±10% |
| Echo: Ember answering itself during a 5-minute monologue | no more often than legacy |
| `[AUDIO] Dropped … stale microphone audio` lines | no more than legacy |
| Reconnect: unplug the network for 20 s, then restore | the same recovery and the conversation resumes |
| Restart via `.runtime-stop.json` / Ctrl+C | the same shutdown summary, devices released |
| CPU and memory (`top`, 1-minute average) | within ±10% |
| Eyes: listening, speaking, thinking, sleep states | identical |
| Reminders, audiobook play/stop, weather, proactive greeting | identical |

Record results in the PR using the TESTING_PROTOCOL format. James signs off.

## 4. Follow-ups once the PR train has merged
- **Split `gemini_tools.extend_config`** into one config step per feature in `ember/registry.py` (persona, session, eyes, location/weather, everyday, search, discovery). The config parity test guards it.
- **Remove the import-time `register()` calls** from `audiobooks.py`, `proactive.py` and `everyday/tools.py`, and let the registry install their hooks. `Registry.problems()` and the parity tests guard the order.
- **Move #21's `runtime_monitor.transcript_listeners` to `ember.bus` "transcript"**, and its gate to a `Plugin(input_gate=...)` placed before `audiobooks`.
- **Fix the single-interrupt reconnect** (WHY.md, "v8 bugs found" #2): re-raise the cancellation in `Runtime.run` when the task is being cancelled, and update `SessionParity`.
- **Port the eyes' monitor slots** (`event_observer`, `transcript_observer`) to bus subscriptions, so the slots become optional.

## 5. Porting v9 and v10
v9 and v10 run their own files through profiles `v9`/`v10` (`python -m ember --profile v10`, or `--core legacy`). These are their differences from v8, found by comparing the files, and the plugin each would become:

| Difference | v9 | v10 | Becomes |
|---|---|---|---|
| Different system prompt and base config | ✓ | ✓ (prompt) | a profile `prompt`/`base_config` step |
| Conversation initiation: hazard CDF, `initiation_monitor`, silence gates, `IDLE_THRESHOLD_SECONDS` … | ✓ | ✓ | `Plugin("initiation", session_task=…, subscribe={"transcript": …})` |
| Face visibility from pose landmarks (`_update_face_visibility`) | ✓ | ✓ | `Plugin("face-visibility", frame_observer=…)` |
| Inline FallDetector in the camera thread, plus `fall_alert_monitor` | v9 off | ✓ | the existing fall toolkit (`features/toolkits/fall_detection`) with v10's thresholds; tune them first |
| Memory retrieval per turn and summaries on exit (ChromaDB) | – | ✓ (`MEMORY_RETRIEVAL_ENABLED`) | `features/memory_palace` plugin: a config step for stored memories, a transcript subscriber, a shutdown hook |
| Spectral denoiser and noise gate (`_SpectralDenoiser`) | ✓ | commented out | drop, or an input gate if kept |
| `BAYMAX_DATA_DIR`, `BAYMAX_LOCAL_URL`, `CAMERA_INDICES` | partly | ✓ | `Settings` fields |
| `_TurnTranscript` | – | ✓ | engine detail |
| **No MVP toolkit, session resumption, waiting cue or fall-toolkit hooks** | ✓ | ✓ | v10 robots *gain* these by running on the ember core; that's a product decision, not a refactor |

For each one:
1. Write its plugin.
2. Add a profile that combines it with the right prompt and defaults.
3. Extend `ember/testing/legacy.py` to load that file. v10 also needs ChromaDB and the ONNX embedder stubbed.
4. Add parity tests like the v8 ones.
5. Only then point `deploy/` at the profile.

**Needs James and Joe:** should fleet robots get the MVP features? Should the MVP get memory and initiation? Who validates v10's fall thresholds?

## 6. Changing the parity rule
`tests/test_pc_hardware.py::ParityTests` requires the v8 file's functions to match `upstream/` AST for AST. Keep it while the legacy core is the default or the rollback. After the cutover:
- keep `test_prompt_models_and_sample_rates_match_upstream`, which is about behaviour;
- treat `tests/test_ember_parity.py` as the definition of "the same as v8";
- retire the function-shape check when `realtime_gemini_8.py` is retired.

This is James's call.

## 7. For feature authors (after merge)
```python
# ember/registry.py, in builtin(), at the position your hooks must run
Plugin("my-feature", "One line on what it does",
       enabled=my_feature.enabled,                   # its existing env flag
       load=my_feature.load,                         # optional imports/setup
       config=my_feature.extend_config,              # optional session config step
       input_gate=lazy("features.toolkits.my_feature:gate"),
       session_task=lazy("features.toolkits.my_feature:session_task"),
       subscribe={"transcript": lazy("features.toolkits.my_feature:on_transcript")})
```
- Don't append to the hook lists from your module, and don't take a monitor slot.
- Add tests next to your feature.
- `python -m ember --plugins` shows where your feature sits.
- `tests/test_ember_plugins.py::test_builtin_order_is_exactly_what_v8_imports_register` shows how to assert the order.

## 8. Rollback
- **On a robot:** `BAYMAX_CORE=legacy` in `.env`, then restart.
- **In the repo:** revert the cutover (phase 6) merge. Phases 0–5 are inert while the default is `legacy`.
