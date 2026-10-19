# Memory Palace boundaries and roadmap

Implemented: durable transcript work, local evidence graph, background local and
Gemini extraction, source quotes, polarity, exact duplicate consolidation,
local embeddings and scoped graph-to-conversation retrieval. A bounded local
post-save sweeper now groups exact copies, creates vector-similarity hints and
records context-derived date candidates while retaining all originals. These
derived relationships/dates are inspectable suggestions, not new recalled facts.

The application supplies a stable profile ID through MemoryContext. Memory
Palace does not identify a person from face or voice, parse spoken introductions,
require enrollment or speaker confirmation, expire an identity or restart the
conversation on a profile change. Recognition and identity/session boundaries
belong to their own features/PRs. A manual profile selector makes memory testing
possible without those dependencies. Scope IDs are not authentication.

Preserve these invariants: unknown scope stays unattributed; Gemini cannot
assign source ownership; profile changes reject stale results but do not change
playback or the Live connection; SQLite evidence/ownership is revalidated after
vector search. Context already sent to the model is not erased by changing the
memory scope.

Next memory improvements, requiring measured evidence:

1. Reviewed corrections and retrospective attribution based on an externally
   supplied identity; audit changes instead of silently guessing ownership.
2. Semantic duplicate proposals and temporal conflict resolution, preserving
   contradictory quotes and allowing user correction.
3. Durable episodes, goals and typed relationships with explicit source subjects.
4. Interaction evidence such as timing and explicit feedback, kept separate from
   inferred emotion or claims about whether the person enjoyed the experience.
5. Retrieval ranking with relevance, recency, evidence quality and repetition
   controls; personalized feeds based on scoped, user-correctable interests.
6. An independently labelled evaluation set covering negation, sarcasm,
   multilingual speech, corrections and prompt injection, plus p50/p95 live
   latency and CPU/RAM/GPU measurements while consolidation is active.

Quality is faithful evidence, useful recall, low false attribution and reliable
correction/forgetting, rather than graph size or time spent in conversation.
