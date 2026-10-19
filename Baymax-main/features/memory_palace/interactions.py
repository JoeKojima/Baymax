"""Evidence about an interaction, separate from facts, identity and inferred satisfaction."""
import math
import re
import threading
import time

FEELINGS = {'happy':1,'excited':1,'proud':1,'sad':-1,'stressed':-1,'frustrated':-1,
            'overwhelmed':-1,'anxious':-1,'calm':0,'tired':None}
MOOD_TTL = 1800
FEEDBACK = (
    ('repeat_request', r'\b(?:play (?:that|it) again|more like (?:this|that))\b'),
    ('timing_boundary', r'\b(?:not now|give me (?:some )?quiet|leave me alone|let me work)\b'),
    ('brevity_request', r'\b(?:keep it short|shorter (?:version|answer)|short version please)\b'),
    ('correction', r"\b(?:that(?:'s| is) wrong|I meant|I already know (?:this|that))\b"),
    ('explicit_positive', r"\b(?:that was helpful|I enjoyed (?:that|this)|I (?:love|like) that (?:song|suggestion))\b"),
    ('explicit_negative', r"\b(?:I (?:didn't|did not) (?:like|enjoy) (?:that|this)|I dislike that (?:song|suggestion))\b"),
)


def clean_metrics(raw):
    """Persist a small whitelist; no waveform or fabricated speed/satisfaction score."""
    if not isinstance(raw,dict):
        return {}
    out={}
    for key, maximum in (('word_count',4000),('transcript_span_ms',180000),('response_gap_proxy_ms',180000)):
        value=raw.get(key)
        if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and 0<=value<=maximum:
            out[key]=round(value,1)
    if isinstance(raw.get('overlap_observed'),bool):out['overlap_observed']=raw['overlap_observed']
    for key,limit in (('profile_label',100),('reply_reference',240)):
        value=raw.get(key)
        if isinstance(value,str) and value.strip():out[key]=value.strip()[:limit]
    if 'response_gap_proxy_ms' in out or 'transcript_span_ms' in out:
        out['timing_source']='transcript_arrival_proxy'
    return out


def encode_interaction(text, timestamp, metrics=None):
    metrics=clean_metrics(metrics)
    emotions=[];feedback=[]
    # Deliberately narrow: questions, quoted/reported and hypothetical speech are not mood evidence.
    eligible=not ('?' in text or any(c in text for c in ('"','“','”')) or
                  re.search(r'\b(if|suppose|pretend|example|said|says|told|claims)\b',text,re.I))
    if eligible:
        pattern=re.compile(r"\bI(?: am|'m| feel)(?: feeling)? (?:really |so |very |a bit )?(not )?("+'|'.join(FEELINGS)+r')\b(?:\s+(?:about|with|because of|when|for)\s+([^.!?\n]{1,120}))?',re.I)
        for m in pattern.finditer(text):
            prefix=text[:m.start()].strip()
            if prefix and not prefix.endswith(('.', '!', ';')) and not re.fullmatch(r'(?:yeah|yes|honestly|actually|today)[, ]*',prefix,re.I):
                continue
            label=m[2].lower();negated=bool(m[1]);target=re.split(r'\s+(?:but|and I)\s+',m[3] or '',flags=re.I)[0].strip(' ,')
            # A self-report is evidence, not a verified psychological measurement.
            emotions.append({'label':('not ' if negated else '')+label,'valence':None if negated else FEELINGS[label],
                'target':target or None,'quote':m[0],'source':'self_report','status':'explicit report',
                'observed_at':timestamp,'expires_at':timestamp+MOOD_TTL,'negated':negated})
        for kind,pattern in FEEDBACK:
            m=re.search(pattern,text,re.I)
            if m:
                feedback.append({'kind':kind,'quote':m[0],'source':'explicit_phrase',
                    'target':'previous_ember_turn_candidate' if metrics.get('reply_reference') else 'unresolved',
                    'status':'context-specific reaction; not a satisfaction rating'})
    return {'metrics':metrics,'emotions':emotions[:4],'feedback':feedback[:4],
            'note':'Unknown is not negative. Timing includes transcription/provider delays; no speech speed is inferred.'}


class InteractionTracker:
    """Use transcript receipts without changing audio/VAD or opening devices."""
    def __init__(self, clock=time.monotonic):
        self.clock=clock;self.lock=threading.RLock();self.active={};self.reply=None;self.scope=None

    def _scope(self,context):
        scope=(context.get('person_id'),context.get('generation'))
        if scope!=self.scope:
            self.active.clear();self.reply=None;self.scope=scope
        return scope

    def fragment(self,speaker,context):
        with self.lock:
            scope=self._scope(context);now=self.clock()
            if speaker not in self.active:
                metrics={}
                if speaker=='user':
                    if context.get('name'):metrics['profile_label']=context['name']
                    metrics['overlap_observed']='ember' in self.active
                    if self.reply is not None and 'ember' not in self.active:
                        gap=(now-self.reply['end'])*1000
                        if 0<=gap<=180000:
                            metrics['response_gap_proxy_ms']=gap
                            metrics['reply_reference']=self.reply['text']
                self.active[speaker]={'start':now,'last':now,'fragments':0,'scope':scope,'metrics':metrics}
            state=self.active[speaker];state['last']=now;state['fragments']+=1

    def finish(self,speaker,text,context):
        with self.lock:
            scope=self._scope(context);state=self.active.pop(speaker,None)
            if not state or state['scope']!=scope or not text.strip():
                return {}
            if speaker=='ember':
                self.reply={'end':self.clock(),'text':text[:240]}
                return {}
            metrics=dict(state['metrics']);metrics['word_count']=len(text.split())
            if state['fragments']>1:metrics['transcript_span_ms']=(state['last']-state['start'])*1000
            return clean_metrics(metrics)


def current_affect(interaction, value, now=None):
    """Only a recent explicit report whose stated target overlaps this fact's text."""
    now=time.time() if now is None else now
    words=lambda text:set(re.findall(r'\b[a-z]{3,}\b',text.casefold()))-{'the','and','with','about','that','this','feel','feeling'}
    out=[]
    for emotion in (interaction or {}).get('emotions',[]):
        if emotion.get('source')=='self_report' and emotion.get('expires_at',0)>now and emotion.get('target') and words(emotion['target'])&words(value):
            out.append({'feeling':emotion['label'],'about':emotion['target'][:100],'source':'self_report'})
    return out[:2]
