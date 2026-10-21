# Optional CPU fall detection

Enable the fall detection checkbox in Setup, save, and restart Ember. This worker reuses the runtime camera frames and runs MediaPipe on CPU across Windows, Intel/Apple Silicon macOS, and Linux profiles. It does not open another camera or microphone.

See [inputs, outputs, dependencies, data flows, controls and tests](FALL_DETECTION.md). Location/weather and other information tools remain in PR #11. Native hardware validation is still required on each target.
