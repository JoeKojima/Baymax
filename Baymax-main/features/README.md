# Ember features

Core feature systems and callable toolkits are siblings here:

```text
features/
  memory_palace/       Connected memories, interests, relationships and goals
  person_recognition/ Recognized people and confirmed identity context
  toolkits/           Callable information sources and other bounded actions
  audio_input/        Opt-in local echo cancellation and microphone cleanup
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

`memory_palace/` contains the opt-in v10 debugging bridge and graph design.
The integrated people-awareness implementation remains in `ember_self.py`,
`person_memory.py` and `face_id.py`; it preserves consent and forgetting.
`person_recognition/` supplies neutral speaker/setting instructions. Named feed
profiles remain separate from recognized people. A full connected memory graph
and automatic feed ranking are planned, not implemented.

Feature package imports must not start providers, open devices or run background
workers. Runtime startup owns lifecycle and enablement; each implemented feature
needs documented inputs, outputs, data flows and tests. Runtime data stays under
the existing `BAYMAX_DATA_DIR`, outside the source tree in Docker.

The responsive audio pipeline is inherited from Conversational Ability (#12).

[Conversational ability](conversational_ability/README.md) is inherited from PR #12
and works independently of the Memory Palace systems in PR #13.
