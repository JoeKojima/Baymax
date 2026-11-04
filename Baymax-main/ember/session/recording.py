"""Raw microphone recording to day_utterance/ for voice analysis, with retention pruning (v8)."""
import glob
import os
import queue
import time
import wave

_PRUNE_EVERY_N_SEGMENTS = 60  # segments are 60s, so roughly hourly


class Recorder:
    def __init__(self, directory, rate, retention_days, shutdown_event, maxsize=5000):
        self.directory, self.rate, self.retention_days = directory, rate, retention_days
        self.shutdown_event = shutdown_event
        self.queue = queue.Queue(maxsize=maxsize)
        os.makedirs(directory, exist_ok=True)

    def offer(self, data_bytes):
        """From the mic callback: never blocks, drops when the writer is behind."""
        try:
            self.queue.put_nowait(data_bytes)
        except queue.Full:
            pass

    def prune(self):
        """Delete recorded segments older than the retention window.

        Segments are meant to be transient: VoiceAnalyzer.concatenate_wavs() merges
        them at the end of a session and deletes them. That only happens on a clean
        shutdown, so a power cut or a hard kill leaves them behind forever. This
        directory has filled the disk twice (18 GB, then 12 GB), and a full disk
        stops recording and fall logging entirely.
        """
        if self.retention_days <= 0:
            return  # retention disabled

        cutoff = time.time() - self.retention_days * 86400
        removed = freed = 0

        for path in glob.glob(os.path.join(self.directory, "seg_*.wav")):
            try:
                st = os.stat(path)
                if st.st_mtime >= cutoff:
                    continue
                os.remove(path)
                removed += 1
                freed += st.st_size
            except OSError:
                # Being written, already gone, or permission denied — skip it.
                continue

        if removed:
            print(f"[RECORD] Pruned {removed} segments older than "
                  f"{self.retention_days:g} days ({freed / 1e9:.2f} GB freed)")

    def run(self):
        """Writer thread: 60-second WAV segments until shutdown and the queue is drained."""
        segment_duration = 60
        samples_per_segment = self.rate * segment_duration
        segment_index = 0
        current_samples = 0
        wf = None

        def _open_new_segment():
            nonlocal wf, current_samples, segment_index
            if wf:
                wf.close()

            # Prune before opening, so a long-running session cannot accumulate
            # audio between restarts.
            if segment_index % _PRUNE_EVERY_N_SEGMENTS == 0:
                self.prune()

            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self.directory, f"seg_{ts}_{segment_index:04d}.wav")
            wf = wave.open(path, "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.rate)
            current_samples = 0
            segment_index += 1

        _open_new_segment()  # segment_index is 0 here, so this prunes on startup

        try:
            while not self.shutdown_event.is_set() or not self.queue.empty():
                try:
                    chunk = self.queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                wf.writeframes(chunk)
                current_samples += len(chunk) // 2
                if current_samples >= samples_per_segment:
                    _open_new_segment()
        finally:
            if wf:
                wf.close()
        print(f"[RECORD] Audio writer stopped. {segment_index} segments written.")

    def stop(self, timeout=5):
        self.queue.put(None)
