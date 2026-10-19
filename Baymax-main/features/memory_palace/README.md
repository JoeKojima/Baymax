# Memory Palace

This PR brings the people-awareness and person-linked memory runtime from #8
alongside #11's tools and shared desktop debugger. The v10 runtime preserves
face consent, corrections, forgetting, scoped recall and session resumption.
`ember_self.py`, `person_memory.py` and `face_id.py` remain the implementation;
this package owns the debugging bridge and future graph integration boundary.

A configured profile, including a profile named James, is background context,
not proof of who is currently speaking. Ember no longer assumes a senior home
or that every speaker is the configured profile owner.

The [graph design](DESIGN.md) describes the next stage: connected relationships,
interests, goals and attributed evidence. That graph, its visualization and
automatic personalized feed ranking are not yet implemented.

## Debugging

The shared #7/#11 Setup and Monitor UI is included. `debug.py` connects v10's
transcripts, tool activity, reply state and memory counts to that same monitor.
The Memory Palace card shows the configured profile separately from visible
people and explicitly leaves speaker identity unverified.

On the supported robot/Linux environment, enable `BAYMAX_DEBUG_MONITOR=1` before
starting `realtime_gemini_10.py`. Open `python devUI.py --monitor` separately.
Both processes must use the same `BAYMAX_MONITOR_DIR` (defaults to
`BAYMAX_DATA_DIR`, then the application directory); create that directory first.
Container and host paths must refer to the same mounted directory. Desktop
clickables still launch v8; v10 depends on the robot's Linux camera/face runtime.
Starting v10 deliberately opens devices and connects to Gemini.

The bridge is disabled by default. It writes local transient status and
transcripts, profile name and visible names for the monitor; it adds no external
service or camera stream. Keep this private runtime data out of Git. Existing
memory extraction/embeddings and audio/video still use the runtime's configured
Gemini services. Public tools use their documented provider endpoints only when
enabled and called. Face records stay in the existing local people store.

Validation: `python check_release.py` covers offline config, mixed tool routing,
recall and monitor integration. The separate `test/` suite exercises person
memory and identity. Live face enrollment, forgetting, resumed conversation and
monitor operation on the physical robot remain pending. The full face/storage
suite requires Linux and Chroma; do not infer hardware parity from Windows tests.
