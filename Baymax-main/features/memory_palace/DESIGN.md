# Memory palace design

Status: proposed architecture and acceptance criteria, not a running implementation.
This design extends the people/memory foundation in open PR #8. The current PR #11
still supplies the feature namespaces and toolkits; it does not implement this graph.

## What the system should achieve

Ember should build an evidence-backed, connected understanding of each person over
time and use a small relevant part of it in each interaction. It should remember
relationships, interests, projects, experiences and unfinished plans; connect new
information to them; and accept corrections naturally. The same memory should
support conversation, follow-ups, personal feeds and shared activities.

The first requirement is correct ownership. A detailed memory about the wrong
person makes every downstream capability worse. Unknown identity is a valid state,
not an error that the model should fill with a guess.

## Identity investigation: the reported James mix-up

Source inspection on 2026-10-18 found no hard-coded James identity in the v8/v10
conversation code checked. James appears in review documentation, not as the
current user. The v8 desktop path disables saved-memory loading/recall and still
contains a scripted Temple University demo context. That location context is not
identity evidence. The inspected Desktop checkout had no saved person/profile file;
its available runtime status snapshot was old and cannot establish the reported run.

The merged v10 baseline has a concrete attribution risk: `_build_config()` reads
up to 50 memories without person filtering and introduces them as established facts
about "this user" that must not be contradicted. Its recall text similarly calls
retrieved records memories about "this user". This can encourage identity blending
when a device contains memories from different people. It is a plausible failure
mechanism, not proof that it caused the James incident. The exact running version,
active session context and, if applicable, its saved profile/memory need checking.
The user clarified that this occurred on the physical robot. Its checked-in startup
prefers v10 but falls back to v8 if v10 is absent, so deployed runtime parity cannot
be assumed. The [read-only identity diagnostic](../person_recognition/README.md#investigate-a-wrong-name-report-on-the-robot)
collects bounded source/profile/memory metadata on the robot without capture or
runtime data modification. It has not yet been run on the affected robot.

PR #8 improves person-linked memory, but it is still open. Its face store already
has stable `p_*` IDs, while person-memory ownership uses names in metadata and
some name-based enrollment can reuse a record by name. Names must become display
labels/aliases, not the primary join key for graph ownership. Two people named James
must be representable as different people.

## Keep five identity concepts separate

| Concept | Owner | Meaning |
|---|---|---|
| Robot identity | Runtime/self description | Ember's name, capabilities and role |
| Primary household person | Confirmed profile | Who the robot is configured to support |
| People present | Person recognition | Who appears to be visible, with uncertainty |
| Current speaker | Session identity resolver | Who is actually speaking, if established |
| People mentioned | Conversation evidence | Who a statement is about; not necessarily present or speaking |

A household profile naming James does not identify the current speaker as James.
Seeing James in view does not establish that a voice belongs to James. A developer
named James, a quoted James, a feed profile ID, a code example or a retrieved memory
about James cannot bind the current speaker. Greeting a recognized person in view
and assigning an utterance's ownership are separate decisions.

## Canonical people and session identity

Reuse and extend #8's existing person directory and stable IDs. Keep face embeddings
with person recognition; the memory graph references person IDs without copying
biometric data. A person can have memories without enrolling a face. Memory consent
and face-enrollment consent are separate, inspectable choices.

The session identity resolver has three explicit states:

- **Unknown:** no established current speaker. Allow conversation and temporary
  session context; do not inject a person's private history or assign their utterances.
- **Candidate/ambiguous:** one or more possible people, with evidence and source.
  Ask a short clarification when identity matters. Preserve uncertainty downstream.
- **Confirmed for this session:** a resolved person ID with recorded confirmation
  evidence. This permits scoped recall under that person's consent/audience settings.

"Call me Nate" may establish a session display name without consenting to permanent
memory or linking to an existing Nate. Resolve duplicate-name ambiguity before
binding a persistent ID. Self-introduction alone is not an authentication mechanism
for revealing somebody else's sensitive stored history.

Corrections take effect immediately: "I'm Nate, not James" clears the prior speaker
binding, retracts attributable session assumptions, and resolves the correct identity.
It does not rename James's record or move his memories onto Nate. Speaker changes,
group conversations and session restarts must not inherit a previous person's binding.

## Graph and evidence model

Use a local SQLite graph store under `BAYMAX_DATA_DIR/memory_palace/` as the
authoritative record. Reuse the existing semantic embedding/index machinery as a
rebuildable search projection, not as a second source of truth. A graph does not
require a separate graph-server dependency for the first implementation.

| Record | Required information |
|---|---|
| Entity | Stable ID, type, display label, aliases, lifecycle status |
| Claim/edge | Stable ID, subject ID, typed relation, object ID or typed value, status |
| Evidence | Utterance/event ID, attributed speaker ID or unknown, source type, timestamp, consent scope |
| Claim provenance | Evidence links, confidence, observed time, validity interval, correction/supersession links |
| Episode | Shared conversation/event ID, participants with attribution certainty, time, concise summary |
| Interaction outcome | Suggested item/action, person ID, explicit feedback, selection/delivery state, time |

Entity types include person, topic, project, goal, activity, place and event.
Relations include `interested_in`, `working_on`, `knows`, `related_to`, `plans_to`,
`experienced`, `prefers` and `avoids`. Claims are tentative, confirmed, disputed,
superseded or retracted. These statuses control retrieval; a confidence number
alone cannot turn an unverified name into a confirmed identity.

Example: a person says they are building a companion robot. Store one episode,
an evidence-backed `working_on` link to their project, and project links to robotics
and conversation design when supported. A possible interest in semiconductors stays
tentative until supported; it must not become a quoted user preference automatically.

Shared events are stored once and linked to participants. Each claim distinguishes
the speaker from its subject and from bystanders. Unknown-speaker utterances remain
unattributed until resolved. A quoted "James said I like jazz" does not establish
that the current speaker likes jazz.

## Learning and correction pipeline

1. Receive the existing transcript stream and identity context; open no new devices.
2. Keep attributed utterances and episode boundaries using #8's existing durable log.
3. Extract candidate entities/claims with evidence IDs using the existing summarization
   path. Model output is a proposal, never an instruction to trust or overwrite data.
4. Validate IDs, allowed relations, ownership, evidence references and consent in code.
   Reject unsupported evidence IDs and fabricated people. Keep attribution ambiguous
   when it is ambiguous; the extractor must not resolve identity by guessing.
5. Merge equivalent supported claims transactionally; retain their separate evidence.
   Record contradictions explicitly instead of silently overwriting all older wording.
6. Commit validated claims, then update the rebuildable semantic index and invalidate
   affected cached context. Queue failures durably for idempotent retry.

Explicit corrections outrank older inference. Repeated independent user evidence
can strengthen a preference; repeating an AI summary or asking the same question
again is not independent evidence. "I dislike crypto" must not produce a positive
crypto interest. Discussing a hypothetical, quoting someone or expressing curiosity
does not automatically establish a lasting personal fact.

Temporary plans and situational preferences have validity windows. Completed goals
and past events remain history without being treated as current tasks. Stale,
unsupported inferences weaken; explicit exclusions remain effective until changed.
Confidence/decay settings begin as documented heuristics and require evaluation.

## Retrieval before conversation or curation

The runtime supplies the resolved identity/audience context; an LLM cannot bypass
ownership by putting another person ID into a tool argument. Apply identity, consent,
claim-status and audience filters **before** semantic ranking or graph traversal.
Unknown-speaker sessions receive no automatic private per-person memory block.

Within the permitted scope, combine semantic relevance with short graph traversal,
recency, evidence quality, current goals, unfinished plans and prior use. Return a
small context budget, not the whole graph. Include provenance and uncertainty in
the structured result so the response can qualify a tentative memory appropriately.
Avoid repeatedly bringing up the same fact or using stale events as conversation hooks.

The prompt must label robot identity, current identity evidence and recalled memory
separately. Remove the legacy instruction to treat every retrieved item as an
unquestionable fact about "this user". A current identity correction always takes
precedence over a recalled name. Session resumption must invalidate stale identity
or forgotten/corrected context before it can be reused.

## Personal feeds and curated interactions

Toolkits consume a bounded interest/context projection from this graph. They do
not own a parallel identity or memory system. Once identity integration exists,
migrate explicit local feed preferences to linked claims only with confirmed mapping;
never match a feed ID to a person merely because names resemble each other.

For feeds, retrieve stories, then rank by supported interests, current projects,
freshness, novelty and explicit feedback. Cluster repeated coverage; diversify
sources/topics; provide occasional adjacent discoveries where the person wants them.
Apply explicit exclusions before ranking. Keep the reason for each selection.

For conversation, the same context can suggest an unfinished project follow-up,
a relevant new story, a remembered shared activity or a useful connection. Suggestions
are candidates: the runtime still decides whether the timing and social context fit.

Track suggested/returned/delivered/acknowledged states separately. Returning a story
from a tool does not prove the person heard it or liked it. Dwell time, silence,
confusion or repeated questions must not be interpreted automatically as enjoyment.
Use explicit more/less/not-now feedback and supported conversational evidence.

Evaluate helpfulness, voluntary return, preference accuracy, correction recovery,
successful follow-ups and topic fatigue. Optional routines/progress can support the
person's goals. Session duration is not the sole success metric; a satisfying short
interaction is successful. Users can pause proactive suggestions and reset learned
preferences without erasing all personal history.

## Inspectability, forgetting and runtime boundaries

The debugging UI should show a person-centered graph with separate confirmed and
tentative connections. Selecting an edge shows evidence, attribution, timestamps,
why it is active and where it was used. Provide correction, merge/split, dismiss,
expiry and forget actions. Show the current speaker state separately from visible
people, including why a name was suggested. This would make the James failure
traceable without guessing from the final spoken response.

Extend #8's existing voice/dashboard forgetting flow. Remove or rewrite affected
claims, evidence, shared episodes and index projections; invalidate session/prompt
caches and pending extraction work so forgotten data cannot be reintroduced. Preserve
unrelated people's memories. Use durable deletion/tombstone handling for interrupted
operations and test crash recovery. User-visible correction/deletion should remain
available if a model/provider is unavailable.

Keep runtime data out of Git and Docker images. Preserve one authoritative person
directory, one authoritative graph and a rebuildable search index. Coordinate core
and dashboard writes transactionally. Background extraction/index work must be bounded
and must not delay audio/video or open additional microphone/camera streams. Document
what is sent through existing Gemini extraction and any cloud sync before enabling it.

## Implementation order and acceptance scenarios

1. **Identity boundary:** stable person-ID mapping, unknown/candidate/confirmed session
   states, correction handling, scoped recall, source-attribution UI and legacy-memory
   quarantine. Do this before enabling automatic graph learning or graph-fed news.
2. **Evidence graph:** local schema/versioning, idempotent extraction/import, typed
   claims, corrections, shared events, forgetting and semantic-index projection.
3. **Context service:** scoped retrieval, graph traversal, freshness/novelty, context
   budgets and follow-up selection for desktop and robot runtimes.
4. **Curation and inspection:** graph-driven toolkits and conversational suggestions,
   explicit feedback, saturation control and an inspectable graph UI.

Required acceptance scenarios include:

- A device belongs to James; Nate starts speaking. No speaker binding or James-memory
  injection occurs merely because James is the primary profile.
- James is visible while Nate speaks off-camera; face presence does not attribute
  Nate's statement to James.
- "I'm Nate, not James" updates the session immediately without renaming James or
  transferring his memories. Reconnect does not revive the wrong binding.
- Two different people named James retain separate IDs, histories and face samples.
- Mentioning James, quoting him, reading test data or discussing Ember's developers
  never identifies the speaker as James.
- Unknown identity, a missing profile, an unavailable recognition model or a group
  conversation permits ordinary conversation without inventing a name.
- A user asks one AI question, dislikes a crypto story, finishes a goal or corrects a
  date. The graph preserves the right polarity, uncertainty and time validity.
- Corrupt storage, extraction failures, crashes/retries and repeated extraction cannot
  lose confirmed corrections, duplicate evidence or resurrect forgotten information.
- A shared event is represented once; forgetting one participant preserves only the
  permitted information for the others and removes affected search/session results.
- A feed/context request cannot retrieve another person's private graph by changing
  an LLM-supplied ID. Explicit exclusions and paused suggestions remain effective.

Legacy unscoped memories stay quarantined until their ownership is confirmed; do
not attach the old database to the first person who introduces themselves. Validate
synthetic identity/correction cases offline first, then consented multi-person live
conversation, interruption/reconnect and target-device performance. Record core,
recognition, memory, toolkit and UI evidence separately before software-lead approval.
