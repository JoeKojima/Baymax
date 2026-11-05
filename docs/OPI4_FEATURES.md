# OPI4 feature groups and flash profile

The application source is OPI4 Demo PR #29, not LattePanda Demo PR #20.
The image-builder work remains separate in PR #22. The non-secret grouped image
profile is `devops/opi4/flash-profile.json`; Wi-Fi, provider keys and access
credentials remain in the existing private image settings.

| Settings group | Source | Image behavior |
|---|---|---|
| Startup | `Baymax-main/features/startup/` | Existing MVP launcher, greeting, validated updates |
| Conversation | `Baymax-main/features/conversation/` | Audio cleanup, 400 ms turn end, engage/disengage voice sleep |
| Memory Palace | `Baymax-main/features/memory_palace/` | Real implementation packaged, dormant; no runtime integration started |
| People Recognition | `Baymax-main/features/person_recognition/` | Face/enrollment/recognition code packaged, dormant; no models loaded |
| Pose Detection | `Baymax-main/features/pose_detection/` | Lite pose model, 10 FPS target, separate inference process, latest pending frame |
| Eyes | `Baymax-main/features/eyes/` | Existing expressions/status; external Arduino needs configuration |
| Tools | `Baymax-main/features/toolkits/` | Existing reminders, audiobooks, discovery and app integrations |
| Device Settings | `pc_hardware.py`, `dev_settings.py`, `devUI.py` | Existing saved key and stable USB selection |

Old Python import paths and `run_mvp.py` remain compatibility entry points. New
implementation changes use the grouped paths. Camera/audio streams, provider
models and conversation prompts are otherwise preserved.

Memory Palace and People Recognition are intentionally dormant code packages in
this OPI4 profile, not a merge of PR #13's much larger runtime. Their memory graph,
encoders, consolidation, recall, forgetting, face models and enrollment libraries
are present. They are not registered with the active conversation/camera lifecycle,
and Setup reports them as packaged/disabled rather than offering a nonfunctional
enable checkbox. Activating them requires integrating and validating those runtime
hooks on this device; changing an environment flag alone does not activate them.
No existing personal data, face models, recordings or saved memory databases are
copied into the image. The image also disables legacy retrieval and shutdown
summary generation; ordinary local runtime audio/transcript behavior is unchanged.

The canonical PR #13 memory presentation remains in that development branch;
the packaged memory README describes its libraries, not active OPI4 behavior.

The OPI4 flash profile disables post-session voice analysis. Its Praat package
is in `requirements-orange-pi-voice-analysis.txt` because it has no ARM64 wheel
in this profile and otherwise requires a long on-device compilation. The main
Orange Pi requirements retain the packages needed by the enabled conversation,
pose, wake, eyes and toolkit paths. The SD image builder stages these packages
locally so first boot does not download them over the hotspot.

Run `python check_release.py` in `Baymax-main`, image provisioning checks, then
perform clean-card boot, native imports and concurrent camera/audio/fall/eye tests
on the OPI4. Offline success is not fall-accuracy or ARM throughput evidence.
