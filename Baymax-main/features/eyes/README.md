# LED eyes

MVP drives two daisy-chained MAX7219 8x8 matrices through the LattePanda onboard
Leonardo running StandardFirmata. No servo, firmware upload, microphone/camera
stream or provider call is added. Python dependency: `pyfirmata2==2.5.1` in
the desktop/Debian x86_64 requirements profile.

## Behavior

- Open hollow eyes with random fast blinks are the default, including while
  listening and speaking. Patterns and timing follow the user's supplied demo:
  0.7–1.4s blink opportunities, 75% chance, 15ms blink with rare double blink.
- User transcription starts/restarts the wait. Thinking appears only when
  **more than two seconds** have elapsed since the last nonblank fragment.
  This is a transcription-based wait estimate, not acoustic end-of-speech.
- The supplied thinking animation moves one 2x2 dot through left/center/right
  on the left eye, then the right eye, at 0.75s per frame. Brightness is 3.
- First reply audio restores normal open/blinking behavior. Turn completion,
  interruption, reconnect and stopping also clear the wait. Late user
  transcription during reply audio does not restart thinking.
- Happy/sad/confused/sleep and the other demo expressions are not activated.

`run_mvp.py` owns the eye thread for the conversation lifetime. Existing
`runtime_monitor.emit()` events and nonblank user-transcription notifications
request state changes only; one render thread owns all serial writes. The
conversation/audio loop never waits for an animation or a Firmata transfer.
The eyes use existing monitor events. The optional waiting-audio feature adds
guarded speaker/microphone hooks; the prompt, models and sample rates stay unchanged.

## Hardware and configuration

Arduino D6 -> eye 1 DIN; D13 -> shared CLK; D10 -> shared CS/LOAD;
5V/GND -> both matrices; eye 1 DOUT -> eye 2 DIN. D9 is unused.
The user's current mounting uses left device 1 and right device 0, with no flips.

Default serial path:
`/dev/serial/by-id/usb-Unknown_LattePanda_Leonardo-if00`.
The user must have serial access (normally membership in `dialout`). Run as the
normal desktop user, not with sudo. Stop standalone eye demos before starting
MVP so only one process owns the Arduino.

Eyes default on for the Linux MVP launcher and off on Windows/macOS. Add to
the local `.env` to override; Setup preserves these values:

```dotenv
BAYMAX_EYES_ENABLED=1
BAYMAX_EYES_PORT=/dev/serial/by-id/usb-Unknown_LattePanda_Leonardo-if00
```

Use `BAYMAX_EYES_ENABLED=0` to disable. Other OS profiles can install pyfirmata2
separately if explicitly enabling eyes. Missing dependency, denied access,
missing hardware or a serial/render error logs `[EYES] Unavailable` and leaves
conversation running. After fixing a connection, restart MVP to reconnect eyes.
Stopping requests shutdown on both matrices and closes serial; a disconnected
device cannot be guaranteed to receive shutdown. No physical second-eye fix is
claimed by this integration.

## Tests

`python -m unittest discover -s tests -p test_eyes.py` checks the strict 2s
boundary, transcription reset, fast replies, late transcripts, reconnect/end,
normal blinking during speech/listening, six single-dot frames, observer wiring
and failure isolation, shutdown, and missing/disabled hardware. A fake board
decodes the actual MAX7219 bitstream to check chain addressing, row orientation,
brightness, diffing/retry and absence of servo commands.

Physical Firmata speed, second-matrix behavior and combined Gemini/audio/eye
operation on ember-002 still require live verification. Pattern hold durations
exclude transfer time; this bit-banged serial path does not guarantee exact
15ms physical blink duration. No keys, transcripts or biometric information are
sent to the Arduino.
