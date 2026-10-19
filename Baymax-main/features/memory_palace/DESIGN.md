# Evidence graph roadmap

Implemented: a durable local queue, one encoder worker, source-backed proposal
nodes, category edges, exact duplicate consolidation and a read-only tree.
Hybrid Gemini enrichment now adds source-checked long-term proposals in bounded
background batches. Semantic merging and person attribution remain future work.
The graph deliberately does not equate face presence with speaker identity.

Next stages, each requiring measured acceptance evidence:

1. Review and verified ownership: confirm who made a statement using explicit
   user confirmation and stable person IDs. Audit attribution changes.
2. Promote approved claims into retrieval with source citations, temporal status
   and scoped permissions. Never promote a proposal because it was repeated by
   the model itself.
3. Local-model evaluation against an independently labelled transcript set:
   extraction precision/recall, attribution errors, corrections, negation,
   third-party statements, hypotheticals, multilingual speech and prompt attacks.
4. Semantic consolidation: propose duplicate merges, preserve conflicting
   evidence and time ranges, allow user correction and explain every change.
5. Typed relationships, goals, episodes and interests; interactive graph search
   and person-specific views. Personalized feeds use approved interests, with
   controls for correction and forgetting.
6. Benchmark inference while audio is active: p50/p95 response latency, encoder
   lag, CPU/RAM/GPU pressure and recovery after service failure. A separate
   worker avoids waiting for inference but does not guarantee zero resource
   contention. A larger local model may improve extraction but must be measured.

Quality is not measured by graph size. Prioritize faithful evidence, low false
personal attribution, reversible corrections, reliable forgetting and relevance.
No numeric quality claim is justified until the evaluation set exists.
