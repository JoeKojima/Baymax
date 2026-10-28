"""Eye expressions chosen locally from the conversation text, without a Gemini tool.

Gemini sometimes spoke a whole reply twice after calling set_eye_expression
mid-reply (seen in ember-002 transcripts even with a NON_BLOCKING, SILENT tool).
So by default Gemini gets no eye tool; instead, when Ember starts a reply:

1. an explicit request in what the person just said ("make a sad face",
   "show me happy eyes", "play dead") picks that expression;
2. otherwise a clear emotional cue at the start of Ember's own reply ("I'm so
   sorry", "congratulations", "good night") picks one.

At most one expression per reply; the controller resets it after the reply as
before. BAYMAX_EYE_TOOL=1 restores the Gemini tool instead.
"""
import os
import re

import runtime_monitor as monitor

INSTRUCTION = """
[EXPRESSIVE EYE CUES]
Your natural opening words also guide Ember's local eye animation. Let the first
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
REQUESTS = [(name, re.compile(_REQUEST.replace('WORDS', words), re.I),
             re.compile(_FACE.replace('WORDS', words), re.I))
            for name, words in WORDS.items()]

# Cues near the start of Ember's own reply; first match wins.
CUES = [
    ('sad', r"\b(?:oh no|(?:i'?m|i am) (?:so |really |very )?sorry|sorry to hear|that(?:'s| is| sounds) (?:so )?(?:sad|hard|tough|rough))"),
    ('sleep', r'\b(?:good ?night|sweet dreams|sleep (?:well|tight))\b'),
    ('love', r'\b(?:love you|aww+)\b'),
    ('surprised', r'\b(?:whoa|no way|oh my|really\?)'),
    ('excited', r"\b(?:that sounds exciting|how exciting|you did it|you nailed it)\b"),
    ('happy', r'\b(?:congratulations|congrats|happy for you|that(?:\'s| is| sounds) (?:so )?(?:great|wonderful|lovely)|glad to hear|nice to (?:see|meet) you|good (?:morning|afternoon|evening))\b'),
    ('curious', r'\b(?:i wonder|tell me more|i\'?m curious|that sounds interesting)\b|[^?!.]{1,100}\?'),
    ('confused', r"\b(?:i'?m not sure what you mean|didn'?t (?:quite )?catch)"),
]
CUES = [(name, re.compile(pattern, re.I)) for name, pattern in CUES]
CUE_CHARS = 120  # Only the opening of a reply sets the mood.


def enabled():
    return os.getenv('BAYMAX_EYE_TOOL', '0') != '1'


def requested(text):
    for name, request, face in REQUESTS:
        if request.search(text) or face.search(text):
            return name
    return None


def cue(text):
    opening = text[:CUE_CHARS].replace('\u2019', "'")
    return next((name for name, pattern in CUES if pattern.search(opening)), None)


class AutoEyes:
    def __init__(self, express):
        self.express = express
        self.reply = None      # Transcript id of Ember's current reply.
        self.done = False      # One expression per reply.
        self.request = None    # Expression the person asked for in their last turn.

    def observe(self, speaker, message):
        if speaker == 'user':
            self.request = requested(message['text']) or self.request
            return
        if message['id'] != self.reply:
            self.reply, self.done = message['id'], False
            if self.request:
                self._show(self.request, 'asked for')
            self.request = None
        if not self.done:
            name = cue(message['text'])
            if name:
                self._show(name, 'reply sounds')

    def _show(self, name, why):
        self.done = True
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
