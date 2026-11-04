"""Ember's runtime core, organised as testable parts.

The behaviour is the version-8 runtime's (`realtime_gemini_8.py`, kept unchanged
as the legacy core and rollback). See docs/architecture/ for why and how:

    ember.settings   one Settings object, read once at startup
    ember.devices    microphone / speaker / camera behind one interface (real or virtual)
    ember.session    the Gemini Live session: config, audio, video, receive loop, reconnects
    ember.bus        typed events any feature can subscribe to
    ember.plugins    the ordered feature registry (gates, sources, observers, session tasks)
    ember.profiles   which core, config and features a robot runs (v8, mvp, v9, v10)
    ember.main       `python -m ember --profile mvp [--devices virtual]`
"""
