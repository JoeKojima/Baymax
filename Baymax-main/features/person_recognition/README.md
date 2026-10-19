# Person recognition

This core feature will own recognized-person identity context, consent and
recognition uncertainty. It supplies identity context to the runtime and memory
palace. Recognizing a face does not establish who is speaking in a group.

**Status:** recognition implementation pending; a read-only diagnostic is available
below. Open PR #8 contains the
existing face/person implementation (`face_id.py`) and associated dashboard,
consent and forgetting behavior; it has not been copied or replaced here.

The [memory-palace design](../memory_palace/DESIGN.md) specifies the shared identity
boundary: robot identity, primary household person, visible people, current speaker
and mentioned people are separate. Reuse #8's stable person IDs; names are labels,
and visible faces are not proof of which person is speaking.

When integrating #8, move its implementation under this namespace with compatible
runtime/dashboard imports and tests. Keep one identity store, preserve consent
and forgetting, reuse the shared camera frame, and coordinate person deletion
with the memory palace. Do not add a second camera stream.

Inputs, outputs, model dependencies, storage, enablement, external data flows and
recognition/consent tests should follow the integrated implementation's documented
contract. This namespace itself performs no recognition, downloads, capture,
requests, persistence or background work on import.

## Investigate a wrong-name report on the robot

`diagnose_identity.py` is a standalone standard-library Python script that can run
on an older robot checkout without installing or deploying this feature branch:

```bash
python3 /path/to/diagnose_identity.py
```

It tries to locate a single running Python v8/v9/v10 core through Linux `/proc`.
If the source or data location is ambiguous, specify the actual runtime paths:

```bash
python3 /path/to/diagnose_identity.py --app-dir /actual/robot/app --data-dir /actual/robot/data --probe-name James
```

For Docker, the host data directory is normally `/var/lib/ember`; inside the
container it is `/data`. The running image's source may differ from the host
checkout. The report labels fallback paths as unverified; compare the detected
process path with the inspected source before drawing conclusions. Custom launchers,
container boundaries or process permissions may prevent automatic discovery.

The JSON report includes only runtime/path indicators, profile name-match flags,
person counts, legacy unscoped-memory prompt indicators and committed Chroma SQLite
metadata counts. It never imports the runtime or Chroma SDK, opens devices, calls
Gemini/network services, reads `.env`/process environments/transcripts, prints face
embeddings or returns memory text. Complete process command lines are not printed.
Source/profile files are bounded reads. Memory inspection uses SQLite's immutable,
query-only committed snapshot to avoid writes/sidecar creation; an active WAL may
contain newer records not represented in those counts. The probe matches metadata
substrings, so a count is not proof that a particular memory identified the speaker.

No robot data or services are changed. This is diagnostic evidence, not an automatic
name reset or a confirmed incident diagnosis. Keep runtime reports local to the
review; no public issue/comment upload is performed by the script.

Ten synthetic checks in `tests/test_identity_diagnostic.py` cover source indicators,
redaction, missing/corrupt files, SQLite read-only behavior, literal probe handling,
process detection and avoiding runtime execution. Run `python check_release.py`.
