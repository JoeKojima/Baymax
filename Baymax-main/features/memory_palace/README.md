# Memory Palace

This PR brings the people-awareness and person-linked memory runtime from #8
on top of #12's conversational ability and #11's tools/shared desktop debugger. The v10 runtime preserves
face consent, corrections, forgetting, scoped recall and session resumption.
`ember_self.py`, `person_memory.py` and `face_id.py` remain the implementation;
this package owns the debugging bridge and future graph integration boundary.

A configured profile, including a profile named James, is background context,
not proof of who is currently speaking. Ember no longer assumes a senior home
or that every speaker is the configured profile owner.

The [graph design](DESIGN.md) describes the next stage: connected relationships,
interests, goals and attributed evidence. That graph, its visualization and
automatic personalized feed ranking are not yet implemented.

## Debugging

The shared #7/#11 Setup and Monitor UI is included. `debug.py` connects v10's
transcripts, tool activity, reply state and memory counts to that same monitor.
The Memory Palace card shows the configured profile separately from visible
people and explicitly leaves speaker identity unverified.

On the supported robot/Linux environment, enable `BAYMAX_DEBUG_MONITOR=1` before
starting `realtime_gemini_10.py`. Open `python devUI.py --monitor` separately.
Both processes must use the same `BAYMAX_MONITOR_DIR` (defaults to
`BAYMAX_DATA_DIR`, then the application directory); create that directory first.
Container and host paths must refer to the same mounted directory. Desktop
clickables still launch v8. Experimental Windows v10 support uses the separate
`run_memory_pc.py` launcher described below.
Starting v10 deliberately opens devices and connects to Gemini.

The bridge is disabled by default. It writes local transient status and
transcripts, profile name and visible names for the monitor; it adds no external
service or camera stream. Keep this private runtime data out of Git. Existing
memory extraction and audio/video still use the runtime's configured Gemini
services; text embeddings run locally with the existing ONNX model. Public tools use their documented provider endpoints only when
enabled and called. Face records stay in the existing local people store.

Validation: `python check_release.py` covers offline config, mixed tool routing,
recall and monitor integration. The separate `test/` suite exercises person
memory and identity. Live face enrollment, forgetting, resumed conversation and
monitor operation on the physical robot remain pending. The storage suite needs Chroma. POSIX shared-user lock permissions still require
Linux; Windows storage uses an exclusive byte-range lock in a writable data folder.


## Memory Palace window

In Monitor, select **Open Memory Palace** to open a separate window at
`/memory-palace`. It shows people, shared/unattributed memories, remembered text,
dates, source metadata, and which entries were sent in the initial prompt or
recalled during the current session. Search applies to displayed entries;
filters show shared memories or entries sent to the session. This is a read-only
inspector of the existing store, not the planned inferred relationship graph.

The v10 monitor thread publishes a separate `.runtime-memory.json` snapshot at
most every five seconds, using the existing Chroma collection without opening a
second database client or loading a model. Saving/forgetting takes priority over
inspection. Snapshot reads are limited to 500 entries and marked partial for
larger stores. Pending-forget names/references are suppressed. Old snapshots are
labelled offline. The snapshot includes private memory text; it stays local,
is ignored by Git, and is served only through the existing localhost/Origin/token
protected debugger endpoint. No face measurements, crops or API-key fields are
exported. Turning debugging off stops new snapshots; prior local snapshots remain
until overwritten or removed. Opening the inspector never starts devices/Gemini.

## Spaces, people and persistence

One **Ember instance/data folder** owns a memory space. Each person's palace is
a filtered view into that space; shared memories link several people without
being copied. People links in memories currently use canonical names, whereas
face records have person IDs. These views are not authenticated per-person
privacy boundaries. The graph design describes the eventual stronger ID model.

The Gemini key is a credential, not a memory-space ID. Rotating it retains memory;
using the same key on two installations does not automatically share local memory.
A new process starts a fresh live conversation, then loads saved context. Closing
the debug window or restarting Ember does not erase the store. Finished transcript
lines are persisted until summarized, allowing recovery after a restart; abrupt
power loss can still lose unfinished speech or writes not yet flushed to disk.

Set `BAYMAX_DATA_DIR` to a different directory **before launching both the runtime
and dashboard** for a separate space. The v10 Chroma store, profile, face records
and pending conversation log now consistently use it; unset retains their prior
application-directory defaults. Create the directory first. For example, two
explicit folders named `ember-lab` and `ember-personal` remain independent even
with the same Gemini key. `EMBER_MEMORY_SPACE_NAME` optionally labels the inspector;
it does not select storage. `BAYMAX_MONITOR_DIR` only selects debug output.

Changing the data directory does not migrate old files. To retain an existing
space when changing location, stop Ember and copy its `chroma_store/`,
`ember_profile.json`, `ember_people.json` (including pending forget requests),
`conversation_pending.jsonl` and relevant transcripts into the new directory.
Do not copy those private files into Git. A configured cloud device can still
sync its profile from the cloud, so a fresh local directory does not reset that
remote profile. No automatic per-key reset or data migration is performed.

Inspector tests: `tests/test_memory_inspector.py` covers shared links, unscoped
entries, biometric exclusion, pending forgetting, bounded reads, offline state,
opt-in/throttling, space paths and restart persistence. `tests/test_devUI_http.py`
covers local route access and token/Origin protections. Browser QA uses synthetic
entries only; real Linux/robot database and live recognition testing remain pending.


## Experimental Windows live test

Use Python 3.12 in a separate environment, install `requirements-memory-pc.txt`,
then run `python convert_model.py` once to obtain the existing local ONNX memory
model. The existing speech-analysis model is installed separately with
`python -m spacy download en_core_web_sm`. Face/pose models download and verify
through the existing startup code. Set the external key file and private
`BAYMAX_DATA_DIR` in `.env`, then run:

```powershell
.\.venv-memory\Scripts\python.exe run_memory_pc.py
# From a second terminal in the same folder:
.\.venv-memory\Scripts\python.exe run_memory_pc.py --stop
```

The launcher verifies Google's configured model access before importing devices,
uses the existing PC microphone/speaker/camera adapters, enables the debug bridge
and creates the selected data folder. It refuses production device/email
credentials for local testing. Standard clickables remain v8. The robot/Linux
entry point keeps its existing device behavior. Models and audio rates are unchanged. The opt-in desktop microphone enhancement
is documented in [audio input](../conversational_ability/README.md).

Windows people-store writes now use an exclusive cross-process byte-range lock.
Reads close their handle before JSON parsing; replacements retry briefly when a
Windows reader holds the old file. POSIX keeps its existing flock and read-only
shared-user lock behavior. Chroma test clients are closed before temporary data
is removed so Windows file handles do not cause false teardown failures.

Observed locally on 2026-10-18: Gemini connected, webcam microphone and USB
speaker active, memory retrieval active, face recognition ready, and Ember's
initial audible/transcribed greeting requested a name and face consent. The
Memory Palace window showed a live empty store and an unrecognized person.
Actual enrollment, personalized recall after a restart, forgetting and physical
robot parity remain user tests; no recognition accuracy claim is made.

## Free-tier model policy

The v10 runtime keeps Gemini 2.5 Flash Native Audio for conversation and uses
`gemini-3.1-flash-lite` for summaries, appearance checks and memory rewrites.
`EMBER_FLASH_MODELS` may select exactly one explicit eligible model:
`gemini-3.1-flash-lite` or `gemini-3.5-flash-lite`. Other values, comma-separated
fallback lists and moving aliases are rejected. There is no model fallback.
These services send the same text/images to Google as before; embeddings and
face matching remain local. Existing public tools retain their provider-specific
configuration; this policy covers the v10 Gemini calls, not arbitrary providers.

Use a **Free Tier project** in Google AI Studio for zero charges. Model selection
cannot verify or change project billing, and the same models cost money on a
Paid project. Google can change pricing/availability. See
[pricing](https://ai.google.dev/gemini-api/docs/pricing) and
[billing](https://ai.google.dev/gemini-api/docs/billing).

Background attempts share a five-second pacing gate. HTTP 429 starts a 15-minute
cooldown, doubling on subsequent quota failures up to one hour; temporary server
errors wait one minute. HTTP 404 disables background requests until restart.
Jobs defer through the existing memory worker; pending conversation lines stay
on disk. A process restart resets this in-memory cooldown. This does not promise
unlimited tokens, persist an account-wide quota counter, or change Live API quota
handling. Offline tests: `python -m unittest discover -s tests -p test_model_policy.py`.
