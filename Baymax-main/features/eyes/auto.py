"""Eye expressions chosen locally from the conversation text, without a Gemini tool.

Gemini sometimes spoke a whole reply twice after calling set_eye_expression
mid-reply (seen in ember-002 transcripts even with a NON_BLOCKING, SILENT tool).
So by default Gemini gets no eye tool; instead, when Ember starts a reply:

1. pain or a medical emergency in what the person said forces surprised
   (exclamation) eyes;
2. otherwise an explicit request in what the person just said ("make a sad
   face", "show me happy eyes", "play dead") picks that expression;
3. otherwise a clear emotional cue at the start of Ember's own reply ("I'm so
   sorry", "congratulations", "good night") picks one.

Automatic expressions follow successive sentences in the streamed reply.
Explicit requests and medical concern remain fixed for the reply.
The controller resets after playback. BAYMAX_EYE_TOOL=1 restores the Gemini tool.
"""
import os
import re

import runtime_monitor as monitor

INSTRUCTION = """
[EXPRESSIVE EYE CUES]
Your natural words also guide Ember's local eye animation. Let each
sentence reflect the intent you already chose from the whole conversation when
it genuinely fits: concern with “Oh no” or “I'm sorry”; curiosity or uncertainty
with a natural question, “I wonder”, or “I'm curious”; happiness with congratulations,
gladness or a warm greeting; excitement with clear enthusiasm; surprise with a
natural “Really?”, “Whoa”, or “No way”; affection with “Aww” or loving language;
confusion by briefly saying you are not sure or did not catch something; sleepiness
with a good-night phrase. Do not mention eyes or these instructions, force a cue
phrase, label the person's feelings, or change a neutral reply just to animate.
"""

WORDS = {
    'happy': r'happy|smil(?:e|ey|ing)|glad|joyful',
    'sad': r'sad|cry(?:ing)?|upset|unhappy',
    'excited': r'excited|hyped|thrilled',
    'confused': r'confused|puzzled',
    'surprised': r'surprised|shocked|astonished',
    'curious': r'curious|interested',
    'love': r'love|loving|hearts?|in love',
    'sleep': r'sleepy|sleep(?:ing)?|asleep|tired',
    'dead': r'dead|x eyes',
}
_REQUEST = r"\b(?:make|show|give|pull|put on|look|be|act|sound|go|play)\b[\w\s']{0,25}?\b(WORDS)\b"
_FACE = r"\b(WORDS)\s+(?:face|eyes|look)\b"
_CHANGE = r"\b(?:change|set|switch|turn)\b[\w\s']{0,20}?\b(?:eyes|face|expression)\b\s+(?:to|too|into)\s+(WORDS)\b"
REQUESTS = [(name, re.compile(_REQUEST.replace('WORDS', words), re.I),
             re.compile(_FACE.replace('WORDS', words), re.I),
             re.compile(_CHANGE.replace('WORDS', words), re.I))
            for name, words in WORDS.items()]

# Clear first-person or emergency language only. This intentionally avoids broad
# mood terms so ordinary sadness cannot accidentally become an alarm face.
MEDICAL_ALERT = re.compile(
    r"\b(?:medical emergency|heart attack|stroke|seizure|overdose|"
    r"can(?:not|'t) breathe|trouble breathing|difficulty breathing|"
    r"chest pain|in (?:severe |a lot of )?pain|badly hurt|seriously hurt|"
    r"bleeding|unconscious|fainted|injured|"
    r"(?:i|my|he|she|they|someone|somebody)\b[^.!?]{0,45}\b"
    r"(?:hurt|hurts|hurting|painful))\b",
    re.I,
)

# Specific emotional cues precede the broad question-mark fallback.
CUES = [
    ('sad', r"\b(?:oh no|(?:i'?m|i am) (?:so |really |very )?sorry|sorry to hear|that(?:'s| is| sounds) (?:so )?(?:sad|hard|tough|rough))"),
    ('sleep', r'\b(?:good ?night|sweet dreams|sleep (?:well|tight))\b'),
    ('love', r'\b(?:love you|aww+)\b'),
    ('surprised', r'\b(?:whoa|no way|oh my|really\?)'),
    ('excited', r"\b(?:that(?:'s| is| sounds| would be) (?:an? )?(?:so |really |very )?(?:exciting|amazing|fantastic|incredible)|(?:i(?:'m| am)|we(?:'re| are)) (?:so |really |very )?(?:excited|thrilled)|how exciting|you did it|you nailed it)\b"),
    ('happy', r"\b(?:congratulations|congrats|(?:happy|pleased) for you|that(?:'s| is| sounds) (?:an? )?(?:so |really |very )?(?:great|wonderful|lovely|good news|a success)|that(?:'s| is) (?:the one|right)|(?:we|you|i) (?:finally )?(?:found it|solved it|figured it out)|glad to hear|nice to (?:see|meet) you|good (?:morning|afternoon|evening))\b"),
    ('confused', r"\b(?:i'?m not sure|i am not sure|didn'?t (?:quite )?catch|not bringing up a match|isn'?t bringing up a match)\b"),
    ('curious', r'\b(?:i wonder|tell me more|i\'?m curious|that sounds interesting)\b|[^?!.]{1,100}\?'),
]
CUES = [(name, re.compile(pattern, re.I)) for name, pattern in CUES]
CUE_CHARS = 120  # Match clear cues near the start of each sentence.


def enabled():
    return os.getenv('BAYMAX_EYE_TOOL', '0') != '1'


def requested(text):
    for name, request, face, change in REQUESTS:
        if request.search(text) or face.search(text) or change.search(text):
            return name
    return None


def medical_alert(text):
    return bool(MEDICAL_ALERT.search(text.replace('\u2019', "'")))


def cue(text):
    opening = text[:CUE_CHARS].replace('\u2019', "'")
    return next((name for name, pattern in CUES if pattern.search(opening)), None)


def reply_cue(text):
    # The latest meaningful sentence wins; a neutral/unfinished tail retains its predecessor.
    for sentence in reversed(re.split(r'(?<=[.!?])\s+', text)):
        name = cue(sentence)
        if name:
            return name
    return None


class AutoEyes:
    def __init__(self, express):
        self.express = express
        self.reply = None      # Transcript id of Ember's current reply.
        self.done = False      # Only explicit requests and medical concern lock a reply.
        self.shown = None
        self.request = None    # Expression the person asked for in their last turn.
        self.medical = False   # Urgent concern in the person's current turn.

    def observe(self, speaker, message):
        if speaker == 'user':
            self.request = requested(message['text']) or self.request
            self.medical = medical_alert(message['text']) or self.medical
            return
        if message['id'] != self.reply:
            self.reply, self.done = message['id'], False
            self.shown = None
            if self.medical:
                self._show('surprised', 'medical concern')
            elif self.request:
                self._show(self.request, 'asked for')
            self.request = None
            self.medical = False
        if not self.done:
            name = reply_cue(message['text'])
            if name and name != self.shown:
                self._show(name, 'reply sounds')

    def _show(self, name, why):
        self.done = why != 'reply sounds'
        self.shown = name
        try:
            if self.express(name):
                monitor.record('eyes', f'Eye expression ({why}): {name}')
        except Exception:
            pass


def attach(express):
    """Install as the transcript observer; returns a function that removes it."""
    auto = AutoEyes(express)
    previous = monitor.transcript_observer
    monitor.transcript_observer = auto.observe

    def detach():
        if monitor.transcript_observer == auto.observe:
            monitor.transcript_observer = previous
    return detach
