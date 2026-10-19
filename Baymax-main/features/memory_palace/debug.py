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
