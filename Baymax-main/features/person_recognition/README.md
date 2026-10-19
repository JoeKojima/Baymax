# Person recognition integration boundary

This namespace reserves a sibling core-system location alongside `memory_palace/` and `toolkits/`. PR #11 adds no recognition engine, identity store, prompt instructions or memory-recall behavior here. Person/setting changes and Memory Palace integration are scoped to PR #12; coordinate existing people-awareness work from PR #8 there. Feed profiles remain explicit local preferences, not identified speakers.

Imports must not open devices or start workers. Future recognition integration must reuse the runtime's existing camera frames and document consent, storage, forgetting, enablement, dependencies and tests.
