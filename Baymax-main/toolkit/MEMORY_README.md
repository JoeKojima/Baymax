# Desktop memory testing

The original desktop conversation remains version 8. Setup now offers an
explicit **Memory testing (v10)** option that runs `realtime_gemini_10.py`, with
the same memory service and timing implementation as the robot development
checkout. Both remain behind `run_pc.py`'s key/model preflight and use the
existing selected PC audio/camera adapters. The robot's service files are not
changed. Mac results do not establish LattePanda performance.

## Use it

1. Run **1 - Setup** once after applying the integration. It installs the added
   memory dependencies, runs offline checks, and prepares the official MiniLM
   ONNX model. The updated Setup/Monitor use local port 8767 to avoid attaching
   to an older pre-integration Setup server.
2. In the new Setup page select **Memory testing (v10)**. Enter a fictional
   test-person ID such as `demo01`. Save your key/devices and memory settings.
3. Leave experiments off for an uncached baseline. Enable **Record local latency
   measurements** to collect timings, then Save.
4. While Ember is stopped, click **Add fictional demo memories**. This writes
   six deterministic fictional records only into that ID's separate Chroma
   directory. Repeating it updates the same IDs, without duplicates.
5. Enable **Reuse results for identical memory searches**, Save, then click
   **Check memory retrieval**. It runs the same local query twice. With demo
   records loaded, expect `chroma → exact_cache`. No capture or Gemini call is
   made by either memory button. These are mechanism checks, not patient-wait
   benchmarks.
6. Use **2 - Monitor → 3 - Start Ember**. Monitor must show **Memory testing
   (v10)** and **Memory ready**. It displays last lookup source/time and cache
   counts. Only Start begins the normal microphone/camera conversation.
7. Use **4 - Stop Ember** to finish and save summaries. Change settings, then
   start a fresh process for each comparison or patient switch.

The foreground query still uses its existing rolling 30-word snapshot. Repeating
a spoken question does not necessarily produce the same query snapshot. Use the
local retrieval check for an exact-hit mechanism test and traces/replay for
representative workload comparisons. Connection-time facts may already be in
Gemini's prompt; a correct answer alone does not prove a cache hit.

## Inputs, outputs, limits

Configuration is saved locally alongside the existing device settings; API keys
are not returned by settings or status endpoints. The runtime selector and
independent trace/exact/comparison/prepared/prediction switches default off or
to the original desktop version. The selector is immutable within a process.
Memory mode writes a private `.memory-desktop.toml` and uses:

```
Baymax-main/.memory-data/patients/<test-person-id>/chroma_store/
Baymax-main/.memory-data/patients/<test-person-id>/transcripts/
Baymax-main/.memory-data/latency/timeline.jsonl
```

An empty ID disables personal-memory reads/writes. The legacy shared DB is not
assigned or migrated. Desktop memory mode disables robot fall alerts and pose
processing; camera/audio still follow the selected devices. The original
desktop's location/weather toolkit remains available in its original version;
those tools are not loaded by the memory-testing core. Each core keeps its own
existing prompt, model names and audio rates.

Prepared serving remains calibration-gated. Comparison mode returns Chroma
results; prediction is a separate opt-in and is never awaited by foreground
retrieval. See `docs/early-memory.md` for cache budgets, revision guards,
throttling, accuracy and rollout gates. Traces exclude transcripts, memory
texts/IDs, audio and patient names. The existing local conversation monitor
does show transcripts, as before; it is separate from the timing exporter.

## Dependencies and external data flows

`requirements-memory-pc.txt` adds the existing robot MiniLM/Chroma stack,
OpenTelemetry, and pytest for the memory review suite. Setup downloads public
`sentence-transformers/all-MiniLM-L6-v2` ONNX/tokenizer files from Hugging Face
and records their resolved revision. There is no model export step. The local
demo and retrieval buttons make no Gemini requests or device capture.

Start sends normal audio/video to Gemini and records session audio locally.
Enabling early preparation adds separate Gemini requests containing the bounded
recent conversation context, using the existing summary model. Shutdown saves
summaries via that model. No OpenAI model/client or cloud telemetry exporter is
added to this feature.

## Validation

Run `python check_release.py --report .local-review/memory-desktop.md` from
`Baymax-main`. It requires desktop core, toolkit and memory checks to pass.
Missing/failing memory tests cannot be silently ignored by the unittest gate.
Tests simulate devices/providers, verify v8/v10 routing, settings privacy and
atomicity, isolated demo data, monitor counters, Mac device selection, disabled
camera behaviour, and the existing memory suite.

Live checks remain pending until performed deliberately: key rejection before
capture, saved devices, audible multi-turn conversation, transcript/monitor
updates, stop/restart, personal-memory save/reload, comparison of flags within
v10, and calibration/performance on the actual LattePanda. Review approval is
required before a shared merge or robot deployment; local checks grant neither.
