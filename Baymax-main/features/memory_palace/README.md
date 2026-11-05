# Memory Palace

**OPI4 package status:** these libraries are bundled but dormant. The OPI4
runtime does not register their workers, encoders, recall or inspector hooks.
No saved data is included. See `../../../docs/OPI4_FEATURES.md` and the source
snapshot provenance in `OPI4_PACKAGE.json`. The description below belongs to
the PR #13 memory work; it is not a claim of active OPI4 integration.

## Human-memory organization

The numbered folders describe the existing data flow, not a strict execution
schedule. Recall and forgetting can happen at any time. Gemini Live retains the
current conversational working memory with sliding-window compression and session
resumption in `realtime_gemini_10.py`; `person_memory.ConversationLog` remains the
local source transcript buffer. The bounded vector cache below complements Gemini context;
it does not recreate its conversational reasoning.

| Order / folder | Purpose and implementation |
| --- | --- |
| `_01_short_term/` | Capture scoped interaction evidence and queue behavioral events: `memory_context.py`, `interactions.py`, `behavior.py`, `recent.py`. Profile context is not speaker authentication. |
| `_02_encoding/` | Extract explicit claims and optionally enrich batches: `local.py`, `hybrid.py`, `model_policy.py`. Existing validation, models and quotas are preserved. |
| `_03_long_term/` | Persist claims, source evidence and repeated observations: `graph.py`. A stored claim is not automatically a verified fact. |
| `_04_consolidation/` | Organize already-stored evidence: `sweeper.py`. Current consolidation produces groups and association/date suggestions; selective short-to-long-term promotion is future work. |
| `_05_recall/` | Search local vectors, check evidence and format bounded conversational context: `retrieval.py`, `runtime_context.py`, `notes.py`. |
| `_06_forgetting/` | Explicit graph deletion and existing raw-event age/count retention: `retention.py`. Automatic long-term claim decay is future work; legacy-store forgetting remains with `person_memory.py`. |
| `diagnostics/` | Inspector, debug bridge and offline persona evaluation; outside the memory lifecycle. |

Storage currently precedes the sweeper, so long-term storage is stage 03 and
consolidation stage 04. These names are an analogy, not biological claims or new
memory behavior. Imports use `features.memory_palace._XX_purpose.module`;
old flat module paths have been removed. Runtime callers, tests and evaluation
commands use the new paths. No saved-data paths or database formats change.

Inputs are existing transcripts, supplied profiles, tool observations and stored
vectors; outputs remain local evidence, inspection snapshots and compact recall
notes. Dependencies, external provider flows and enable/disable switches remain
as documented below. Run `python -m unittest discover -s tests -p "test_memory*.py"`
and the required `python check_release.py` from `Baymax-main`.

## Flexible associations and display hierarchy

The sweeper exports the full average-link hierarchy under `sweep.hierarchies`,
computed from the existing local ONNX vectors within each source scope. It keeps
all merge levels and their continuous similarity scores instead of stopping at
0.40. Automatic display groups follow the largest gap in the observed merge
scores; no permanent category is assigned to a memory. This heuristic can still
make poor groups and is not a model of biological memory. The older standalone
persona diagnostic retains its explicit benchmark cutoff; it does not set the
runtime hierarchy. Retrieval's separate similarity/evidence filters are unchanged.

The same implicit vector association graph also exports a bounded sample of
weighted positive links, at most two per note / 80 total, without the former 0.70
cutoff. These links can cross displayed group boundaries. Weights affect line
visibility/width; they are not truth confidence. Missing/invalid vectors and broad
conversation summaries stay outside the hierarchy. Scopes remain isolated. The
working set is still bounded at 200 sources, with no extra inference/provider call.

The debugger's **Hierarchy detail** control selects an automatic, broader or finer
cut of the saved hierarchy. Depth 3 adds immediate subgroups, not source bubbles.
Display selection/filtering does not rewrite claims, provenance or association
weights. Labels quote representative notes; this does not establish semantic
category names or “is-a” relations. Context-specific learned reweighting and saved
user corrections to associations are not implemented; the current control changes
only the view. Older snapshots retain their exported group fallback.

Sources compile under each note's **Sources & metadata** disclosure. Case/duplicate
consolidation retains original source IDs, distinct quotes, observation dates,
profile attribution and per-source interaction metadata. Collapsed legacy copies
also collect their visible source records under their one note. Snapshot display
limits do not delete originals. Source nodes are absent at every graph depth.

The synthetic validator generates `debugger.html` with actual sweep hierarchies and
weighted links, disables runtime API access and marks its isolated/offline state.
Tests in `test_memory_clustering.py` and `test_memory_network.cjs` cover adaptive
cuts, coarser/finer views, nested subgroup edges, weighted associations, scope
boundaries, stable IDs, and compiled provenance without graph evidence branches.
Live audio contention and general association quality remain unmeasured.

## Metadata-preserving consolidation

`_04_consolidation.claims.save_claim` consolidates equal claim values after case
and whitespace normalization, only within the same supplied owner, category and
polarity. It preserves punctuation, qualifiers and dates. ONNX neighbors remain
suggestions; no semantic paraphrase merging or automatic fact verification is
introduced. Unattributed proposals remain unverified and are never personal recall.

A canonical claim retains all supporting source IDs. Original transcript/jobs,
owner attribution, timestamps and per-interaction feelings/timing stay separate
and unchanged. `evidence_quotes` preserves distinct supporting quotes, including
when several normalized aliases share a source; it is backfilled non-destructively
from older evidence on opening the store. First/last-seen bounds are calculated
from source observation times, including evidence arriving out of order. The
snapshot includes total source count and the latest five source records with
metadata and up to five quotes each; those are display limits, not deletions.
Repeated processing of one source does not create independent reinforcement.

Matching pre-existing claim aliases consolidate when that claim is next saved.
This is transactional and preserves the oldest existing canonical claim ID.
Obsolete vectors are rejected by SQL checks immediately and removed in bounded
batches by the index worker. Derived sweep results are invalidated on claim saves;
inflight sweeps also check a claim revision before committing. Explicit forgetting
clears quote provenance as well as existing evidence and short-term cache state.
New source IDs include supplied profile scope; compatible older source IDs remain
usable without replay duplication. Different people, polarity, qualifiers and
dated values are kept separate. Similarity cannot override these checks.

This changes local persistence only, adds no provider calls and does not alter
model settings or the live reply path. Existing active-memory switches apply.
`tests/test_memory_consolidation.py` covers source metadata/quotes, old-store
migration, scoped source identity, chronology, conflict suppression, stale-vector
cleanup, bounded snapshots, sweep invalidation, forgetting and rollback on failure.

## Local short-term vectors

`_01_short_term.recent.RecentMemory` reuses the loaded ONNX embedder for the most
recent 128 attributed user turns within 30 minutes. The existing background
worker reads the persisted graph jobs and embeds at most eight unseen turns per
pass, after indexing. No inference is added to transcript callbacks, audio
callbacks or Gemini's reply loop. There is no second model instance, new provider
call, schema change or cloud fallback. It uses the same local ML dependencies and
CPU; background scheduling does not establish zero audio contention.

The cache keeps source IDs, original text, profile scopes, timestamps and vectors
in RAM. A turn is embedded once while retained. Expiry/cap eviction removes cached
vectors, not the existing transcript/job evidence; these are separate retention
policies. Restarting clears the cache, which can rebuild from eligible recent
jobs. `EMBER_ACTIVE_MEMORY=0` disables its worker with the graph. If embeddings are
unavailable it stays disabled; inference failure defers optional cache work.

The 60-second sweeper compares cached vectors with other recent turns and bounded
saved-memory candidates. It reuses vectors and makes no model/provider call.
Recent duplicate groups and similarity/difference hints stay in the cache in RAM;
they are excluded from SQLite `sweep_results`, exported runtime snapshots and
Gemini context. The existing network view does not render recent-turn nodes.
The private runtime snapshot exports cache health/counts, not its text or vectors. Cache changes/expiry invalidate its derived hints;
forgetting clears it and rejects inflight embeddings/results. Short-term state
includes count, capacity, lifetime and deferred status. Long-term source evidence,
existing enrichment frequency and recall selection remain unchanged. Similarity
is a candidate relationship, not factual equivalence; negation flags are narrow
text cues, not general contradiction detection. Inputs longer than the embedder's
256-token limit are truncated for vector computation; original evidence remains.

Validation: `tests/test_memory_recent.py` covers bounded batches/capacity, expiry,
owner isolation, vector reuse, invalid output/retry, source preservation and
forgetting during inference. The sweeper's recent results refresh even when its
durable-source fingerprint is unchanged. No token savings or live audio latency
improvements have been measured for this change.

## Behavioral events

The v10 memory runtime now records compact behavioral events locally. Completed
user turns and assistant transcripts have stable IDs; user responses can link to
the previous same-profile assistant transcript. Existing public-tool calls record
start, completion/failure and elapsed time without their arguments or results.
The event queue does not call a model, embed text, write disk or wait for storage
on the conversation path. It uses `put_nowait`, holds at most 512 entries and
drops optional telemetry on overload. This adds small allocation/serialization
work, not a claim of zero latency. No audio callback, VAD, model or prompt changes.

The existing background worker drains at most 128 events per pass into the local
graph SQLite database. Recent unflushed events can be lost on shutdown; failed
batches are dropped rather than retried on the reply path. Snapshot health exposes
queue depth, drops and storage failures. `EMBER_BEHAVIOR_EVENTS=0` disables runtime
capture. Raw events are pruned on batch writes after 30 days and capped at 20,000.
No private text, audio or satisfaction scores enter event payloads. Ordinary
conversation evidence remains in its existing stores and retention policies.
Forgetting clears persisted events and rejects queued pre-forget events.

`BehaviorRecorder.emit(kind, context, payload, ref_id=..., topic_id=...)` is the
adapter API for actual `offer_presented`, `action_started`, `action_stopped`,
`action_skipped`, `action_replayed`, `item_saved` and `item_dismissed` events.
An offer ID can be passed as the reference of a later observed action; references
are validated against the stored owner. A returned ID means queued, not durable.
Use opaque item/topic IDs and explicitly whitelisted numeric measurements such
as `played_ms`. Emit an offer only when the adapter confirms it was actually
presented; an assistant transcript is not proof of delivery or recommendation.
No current music player reports playback outcomes: those fields remain absent
until its adapter is instrumented. No regex guesses offer acceptance or enjoyment.

The bounded `graph.behavior` debug snapshot includes event counts, recent linked
events, observed-offer denominators and offers followed by actual action starts.
It also includes per-runtime-session turn counts/elapsed span and repeated claim
source/day counts. Runtime sessions are not conversation boundaries; linked
responses are not topic-specific depth or satisfaction. Timing still uses
transcript-arrival proxies, not actual speech duration, playback-end latency or
speaking speed. Topic initiation and exact media completion remain unmeasured.
The inspector's collapsed **Observed behavior** section respects the selected
profile. These measurements are not yet used by a learned recommendation ranker
and are never added wholesale to Gemini context. No new provider calls are made.

Offline coverage: `tests/test_memory_behavior.py` covers nonblocking overflow,
owner isolation, references, deduplication, persistence, forgetting, retention,
observed offer denominators and storage failure. Live speech performance remains
a separate measurement; installing this code does not restart an active session.

The [interactive architecture overview](../../../docs/memory-palace/index.html)
is the living presentation for this feature. Update its affected stages in the
same change as pipeline behavior; see [maintenance guidance](../../../docs/memory-palace/README.md).

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
The inspector reads both the store's `|Name|Name|` person links and older JSON
lists. These describe whom a saved memory involves, not who is currently speaking.
Empty person links remain unattributed. This decoding is local and adds no model calls.

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

Run `python -m features.memory_palace.diagnostics.evaluate_persona` from `Baymax-main` with
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
person identification. Profile-scoped self-reported claims can be recalled
through the local graph index; unattributed proposals cannot. A single utterance is not evidence
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

## Person network view

Selecting a person scopes both the bubble map and saved-note list. Depth 1 shows
person/theme links, depth 2 adds memories, depth 3 adds up to two evidence quotes
per claim (or the saved original note for older summaries). A saved summary is
not an original utterance. Shared notes retain links to everyone involved; claims
require a matching nonempty profile ID. Inspection never changes the active
conversation's storage profile.

Colors identify local, keyword-based theme groups (music, work/research, food,
outdoors, markets, wellbeing and other). A note may link to multiple themes.
These are UI groupings, not stored semantic assertions, emotions or identity.
Click a theme to focus it; Reset view clears focus and pan/zoom. Drag bubbles or
the background, scroll or use +/- to zoom. Keyboard Enter/Space activates nodes;
the depth slider and zoom buttons also work without a pointer. Full text/status,
dates, record IDs and source evidence remain available below the map.

At most 40 newest matching memory records are drawn within the existing snapshot
limit, plus theme/source nodes. A deterministic bounded spring layout runs on
changes, not continuously. No CDN, external graph service, model calls or new
vectors are needed. Person filters, context/shared filters and evidence search
remain available. Empty person views never acquire unowned graph claims.

## Compact notes and model context

`notes.py` derives readable compact notes without changing stored source text.
It removes a repeated subject only for a single explicitly linked person, removes
old summary boilerplate, and shortens written dates. Explicit years become ISO
dates; missing years stay missing. Negation, skill levels, uncertainty and shared
names remain intact. Older saved entries receive a derived `note` in snapshots;
new summary prompts request succinct fragments and ownership metadata. Originals
remain accessible under Original wording / Original text & evidence.

Graph recall sends at most three unique compact category/value JSON notes, with
ISO observation dates and `not:true` for negative claims. One shared profile header
labels their scope. Full quotes/status strings and repeated profile names are not
sent for routine recall. The injector limits note bodies to 900 characters per
injection across graph/legacy paths, omitting whole oversized notes rather than
truncating qualifiers. This is a character limit, not a tokenizer quota. Existing
session deduplication, conflict checks and profile-generation checks still apply.
Storage/evidence is unchanged. Graph-present recall still does not fall back to
legacy summaries after an empty graph result.

The change adds no model call or audio dependency; the asynchronous local search
path stays the same. New formatting applies after restarting v10. Gemini already
has any previously injected context until its existing session is replaced or
compressed; formatting cannot remove it retroactively. On a three-note synthetic
fixture the note bodies shrink from 700 to 147 characters (79%); this is not a
measured Gemini token reduction, whole-session saving, or latency benchmark.

Validation: `node tests/test_memory_network.cjs` covers person isolation, shared
links, evidence search, filters, node bounds, script syntax, depth/focus and layout
stability. `python -B check_release.py` includes compact-note tests covering
polarity, qualifiers, date ambiguity, source preservation and context size.
Isolated ONNX/Chroma + real-injector checks use synthetic data. Browser checks
exercise Natey, depth controls, theme focus, evidence and zoom. Private artifacts
stay in ignored `.local-review/`.

### Readable graph labels and dates

Only person/theme labels are persistently visible by default. Note labels appear
on hover, keyboard focus or selection; the Show short note labels checkbox can
show them all. Captions are bounded extractive snippets, not rewritten memory
facts. Sources expand under their note with observation dates and metadata.
Depth 3 adds hierarchy subgroups; it never adds source branches. Stored source
wording remains untouched.

The browser formats displayed observation dates as MM/DD/YY and textual note
dates as MM/DD/YY when the year is known, otherwise MM/DD. The runtime's ISO
context dates remain unchanged. Long dates in original wording are intentionally
preserved inside expandable source details. No runtime restart or model call is
needed for this display change. JavaScript checks cover these captions/date
formats; browser checks cover depth 3, label visibility, toggle and note access.

## Post-memory sweeper

A separate daemon runs every 60 seconds after memories have been saved. It processes
at most 200 owned records (legacy summaries and graph claims) and reuses their
existing Chroma vectors; it makes no Gemini/embedding API calls or new embedding
inferences. Fingerprints skip recomputation when records/vectors are unchanged.
It defers to memory saving/forgetting and stays outside the speech response path.
Cosine comparisons still use local CPU; large-store speech latency has not been
benchmarked. The working set is bounded, not a full-vault scalable sweep yet.

Exact copies within the same scope/kind/polarity are grouped with every original
source ID retained. Legacy facts and episodes only combine within the same
observation day; unknown-date legacy records stay distinct. The bubble map folds
exact copies into one node. Original database rows and evidence are not deleted.
Positive weighted associations (at most two edges per note / 80 total) produce dashed
similarity hints and extractive related-note digests, not merged personal facts.
Opposite polarity produces a review-difference hint rather than a combined fact.
Broad conversation summaries are excluded from semantic-neighbor matching.

Year completion is deliberately provisional. An explicitly dated conversation
frame must match month/day/weekday and be within seven days of its recorded time.
A fact with one explicit weekday/month/day may use a matching frame within one day
from the same memory scope. Store the inferred ISO date, recorded observation date,
basis and supporting record ID separately, labelled context-derived candidate.
Missing weekday, old/ambiguous dates, explicit existing years and conflicting
context remain untouched. A record timestamp alone is not an event date.

The debugger shows worker status, duplicate groups, related-note digests and
candidate dates; selecting a note exposes its sources and supporting date context.
Inferred dates, digests and similarity links do not become authoritative graph
claims or enter routine conversational context. This version does not do freeform
semantic rewriting, model-judged equivalence or automatic conflict resolution.

Results persist in the private graph SQLite `sweep_results` table, alongside a
source fingerprint. Forgetting clears this table and an epoch check discards
inflight stale writes. Snapshots clear derived data while forgetting is pending.
Name-linked legacy sources and explicit profile-ID sources keep separate scopes;
the sweeper does not invent mappings or identify people. Set
`EMBER_MEMORY_SWEEPER=0` to disable processing on restart; persisted results remain
inspectable with the worker labelled disabled. `EMBER_ACTIVE_MEMORY=0` disables
the graph and sweeper together.

Validation: `test_memory_sweeper.py` covers exact consolidation, people/time/polarity
isolation, related digests, bounded links, contextual years, ambiguous dates,
persistence, unchanged passes and forgetting races. JavaScript checks verify
scoped hint edges and exact-copy node folding. Isolated real ONNX/Chroma tests use
only synthetic notes and check source preservation, person separation, candidate
years and forget cleanup. Existing speech, models and free-tier policies remain
unchanged; there is no additional cloud data flow from this worker.

## Interaction encodings and graph lenses

`interactions.py` records evidence about completed user turns separately from
personal facts. A narrow local parser captures explicit first-person feelings
with their quoted words, stated target and timestamp. Negating a feeling (not
happy) leaves valence unknown; liking music or dancing never implies happiness.
Quoted, hypothetical/reported statements and questions are conservatively skipped.
This is an English rule-based first version, not a general emotion-understanding
model. Reports can be wrong; they are never labelled verified psychological facts.

The current-state window for a feeling is 30 minutes. The historical report remains
inspectable after expiry; no stable personality or causal music/mood association
is inferred. Claim context can include a small recent-context field only when the
same owner's source report is unexpired and its explicit target shares a content
word with the recalled fact. Unrelated/expired reports stay out of model context.
This lexical check is conservative and is not a semantic causal inference engine.

Natural feedback captures explicit phrases (requested again, not now/space,
shorter answer, correction, explicit positive/negative reaction). No surveys are
added, and silence, yes/no, thanks or response length are not satisfaction scores.
The preceding Ember transcript is at most a candidate referent, not proof of which
song/topic was liked. Timing boundaries do not become dislikes of the topic.

Metrics include completed utterance word count, first-to-last transcript fragment
arrival span when multiple fragments exist, and a response-gap proxy from the last
completed Ember transcript to the user's first fragment. These include ASR,
provider, network and playback-buffer timing differences. They are not precise
acoustic speech duration or speaking rate; those remain unmeasured. Concurrent
transcript streams can be recorded without interpreting why the user interrupted.
No new device streams, VAD/audio changes or cloud calls are introduced.

The tracker resets on memory-scope changes. Pending turns persist a whitelisted
metrics object; graph ingestion durably stores interaction_events under the job's
immutable owner. Source IDs link episodes to claims through existing evidence.
Unowned episodes remain unattributed; inspection never identifies speakers.
Forgetting clears interaction events and removes pending interaction metadata;
old records receive no invented timing or emotions.

In Connect by, choose Topic, Reported emotion, Response gap, Response length or
Natural feedback. The encoding slider changes feeling/reaction category, maximum
gap in seconds or minimum word count; it is not an emotional-intensity score.
Include unmeasured keeps older unknown notes visible in a separate gray group.
Non-topic lenses also show measured interaction episodes, including feelings that
do not create durable preference claims. Groups/colors/links express the selected
encoding, not a reward or desirability ranking. Depth, search, profile isolation,
source inspection, pan/zoom and quiet labels remain available. Per-frame visual
emotion, true speech-speed measurement, learned outcome ranking and automatic
proactive song recommendations remain future integrations.

Validation: `test_memory_interactions.py` covers explicit reports/negation,
false-inference exclusions, natural reactions, timing proxies, scope changes,
whitelisting, persistence/forgetting, source ownership and context expiry/target
matching. JavaScript tests cover lenses, thresholds, unknown values and cross-owner
exclusion. Browser checks use the live store for unknown-data behavior; invented
fixtures stay isolated and are never inserted into the user's database.

## Labeled local conversation validation

The default `tests/fixtures/memory_five_day_conversation.json` contains 50 invented
turns over five dates/sessions: immunology work/research, League of Legends teams,
PhD applications, rhythm games, and daily Chinese translation requests for the
speaker's parents. It has 24 recall checks, repeated interests, a profile-isolation
guest, a replay, older evidence arriving later and a corrected team preference.
Chinese replies are supplied illustrative dialogue; translation quality and actual
task execution are not evaluated. Expected labels never enter model inputs.
The earlier 25-turn `memory_conversation_labeled.json` remains a separate baseline.
Labels describe intended self-reports, not independently verified real-world facts;
episodic remarks/temporary feelings are outside durable-claim ground-truth scoring.

From `Baymax-main`, run:

```powershell
python -m features.memory_palace.diagnostics.validate_conversation
```

The diagnostic loads the existing local ONNX model with Hugging Face offline
flags and uses an in-memory Chroma client with telemetry disabled. Each simulated
session reopens the same temporary SQLite store and recreates its short-term
buffer; the model process and transient Chroma client are reused. This exercises
local persistence and expiry, not real process/device or Gemini session restarts.
It does not read private memories, open devices, restart Ember, fetch models or
call Gemini. There is no runtime feature to enable; invoke the diagnostic directly.
Outputs under `.local-review/five-day-conversation/` are `conversation.md` (all
turns/labels), `report.md`, `results.json` and `debugger.html`. Exit 1 means labels found failures,
even though the run completed and wrote its report. To run the earlier baseline,
pass `--fixture tests/fixtures/memory_conversation_labeled.json` and a separate
`--output .local-review/labeled-conversation` directory.

The evaluator scores source-level claim precision/recall, canonical source sets,
per-source metadata/observation-time bounds, daily profile-scoped production
retrieval, long-term store restoration, previous-cache expiry, short-term vector
sweeping and forgetting/replay protection. Expected query labels are checked only
after each session's extraction/indexing. Assistant speech is excluded and replayed
sources do not inflate extraction totals. This is a small synthetic diagnostic,
not general accuracy, live audio latency, translation quality or token savings.

Initial five-day actual-ONNX results: 21 true-positive claims, three false-positive
truncated values and eight missing labeled claims (87.5% precision, 72.4% recall).
Recall passed 21/24; all four local-store reopenings and previous-day cache expiry
checks passed, with zero metadata errors. Daily parents-translation goals and the
initial PhD goal were retrievable through day five. Failures include missed
occupation/research/negation/paraphrase statements and punctuation truncation of
`Gen.G`/`osu!mania`. Missing corrections allow stale team recall. These failures
are retained, not supplied from labels. No Gemini enrichment is called or mocked.

`tests/test_memory_conversation_validation.py` covers evaluator scoring, fixture
constraints, session grouping and readable transcript generation. Run
`python check_release.py` for the separate offline software gate. A passing gate
does not make the labeled ONNX diagnostic pass or establish live audio behavior.
