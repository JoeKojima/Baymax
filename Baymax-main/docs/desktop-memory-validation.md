# Desktop memory integration validation

The extracted desktop installation has been updated in place. Changed source
files are backed up under its `.memory-integration-backup/` directory. The
installer's explicit manifest excludes `.env`, environments, recordings,
downloaded models and existing databases.

The installed Python 3.12 environment now contains the pinned memory
dependencies, and Setup downloaded the official MiniLM ONNX model. The
existing system Python is unchanged. Original desktop mode still launches
version 8; the new explicit memory-testing option launches version 10 through
the same key/model preflight and saved-device selection.

Completed offline and native checks:

- Desktop core, toolkit and memory automated review suites pass. The latest
  installed-copy report is `Baymax-main/.local-review/memory-desktop-final.md`.
- Actual MiniLM/Chroma lookup: first source `chroma`, identical-query source
  `exact_cache`, with one embedding and one database search across both calls.
- The real demo-seeding/retrieval functions return three relevant records from
  an isolated fictional profile; an unseeded second profile returns none. The
  legacy shared database is untouched.
- The real prepared-cache calibration tool passes its fictional development
  and heldout cases. Heldout transcripts differ from development transcripts;
  duplicate conversations across splits are rejected. This is a demo-corpus
  result, not representative patient validation, and serving is not enabled.
- The updated Setup page has been opened and its Memory testing controls
  verified. Subsequent manual testing exercised Mac microphone/speaker
  interaction, live Gemini conversation and the fictional-memory retrieval
  check. The check returned `chroma` followed by `exact_cache`.
- The latest offline gate passed all 133 checks (72 core, 18 toolkit, 43
  memory). A controlled real MiniLM/Chroma test measured 1.939 ms median
  uncached retrieval versus 0.088 ms for warmed exact hits. This was a local
  repeated-query test, not a patient-wait or LattePanda benchmark.

Full patient-wait comparisons, camera/other-platform checks, prepared serving
on representative conversations and loaded LattePanda/stability gates remain
pending. Seven latest live lookups all searched Chroma, and their patient-wait
associations were excluded; conversational latency improvement is unproven.
Shutdown summary saving failed with a model-availability 404; live prediction
uses the same setting and has not been validated. All experimental switches
retain disabled defaults. Selecting the new option in an unsaved Setup form
does not change the saved configuration.

Follow `toolkit/MEMORY_README.md` for the short testing procedure.
