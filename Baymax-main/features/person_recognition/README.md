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

## Integrated recognition

The #8 implementation is included in this PR in the existing root `face_id.py`,
`ember_self.py` and `person_memory.py` modules. Consent, person records, correction
and coordinated forgetting remain there. This package does not add speaker
verification or a second identity store. See the Memory Palace README for the
v10 debugging bridge and physical-robot validation still needed.
