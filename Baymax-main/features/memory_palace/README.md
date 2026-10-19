# Memory Palace

This PR brings the people-awareness and person-linked memory runtime from #8
on top of #12's conversational ability and #11's tools/shared desktop debugger. The v10 runtime preserves
face consent, corrections, forgetting, scoped recall and session resumption.
`ember_self.py`, `person_memory.py` and `face_id.py` remain the implementation;
this package owns the debugging bridge and the experimental active evidence graph.

A configured profile, including a profile named James, is background context,
not proof of who is currently speaking. Ember no longer assumes a senior home
or that every speaker is the configured profile owner.

The [graph design](DESIGN.md) describes the next stage: connected relationships,
interests, goals and attributed evidence. A local evidence tree is implemented;
verified person ownership, broader relationship inference and personalized feed ranking remain future work.

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
inspector of the existing store, followed by the experimental active evidence tree.

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
being copied. Legacy Chroma memories link people by canonical names. New graph claims use
the stable person IDs from the face registry under an application-supplied profile. These views are not authenticated per-person
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
`EMBER_FLASH_MODELS`, if set, must be `gemini-3.1-flash-lite`. Other values, comma-separated
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

## Active memory encoder (experimental)

The v10 runtime starts one background worker by default. Disable with
`EMBER_ACTIVE_MEMORY=0`. Every three seconds it reads completed utterances from
the existing durable conversation log, deduplicates them into a SQLite queue,
and processes one job. It never runs inference or database writes on the audio
callback. Jobs survive restart; failures retry with backoff up to three attempts
then remain visibly failed. Unsampled final utterances remain in the existing
pending log and can be ingested at next startup. The worker samples the most
recent 200 pending turns per poll; it is not a complete historical migration.

Immediate extraction is a small **local rule encoder**, not a language model.
The default hybrid mode also schedules Gemini enrichment as documented below.
It recognizes explicit English first-person likes/preferences, interests,
learning and goals. Questions, quoted speech, common hypotheticals and reported
speech are skipped. It is deliberately limited and is not an accuracy guarantee.
Ember's own replies are never extracted as user facts.

Optional `EMBER_MEMORY_ENCODER_MODEL` uses an already installed Ollama model at
`127.0.0.1:11434` with structured JSON, two CPU threads and a 30-second timeout.
No installer or model download runs automatically. This adapter is not yet
benchmarked on the user's machine. Requests bypass environment proxies, reject
redirects and reject cloud model names. It sends completed transcript text to
that local service; its operator must keep it local. No Gemini fallback occurs.
Without this setting, the immediate local pass uses Python standard-library rules.
Hybrid mode separately sends bounded transcript batches to Gemini. Set
`EMBER_MEMORY_MODE=local` to disable that graph enrichment; existing
end-of-conversation Gemini summaries are unchanged by either setting.

The private `memory-palace.sqlite3` in `BAYMAX_DATA_DIR` holds jobs, claims and
source quotes. Graph nodes connect an unverified speaker to categories and
claims; evidence identifies the original utterance. Exact kind/value duplicates with the same polarity
share a node and accumulate evidence. Only bounded, evidence-matching proposals
are accepted. These are **not verified facts** or automatically assigned to a
visible person. Contradictions remain separate for review; there is no automatic
semantic consolidation or confidence score masquerading as a probability.

The Memory Palace window shows worker/queue status and an expandable tree with
source quotes. It publishes at most 100 recent graph claims and five evidence
quotes per claim through the existing protected local snapshot endpoint.
Unattributed graph proposals remain inspection-only. New statements recorded
under a supplied memory profile are eligible for scoped graph recall
as described below. They remain self-reported, not independently verified facts.
The graph uses a separate Chroma vector index; the SQLite graph remains the
source of truth for attribution and deletion.

Forgetting clears **the entire experimental graph**, because ownership is not
verified yet; existing scoped memory forgetting remains unchanged. A persisted
cutoff prevents old pending turns being re-ingested. In-flight extraction checks
this cutoff before committing. While forgetting is pending the graph is hidden
and ingestion pauses. SQLite secure deletion is enabled, but backups/filesystem
snapshots are outside the app's deletion boundary. Private graph files and
sidecars are ignored by Git.

Tests: `python -m unittest discover -s tests -p test_memory_graph.py` covers real
SQLite persistence, deduplication, attribution boundaries, rejected unsupported
claims, retry, and forgetting racing an in-flight encoder. Live local-model
quality/latency, multilingual extraction and multi-speaker attribution remain
pending. See `DESIGN.md` for the next stages.

## Synthetic persona / embedding diagnostic

Run `python -m features.memory_palace.evaluate_persona` from `Baymax-main` with
local ONNX assets installed. The fixture is an explicitly invented UCSF
researcher with piano/classical music, surfing, beginner investing and fresh
fruit interests: 20 natural utterances and five retrieval probes. It never
opens devices, calls Gemini, or reads/writes the real memory space. Graph
extraction runs in a temporary SQLite database that is removed afterwards.
Embedding uses `SemanticEmbedder(chroma_dir=None)` and the existing local
384-dimensional all-MiniLM-L6-v2 model. For offline execution set
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`. Plot output needs matplotlib;
normal runtime memory does not.

Reports go to ignored `.local-review/persona-evaluation/`: JSON results, a
readable HTML report and a PCA scatter plot. `--output` selects another folder.
Clustering uses cosine similarity and average linkage with a preselected 0.40
threshold; no fixed count or topic labels are used to form groups. Labels are
only for scoring. Retrieval embeds five separate queries. This diagnostic
embeds all utterances, including ones the rule encoder misses: it evaluates a
possible semantic organization layer, not a feature already wired into the live
graph worker. It does not silently turn similarity into identity or fact truth.

Observed on the supplied 20-utterance fixture: 9 utterances produced rule-based
claims (music 2/4, research 1/4, surfing 2/4, investing 2/4, food 2/4).
Questions and episodic remarks are included; not all should become personal
facts, so these counts are not a recall/accuracy score. Embeddings yielded seven
pure-topic groups (surfing and investing each split into two), 20/20 same-topic
nearest neighbors and 5/5 correct-topic first retrieval results. This tiny,
hand-authored diagnostic is not a general quality benchmark. Broader extraction,
negation/corrections, ambiguous cross-topic statements and identity need
independent evaluation. Reproduce rather than hard-code these outcomes.

## Hybrid long-term consolidation

`EMBER_MEMORY_MODE=hybrid` is now the default when active memory is enabled.
`local` retains immediate rules/optional local Ollama without Gemini graph
requests; `EMBER_ACTIVE_MEMORY=0` disables the graph worker entirely. These
switches do not disable the existing Gemini conversational or summary services.

One worker continues the local pass and schedules at most one bounded Gemini
batch per 120 seconds. A batch contains up to eight completed user utterances
and 8,000 transcript characters. It can fill immediately at eight entries;
smaller batches wait until the oldest entry is two minutes old. This runs on
the memory worker, never an audio callback or a spoken-response dependency.
Selected transcript text and source IDs go to Google; no face images, database
contents or inferred person identity are included. Schema-constrained output
adds occupation, affiliation and research to the existing interest/preference/
goal/learning categories. Quotes and literal values are checked locally against
the specific source, but these checks cannot guarantee semantic correctness.

The graph keeps positive and negative proposals separately. It does not execute
model-suggested deletes, automatically merge paraphrases, establish verified
biometric speaker ownership. Confirmed self-reported claims can now be recalled
through the local graph index; unconfirmed proposals cannot. A single utterance is not evidence
that all speakers in a batch are the same person.

The runtime reuses `_flash` and its explicit free-tier Flash-Lite model,
shared pacing/cooldown and no-model-fallback policy. A persistent graph budget
allows up to 200 attempted enrichment calls per UTC day **per data folder**;
failed attempts count conservatively. This is not a project-wide quota or cost
cap. A Google Free Tier project is still required. Quota/network/validation
failures leave enrichment pending and retry after five minutes; the shared
provider cooldown can defer the actual network attempt longer. Local extraction
continues between retries. Budget and enrichment markers survive restart.
Forgetting checks run again before committing in-flight cloud output. Clearing
local memory cannot retract transcript batches already sent to Google.

Validation: nine offline hybrid tests cover batching, schema/evidence checks,
provider failure and recovery, persisted budgets, positive/negative evidence,
empty output, legacy database migration and forgetting during inference. The actual Free Tier Flash-Lite
synthetic run used three requests against the same 20 invented utterances:
13 utterances produced proposals versus nine with rules alone; research coverage
rose from 1/4 to 3/4. Questions and episodic remarks are included, so this is not
an extraction recall/accuracy score. The combined graph held 20 proposals,
including overlapping local/cloud phrasings; semantic duplicate consolidation
is still pending. Outputs stayed in an isolated temporary graph and ignored
`.local-review/hybrid-persona-evaluation/`, never the live profile.

## Memory scope and graph-to-conversation retrieval

Memory Palace consumes an application-supplied `person_id` as a storage/retrieval
scope. It does not identify anyone, inspect faces to establish the scope, parse
spoken introductions, enforce single-speaker confirmation, expire identity or
restart the live conversation. Identification and any authenticated identity
handover belong to other features/PRs. Existing face recognition remains its
own feature and is not a prerequisite for the memory path.

The integration boundary is `MemoryContext.set_person(person_id, name=None)` in
`memory_context.py`. Upstream code must supply a stable ID, change it when
appropriate, and clear it when unknown. The optional display name is a label,
not evidence of identity. No ID means future statements remain unattributed and
personal graph recall is skipped. Existing attributed records keep their IDs.

For testing at `/memory-palace`, choose **Use local test profile**, or type a
profile ID and optional label and choose **Use profile**. These select a memory
space within the current data folder; they do not enroll or identify a person.
The local endpoint `/api/memory-palace/context` retains Host/Origin/token checks,
PID-bound commands and a 30-second command lifetime. No enrollment registry or
camera is consulted. The selected scope is in-memory and must be supplied again
after process restart; the graph contents themselves persist.

Each utterance captures the current scope generation when its first fragment
arrives. If the scope changes mid-utterance, that turn stays unattributed.
Gemini never chooses ownership; graph extraction retains each source ID's local
scope. Deduplication includes owner ID and polarity, keeping identical claims
from different profiles separate. Old unattributed entries are not reassigned.

The background worker indexes up to 16 owned claims at a time with the existing
local ONNX embedder and a separate `memory_palace_graph` Chroma collection.
Search uses the supplied ID, rechecks ownership/evidence against SQLite and
omits direct positive/negative conflicts. It sends up to three matches within
cosine distance 0.65, with quotes, status and dates through the existing
`turn_complete=False` injector. Results arriving during a reply are held, and
results from a previous scope generation are discarded. This adds no Gemini
request for searching; consolidation still uses its documented background
budget. The similarity threshold is not a general accuracy guarantee.

Initial prompts do not preload legacy personal memories. In active graph mode,
empty graph results do not fall back to potentially contradictory legacy notes;
old Chroma entries remain inspectable. With the active graph disabled, legacy
recall remains narrowly scoped to the supplied label's sole name link. Legacy
personal greeting/initiation notes are disabled in graph mode.

Changing the memory profile clears only the recent retrieval query window and
rejects pending old-scope results. It does **not** close the Live session, clear
playback, reset its resumption handle or erase context already sent to Gemini.
The identity/conversation coordinator must handle those boundaries if needed.
This test selector is not a privacy or authentication boundary.

Validation: memory-context, indexing, HTTP and actual injector tests cover ID
validation, process-bound commands, separate profile records, stale-index and
stale-delivery rejection, mid-turn scope changes and absence of live-session or
playback resets. Earlier isolated real ONNX/Chroma evidence verifies scoped
retrieval, unattributed exclusion, conflict suppression and forgetting. No
invented facts or identities are inserted into the live store by automated tests.
