# Ember features

Core feature systems and callable toolkits are siblings here:

```text
features/
  memory_palace/       Connected memories, interests, relationships and goals
  person_recognition/ Recognized people and confirmed identity context
  toolkits/           Callable information sources and other bounded actions
  conversational_ability/ Responsive listening, audio cleanup and turn-taking
  eyes/                Normal blinking and delayed thinking on MAX7219 matrices
```

The runtime coordinates these systems. Person recognition supplies identity context;
the memory palace maintains the connected understanding of that person; toolkits
retrieve information or perform supported actions. A personal news feed is one
consumer of that understanding, alongside conversation starters and follow-ups.
Memory and recognition are core feature systems, not toolkits.

`toolkits/` contains the existing implementation used by desktop v8 and robot v10.
Its [README](toolkits/README.md) and [public experiment guide](toolkits/EXPERIMENTAL.md)
describe providers, data flows, enablement and tests. Imports use
`features.toolkits`; desktop Setup/Monitor and clickables keep the same entry points.

`memory_palace/` and `person_recognition/` reserve integration boundaries; this PR adds no memory graph, recognition engine, identity prompt or changes to recalled-memory wording. Those behaviors are scoped to PR #13 and the existing people-awareness work in PR #8. Do not create another identity store or treat feed profile IDs as recognized people.

Feature package imports must not start providers, open devices or run background
workers. Runtime startup owns lifecycle and enablement; each implemented feature
needs documented inputs, outputs, data flows and tests. Runtime data stays under
the existing `BAYMAX_DATA_DIR`, outside the source tree in Docker.

[Conversational ability](conversational_ability/README.md) is implemented independently
of people recognition and memory. Memory Palace is reviewed in PR #13.

[LED eyes](eyes/README.md) are started by the MVP launcher: normal blinking
while listening/speaking, and the supplied thinking dots only after a wait
exceeds two seconds. Serial hardware is optional and failures are non-fatal.
