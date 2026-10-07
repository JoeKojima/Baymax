"""Local activity telemetry. Includes requested speech transcripts; no keys or tool arguments."""
import atexit
import _thread
from collections import deque
import json
import os
from pathlib import Path
import threading
import time

PATH = Path(__file__).with_name('.runtime-status.json')
STOP_PATH = Path(__file__).with_name('.runtime-stop.json')
lock = threading.RLock()
events = deque(maxlen=80)
transcripts = deque(maxlen=60)
active_transcripts = {}
transcript_sequence = 0
state = 'starting'
detail = 'Starting Ember'
activities = {}
active_tool_events = {}
activity_sequence = 0
started = False
stop_event = threading.Event()


def emit(next_state, message):
    global state, detail
    with lock:
        if (state, detail) != (next_state, message):
            events.append({'time': time.time(), 'state': next_state, 'message': message})
        state, detail = next_state, message


def activity(name, running=True, background=False):
    global activity_sequence
    with lock:
        now = time.time()
        if running:
            activity_sequence += 1
            activities[name] = {'background': background, 'since': now}
            event = {'id': 'tool-' + str(activity_sequence), 'time': now, 'state': 'toolkit',
                     'name': name, 'started_at': now, 'finished_at': None, 'background': background,
                     'message': 'Started ' + name + (' (background)' if background else '')}
            active_tool_events[name] = event
            events.append(event)
        else:
            activities.pop(name, None)
            event = active_tool_events.pop(name, None)
            if event is not None:
                event.update(finished_at=now, message='Finished ' + name + (' (background)' if background else ''))


def transcribe(speaker, fragment):
    """Append exact Gemini transcription deltas, preserving token whitespace."""
    global transcript_sequence
    if speaker not in ('user', 'ember') or not isinstance(fragment, str) or not fragment:
        return
    with lock:
        if not fragment.strip() and speaker not in active_transcripts:
            return
        for other in list(active_transcripts):
            if other != speaker:
                active_transcripts[other]['live'] = False
                active_transcripts.pop(other)
        message = active_transcripts.get(speaker)
        if message is None:
            transcript_sequence += 1
            message = {'id': transcript_sequence, 'speaker': speaker, 'text': '', 'time': time.time(), 'live': True}
            transcripts.append(message)
            active_transcripts[speaker] = message
        message['text'] = (message['text'] + fragment)[-8000:]


def finish_transcripts(speaker=None):
    with lock:
        for role in list(active_transcripts):
            if speaker is None or role == speaker:
                active_transcripts.pop(role)['live'] = False


def snapshot(metrics=None):
    with lock:
        current = state
        if metrics and metrics.get('playback_bytes', 0) > 0 and current in ('listening', 'thinking', 'speaking'):
            current = 'speaking'
        return {'pid': os.getpid(), 'heartbeat': time.time(), 'state': current, 'detail': detail,
                'activities': dict(activities), 'events': [dict(event) for event in events], 'metrics': metrics or {},
                'transcripts': [dict(message) for message in transcripts],
                'capabilities': {'memory': 'Disabled in version-8 demo', 'google_search': 'Not implemented',
                                 'toolkit': 'Location and weather'}}


def write_status(metrics=None):
    temporary = PATH.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(snapshot(metrics)), encoding='utf-8')
        os.replace(temporary, PATH)
    except OSError:
        # Diagnostics must never interrupt conversation or shutdown.
        pass


def start(sample=lambda: {}):
    global started
    if started:
        return
    started = True
    def worker():
        while not stop_event.is_set():
            if consume_stop_request():
                emit('stopping', 'Stop requested from clickable launcher')
                write_status()
                _thread.interrupt_main()
            try:
                metrics = sample()
            except Exception:
                metrics = {}
            write_status(metrics)
            stop_event.wait(.2)
    threading.Thread(target=worker, name='ember-monitor', daemon=True).start()
    atexit.register(stop)


def consume_stop_request():
    try:
        request = json.loads(STOP_PATH.read_text(encoding='utf-8'))
        matches = request.get('pid') == os.getpid() and 0 <= time.time() - request.get('requested_at', 0) < 15
        STOP_PATH.unlink(missing_ok=True)
        return matches
    except (OSError, ValueError, TypeError):
        return False


def stop():
    stop_event.set()
    finish_transcripts()
    emit('stopped', 'Ember stopped')
    write_status()


def read_status(path=PATH, now=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Invalid monitor state')
        if (now if now is not None else time.time()) - value['heartbeat'] > 5:
            value.update(state='offline', detail='Ember is offline; showing the last session', activities={})
            for message in value.get('transcripts', []):
                message['live'] = False
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'offline', 'detail': 'Ember is not running', 'events': [], 'activities': {}}
