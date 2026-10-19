# Living Memory Palace overview

`index.html` is the canonical, version-controlled presentation of Ember's Memory
Palace. It should grow with the feature rather than become a one-off architecture
snapshot. Open the file directly in a browser; it needs no build, dependencies,
API key, network calls, microphone, camera or runtime data.

For local browser review, serve this folder on loopback with an installed Python:

```powershell
python -m http.server 8772 --bind 127.0.0.1 --directory docs/memory-palace
```

Open `http://127.0.0.1:8772/`. This documentation server does not start Ember.

## Update alongside implementation

When a Memory Palace change alters the experience or data flow, update the
relevant diagram label, its matching entry in the inline `stages` array and the
illustrative example in the same change. Add or split stages when a new concept
needs its own explanation; do not keep five stages merely for visual symmetry.
Keep the overview short enough to present, with deeper explanations on click.

| Presentation stage | Implementation to consult |
| --- | --- |
| Capture | `realtime_gemini_10._TurnTranscript`, `person_memory.ConversationLog`, `interactions.InteractionTracker` |
| Understand | `graph.LocalEncoder`, `hybrid.HybridEncoder`, `interactions.encode_interaction` |
| Remember | `graph.PalaceGraph`, `memory_context.MemoryContext`, `retrieval.GraphIndex` |
| Connect | `sweeper.MemorySweeper` and evidence/provenance handling |
| Recall | `retrieval.GraphIndex`, `runtime_context.graph_lines`, `interactions.current_affect`, `realtime_gemini_10._MemoryInjector` |

Paths without a module prefix above refer to `Baymax-main/features/memory_palace`.
The live runtime and `person_memory` modules are under `Baymax-main`.

Keep these distinctions clear as the system develops:

- Current implementation versus planned capability; prototype versus verified
  production behavior.
- Explicit statements versus inferred feelings; timing proxies versus measured
  acoustic response time; similarity hints versus factual relationships.
- Supplied profile ownership versus actual person identification, which belongs
  to a separate system.
- Background learning versus the live reply path. Do not promise zero latency,
  unlimited scaling, guaranteed model use or measured savings without evidence.
- New graph storage versus the legacy summary path while both remain in use.

Examples must be invented and labelled illustrative. Do not embed personal
memories, transcripts, credentials, biometric information or runtime snapshots.
This page explains the pipeline; it does not inspect a live memory store.

## Review before sharing

Open the page at desktop and narrow widths. Check every stage expands and
collapses, the example advances, Previous/Next respect boundaries, optional
technical details open, Escape collapses, and Present toggles presentation mode.
Check keyboard focus and ensure labels do not overlap or clip. Check the inline
script parses after edits. Run the repository's required release gate for PR
changes; live hardware tests are separate and are not started by documentation.

The generated Graphify graph remains an optional local engineering aid. Do not
copy its full implementation graph into the investor overview. Refresh it when
needed to verify connections, then edit this canonical presentation deliberately.
No background synchronization, scheduled job or paid service is required.

## Presenting and exporting

Click **Present** to hide the documentation footer. Click a stage to reveal its
role, value and an example; **Under the hood** contains technical limits. The
browser's print command uses the included print styles. Keep `index.html` as the
editable source when preparing a later deck or export.
