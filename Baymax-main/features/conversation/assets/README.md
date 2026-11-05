# Local waiting cue

`hmm.wav` is an intentionally generated synthetic “Hmm.”, not a user recording.
Created offline with Windows System.Speech / Microsoft David Desktop at rate -2,
then silence-trimmed with short margins and scaled to peak 4500 (~14% full scale).
Format: 48,000 Hz, mono, signed 16-bit PCM; duration approximately 0.418 seconds.
No cloud request or voice cloning was used. This local cue is a different voice
from Gemini's configured Fenrir voice; audible fit on the robot is unverified.

The clip is loaded once at startup and consumed through the existing speaker
callback. It opens no audio device itself. Keep replacements nonempty mono
48 kHz 16-bit uncompressed WAVs no longer than two seconds; bad/missing clips
disable the cue without stopping conversation.
