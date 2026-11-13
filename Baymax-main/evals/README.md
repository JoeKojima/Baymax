# Live evaluation: Gemini in the loop, no robot

`evals/live_eval.py` checks how Gemini behaves with Ember's **real MVP session**
before anyone stands in front of a robot. It plays scripted turns into a fresh
Gemini Live session and checks what Gemini heard, said and which tools it called.
Each scenario runs several times (default 3), because Gemini isn't deterministic.

```bash
python evals/live_eval.py --dry-run                     # what would be sent; connects to nothing
python evals/live_eval.py                               # all scenarios that apply, 3 runs each
python evals/live_eval.py --only weather --repeat 5
python evals/live_eval.py --root ../../Baymax-look-closely/Baymax-main   # test another PR's checkout
```

## What is exercised
- **Session config.** It's built from the core that `run_pc.py` starts
  (`realtime_gemini_10.py` on mvp-v10, `realtime_gemini_8.py` on MVP), parsed without
  importing devices: `MODEL`, the base prompt, `_BASE_CONFIG`, the audio-config hook
  and `_build_config`. On v10 that includes the `[MEMORY CONTEXT]` and `[WHEN TO SPEAK]`
  sections and proactive audio, which follow `memory.retrieval_enabled` and
  `conversation.only_when_addressed` in `/etc/ember/device.toml` (built-in defaults
  when there is none). That includes the MVP persona and the toolkit declarations.
  The environment defaults come from `run_pc.py` and `run_mvp.py`, and the toggles
  from your `.env` apply, as they do on the robot.
- **v10's address gate.** The robot only plays a reply meant for Ember: the person
  said its name, spoke within 20 s of its last reply, or the reply answers one of
  Ember's own notes (`[FALL ALERT]`, ...). When the core has the gate on, each session
  runs the branch's own `AddressGate` over Gemini's messages, so `reply`, the audio
  and first-audio latency are what the robot would have played. Gemini's full reply
  is kept as `gemini_reply`, and a step that needed a reply fails with "the address
  gate dropped Gemini's reply ..." rather than "no reply". `BAYMAX_ADDRESS_GATE=0`
  turns this off, as on the robot. Scenario lines therefore name Ember, except where
  a scenario tests the gate (`overheard`, `follow-up`).
- **Turns.**
  - `say`: macOS `say` (or `espeak-ng`) text-to-speech, sent as 16-bit 48 kHz PCM in
    real time over quiet room noise (2 s lead-in, 1.5 s after) so Gemini's activity detection starts and ends the turn. With the MVP's LOW start sensitivity, Gemini ignored clean speech framed by digital silence.
  - `text`: a client text turn, like the `[FALL ALERT]` and `[ENGAGED]` notes.
  - `card`: a generated 640×480 JPEG of text lines, sent as a camera frame.
  - `image`: a JPEG file next to the scenarios.
- **Checks per turn:**
  - `heard` regex on the input transcript, which catches transcript-matched commands
    such as "Ember disengage".
  - `reply` / `not_reply` regex.
  - `tools` / `not_tools` / `no_tools`.
  - `max_words`.
  - `max_latency_s`: the time from the end of speech to the first reply audio.
  - `silent`: nothing was played (with the gate: Gemini may answer, the robot drops it).
- **Feature scenarios** declare `requires` (a tool name or a prompt marker). They are
  skipped on branches without that feature, so one `scenarios.json` covers the MVP and
  the open PRs.

## What it does not test
The robot's microphone, echo cancellation, speaker, camera, eyes, fall detection, CPU
load, offline Vosk wake words, startup greetings and session tasks. Those still need
the robot (TESTING_PROTOCOL.md §2–3). A pass here is Gemini-behaviour evidence, not
release approval.
`evals/robot/` (Layer 2) covers most of these on a dedicated test robot, reusing
this file's scenario format and helpers.

## Inputs, outputs and data flows
- **Sent to Google Gemini Live:** the MVP system prompt and tool declarations,
  synthetic speech of the scripted lines, the generated cards or images, text notes,
  and the stub tool results. `--dry-run` prints exactly this. A normal run asks before
  connecting unless you pass `--yes`.
- **Not used:** no microphone, camera or speaker is opened, and no recordings are made.
- **Tools.** Tool calls get the scenario's `tool_results` stubs (default `{"result": "ok"}`).
  `--real-tools` runs the real toolkit instead. That contacts its providers (weather,
  discovery services) and audio tools may play sound, so use it deliberately.
- **Key.** `GOOGLE_API_KEY` is read from the environment, else `Baymax-main/.env` of the tested branch, this checkout or the main checkout (when run from a git worktree). Never
  paste it into chat or a PR. Runs use your API quota.
- **Output.** `.local-review/live-eval.md` and `.json` (gitignored), holding transcripts
  of the synthetic speech. The process exits with 1 if any run failed.

## Dependencies
The desktop requirements (`google-genai`, `numpy`, `opencv`, `python-dotenv`), plus
macOS `say`/`afconvert` or `espeak-ng` for speech.

## Enabling and disabling
This is a developer tool. Nothing in the runtime imports it.

## Tests
`tests/test_live_eval.py` (core, offline) builds the real config, validates
`scenarios.json`, checks the expectation logic, and drives a fake Live session (spoken
turn, tool call with a stub, card frames, timeout). It also confirms that a dry run or a
declined prompt connects to nothing.

## Adding a scenario
Add an entry to `scenarios.json`:
- `name`, `about`, `steps` (each with an `expect`).
- Optional `requires` and `tool_results`.

Keep the content synthetic. No real names, medicines or private details.
