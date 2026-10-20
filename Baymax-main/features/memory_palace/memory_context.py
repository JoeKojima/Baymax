"""Memory scope supplied by the application or debugger; identification belongs upstream."""
import json
import os
from pathlib import Path
import re
import threading
import time


def validate_context(person_id, name=None):
    if person_id is None:
        return None, None
    if not isinstance(person_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',person_id):
        raise ValueError('Use a stable profile ID containing letters, numbers, dots, hyphens, underscores or colons.')
    if name is None:
        name=person_id
    if not isinstance(name,str) or not name.strip() or len(name)>100:
        raise ValueError('Use a display name up to 100 characters.')
    return person_id,name.strip()


class MemoryContext:
    def __init__(self):
        self.lock=threading.RLock()
        self.person_id=self.name=None
        self.generation=0

    def set_person(self,person_id,name=None):
        person_id,name=validate_context(person_id,name)
        with self.lock:
            if self.person_id!=person_id:
                self.generation+=1
            self.person_id,self.name=person_id,name
            return self.snapshot()

    def clear(self):
        return self.set_person(None)

    def snapshot(self):
        with self.lock:
            return {'person_id':self.person_id,'name':self.name,'generation':self.generation,
                    'reason':'Memory scope supplied by the application; no person identification is performed.'}


def command_path(directory):
    return Path(directory)/'.runtime-memory-context.json'


def queue_context(directory,runtime,snapshot,person_id,name=None):
    if not snapshot.get('live') or runtime.get('pid')!=snapshot.get('pid'):
        raise ValueError('Start a live Memory Palace session before selecting a memory profile.')
    person_id,name=validate_context(person_id,name)
    path=command_path(directory);temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'pid':runtime['pid'],'issued_at':time.time(),'person_id':person_id,'name':name}),encoding='utf-8')
    os.replace(temporary,path)


def consume_context(directory,context):
    path=command_path(directory);processing=path.with_suffix('.processing')
    try:
        os.replace(path,processing)
    except OSError:
        return
    try:
        raw=processing.read_bytes()
        if len(raw)>2048:
            raise ValueError('Invalid context command')
        data=json.loads(raw)
        if not isinstance(data,dict):
            raise ValueError('Invalid context command')
        age=time.time()-float(data.get('issued_at',0))
        if data.get('pid')!=os.getpid() or not 0<=age<=30:
            return
        context.set_person(data.get('person_id'),data.get('name'))
    except (ValueError,TypeError,OSError):
        pass  # Corrupt input never changes an existing valid memory scope.
    finally:
        try:
            processing.unlink(missing_ok=True)
        except OSError:
            pass
