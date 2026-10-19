"""Explicit single-speaker attribution using Joe's existing person IDs; not authentication."""
import json
import os
from pathlib import Path
import re
import threading
import time


class SpeakerSession:
    def __init__(self, clock=time.monotonic, ttl=900):
        self.clock, self.ttl = clock, ttl
        self.lock = threading.RLock()
        self.person_id = self.name = None
        self.expires = 0
        self.generation = 0
        self.reason = 'Confirm who is speaking; face recognition alone is insufficient.'
        self.absent_since = None
        self.face_bound = False

    def clear(self, reason='Speaker confirmation cleared.'):
        with self.lock:
            if self.person_id:
                self.generation += 1
            self.person_id = self.name = None
            self.expires = 0
            self.absent_since = None
            self.reason = reason

    def observe(self, known, unknown=0):
        with self.lock:
            ids = {p['person_id'] for p in known}
            if len(ids)+unknown > 1:
                self.clear('Several people visible; attribution paused.')
            elif self.person_id and (ids and ids != {self.person_id} or unknown):
                self.clear('Visible person changed or is unrecognized; confirm again.')
            elif self.person_id and self.clock() >= self.expires:
                self.clear('Speaker confirmation expired; confirm again.')
            elif self.person_id and self.face_bound and not ids:
                self.absent_since = self.clock() if self.absent_since is None else self.absent_since
                if self.clock()-self.absent_since >= 10:
                    self.clear('Confirmed face left view; confirm again.')
            elif ids:
                self.absent_since = None
            return self.snapshot()

    def confirm(self, person_id, people, known=(), unknown=0):
        with self.lock:
            person = next((p for p in people if p.get('id') == person_id), None)
            ids = {p['person_id'] for p in known}
            if not person or unknown or len(ids)>1 or ids and ids != {person_id}:
                raise ValueError('Choose one enrolled person, with no conflicting or unknown faces visible.')
            if self.person_id != person_id:
                self.generation += 1
            self.person_id, self.name = person_id, person['name']
            self.face_bound = bool(ids)
            self.absent_since = None
            self.expires = self.clock()+self.ttl
            self.reason = 'Explicit single-speaker confirmation; not biometric speaker verification.'
            return self.snapshot()

    def self_identify(self, text, people, known=(), unknown=0):
        matches = [p for p in people if re.fullmatch(
            r"\s*(?:hi[,! ]+)?(?:my name is|i am|i'm|this is)\s+"+re.escape(p.get('name',''))+r'[.!]?\s*', text, re.I)]
        if len(matches) == 1:
            return self.confirm(matches[0]['id'],people,known,unknown)
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            if self.person_id and self.clock() >= self.expires:
                self.clear('Speaker confirmation expired; confirm again.')
            return {'person_id':self.person_id,'name':self.name,'generation':self.generation,
                    'seconds_remaining':max(0,int(self.expires-self.clock())), 'reason':self.reason}


def command_path(directory):
    return Path(directory)/'.runtime-speaker-command.json'


def queue_confirmation(directory, runtime, snapshot, person_id):
    if not snapshot.get('live') or runtime.get('pid') != snapshot.get('pid'):
        raise ValueError('Start a live Memory Palace session before confirming a speaker.')
    if person_id is not None and not any(p.get('id') == person_id for p in snapshot.get('people', [])):
        raise ValueError('Choose an enrolled person from the current session.')
    path=command_path(directory)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid':runtime['pid'],'issued_at':time.time(),'person_id':person_id}),encoding='utf-8')
    os.replace(temporary,path)


def consume_confirmation(directory, speaker, people, known, unknown):
    path=command_path(directory);processing=path.with_suffix('.processing')
    try:
        os.replace(path,processing)
    except OSError:
        return
    try:
        raw=processing.read_bytes()
        if len(raw)>2048:
            raise ValueError('Invalid speaker command')
        data=json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('Invalid speaker command')
        age=time.time()-float(data.get('issued_at',0))
        if data.get('pid') != os.getpid() or not 0<=age<=30:
            return
        if data.get('person_id') is None:
            speaker.clear()
        else:
            speaker.confirm(data['person_id'],people,known,unknown)
    except (ValueError, TypeError, OSError):
        speaker.clear('Speaker confirmation rejected; refresh and try again.')
    finally:
        try:
            processing.unlink(missing_ok=True)
        except OSError:
            pass
