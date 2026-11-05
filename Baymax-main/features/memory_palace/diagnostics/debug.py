"""Opt-in v10 bridge to the shared local debugging UI; never opens devices."""
import os
import runtime_monitor as monitor


def enabled():
    return os.getenv("BAYMAX_DEBUG_MONITOR", "0") == "1"


def start(sample):
    if enabled():
        monitor.emit("starting", "Starting Memory Palace v10")
        monitor.start(sample)


def state(value, detail):
    if enabled():
        monitor.emit(value, detail)


def transcribe(speaker, text):
    if enabled():
        monitor.transcribe("ember" if speaker == "gemini" else "user", text)


def finish(speaker=None):
    if enabled():
        monitor.finish_transcripts("ember" if speaker == "gemini" else speaker)


def activity(name, running=True):
    if enabled():
        monitor.activity(name, running=running)


_last_inspection = float('-inf')


def inspect_memory(sample):
    """Called only by the monitor thread; keep database reads off the audio loop."""
    global _last_inspection
    if not enabled():
        return
    import time
    from . import inspector
    now = time.monotonic()
    if now - _last_inspection < 5:
        return
    _last_inspection = now
    try:
        snapshot = sample()
        if snapshot is not None:
            inspector.publish(snapshot)
    except Exception:
        inspector.publish({'available': False, 'people': [], 'memories': [],
                           'note': 'Memory snapshot unavailable. Check the runtime memory system.'})
