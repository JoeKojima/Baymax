# Neutral person and setting context

This small fix is based on the open people-awareness PR #8. It removes the default
older-adult/home framing from `ember_identity.md`, the missing-file fallback in
`ember_self.py`, and the memory-extraction/appearance prompts in `person_memory.py`.
Ember remains a companion robot with its existing capabilities and safeguards.

The configured profile is background information, not proof of the current speaker.
The former `Their name is <profile>. Call them <profile>` instruction is removed,
including from mid-session profile updates. Unknown identity stays unnamed; current
corrections take priority. A profile named James may remain stored and useful without
requiring Ember to call every visitor James. Nothing rewrites saved profiles/memories.

This is a prompt correction, not a new recognition/identity-verification system.
There are no added dependencies, capture streams, provider requests, storage files
or feature switches. Existing Gemini sessions receive the revised initial prompt
after a fresh session/restart in a test deployment; this PR does not deploy changes.

Validation on Windows: 54 platform-independent profile/identity/live-status and
memory-log/name/extraction/appearance/initiation/greeting tests pass, including a
James-profile regression and missing-template fallback. The existing API tests
require Linux `fcntl`; stored-memory integration tests require ChromaDB, which is
unavailable in this test environment. The wider attempted run reported those
environment errors; no full-suite or live-robot pass is claimed. The existing fake
Live identity scenario assertions are updated, but that scenario still needs a
Linux environment. Face behavior, storage algorithms and audio/model settings are
unchanged. Review and verify actual spoken behavior on the robot before deployment.
