# Person recognition

This core feature will own recognized-person identity context, consent and
recognition uncertainty. It supplies identity context to the runtime and memory
palace. Recognizing a face does not establish who is speaking in a group.

**Status:** architecture namespace only in this PR. Open PR #8 contains the
existing face/person implementation (`face_id.py`) and associated dashboard,
consent and forgetting behavior; it has not been copied or replaced here.

When integrating #8, move its implementation under this namespace with compatible
runtime/dashboard imports and tests. Keep one identity store, preserve consent
and forgetting, reuse the shared camera frame, and coordinate person deletion
with the memory palace. Do not add a second camera stream.

Inputs, outputs, model dependencies, storage, enablement, external data flows and
recognition/consent tests should follow the integrated implementation's documented
contract. This namespace itself performs no recognition, downloads, capture,
requests, persistence or background work.
