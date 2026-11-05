# Ember features

Core feature systems and callable toolkits are siblings here:

`persona.py` supplies Ember's spoken identity and social guidance through the
existing session config hook, with toolkits enabled or disabled. Replies normally
make one brief conversational move, follow a specific detail with a question or
observation when appropriate, and leave space for the person. Personality stays
present without routine praise, unsolicited wellness reminders or invented topics.
The prompt is sent to Gemini with session instructions; it adds no provider,
device stream or setting. `tests/test_toolkit_eyes.py` checks persona injection;
live conversation still needs evaluation for brevity, relevance and turn balance.

```text
features/
  memory_palace/       Connected memories, interests, relationships and goals
  person_recognition/ Recognized people and confirmed identity context
  toolkits/           Callable information sources and other bounded actions
  startup/             Setup, launch lock and MVP lifecycle
  conversation/        Responsive listening, audio cleanup and voice sleep
  pose_detection/      Camera model processes, observers and fall detection
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

`memory_palace/` and `person_recognition/` now package the existing PR #13
libraries dormant, without their active conversation/camera hooks or saved data.
They remain off in the OPI4 image. See `../../docs/OPI4_FEATURES.md` for the grouped
source map, scope and activation limits. Feed profile IDs are not recognized people.

Feature package imports must not start providers, open devices or run background
workers. Runtime startup owns lifecycle and enablement; each implemented feature
needs documented inputs, outputs, data flows and tests. Runtime data stays under
the existing `BAYMAX_DATA_DIR`, outside the source tree in Docker.

[Conversational ability](conversational_ability/README.md) is implemented independently
of people recognition and memory. Memory Palace is reviewed in PR #13.

[LED eyes](eyes/README.md) are started by the MVP launcher: normal blinking
while listening/speaking, and the supplied thinking dots only after a wait
exceeds two seconds. Serial hardware is optional and failures are non-fatal.
