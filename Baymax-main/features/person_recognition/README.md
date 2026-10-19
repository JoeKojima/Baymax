# Person recognition and current-person context

This core feature separates visible people, configured profiles and the current
speaker. Recognizing a face does not establish who is speaking in a group. The
[memory-palace design](../memory_palace/DESIGN.md) describes the broader person-ID,
consent, uncertainty and forgetting contract.

## Current runtime behavior

`context.py` appends neutral person/setting instructions in v8, v9 and v10. It does
not assume anyone's name, age, location, living arrangement or care needs. A
configured profile, a visible person, an old memory or a mentioned name is not
automatically the current speaker. If uncertain, use no name; accept corrections.
The old v8 Temple University demo setting is removed. v9/v10 label retrieved notes
as potentially concerning other people or outdated circumstances, rather than
unquestionable facts about whoever is currently speaking.

This behavior is part of conversation configuration, independent of optional
public-tool switches. It changes prompt context only: no face/voice identification,
new capture, requests, persistence, dependencies or background work is introduced.
It does not delete or rewrite existing memories/profiles. Fresh session configuration
picks it up after restarting the selected runtime in a test deployment.

`tests/test_person_context.py` executes real config/recall function bodies without
devices/Gemini, including old notes mentioning a different person and current
correction instructions. v8 parity tests permit exactly the documented demo-setting
removal and context hook while preserving models, audio rates and original processing.
Run `python check_release.py`. Actual spoken behavior on the robot remains a live
test, not an offline assertion. No identity diagnostic is included.

## Recognition implementation and #8

Recognition itself is still pending integration in this feature tree. Open PR #8
contains the face/person implementation (`face_id.py`), profile formatting,
dashboard, consent and forgetting. Its age/home/profile-as-speaker assumptions are
corrected in a small companion branch based on #8; they are not duplicated here.

When integrating #8, move its implementation here with compatible runtime/dashboard
imports. Retain its stable person IDs and one identity store, coordinate person
deletion with the memory palace, and reuse the shared camera frame. Do not add a
second camera stream. Model dependencies, storage, enablement, provider data flows
and recognition/consent tests should follow the integrated implementation's contract.
