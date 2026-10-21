"""Notice a lost route to Gemini within seconds, for the eyes.

A dropped network leaves the Gemini Live websocket silently stalled for 20-60 s
or more before it reports an error. Every 3 s this opens (and immediately
closes) a TCP connection to the Gemini API host, ~0.5 KB and no audio or
credentials. After failing continuously for `offline_after` seconds (so 6-9 s
after the drop) it reports 'network_lost', and 'network_ok' as soon as a check
succeeds again.
"""
import socket
import threading
import time

GEMINI_HOST = "generativelanguage.googleapis.com"


class NetworkWatch:
    def __init__(self, report, host=GEMINI_HOST, port=443, offline_after=5.0, interval=3.0,
                 timeout=2.0, connect=socket.create_connection, clock=time.monotonic):
        self.report, self.host, self.port = report, host, port
        self.offline_after, self.interval, self.timeout = offline_after, interval, timeout
        self.connect, self.clock = connect, clock
        self.failing_since = None
        self.offline = False
        self.stopping = threading.Event()
        self.thread = None

    def reachable(self):
        try:
            self.connect((self.host, self.port), timeout=self.timeout).close()
            return True
        except OSError:
            return False

    def check(self):
        if self.reachable():
            self.failing_since = None
            if self.offline:
                self.offline = False
                print("[EYES] Network back.", flush=True)
                self.report("network_ok")
            return
        now = self.clock()
        if self.failing_since is None:
            self.failing_since = now
        if not self.offline and now - self.failing_since >= self.offline_after:
            self.offline = True
            print(f"[EYES] Cannot reach Gemini for {self.offline_after:g}s; showing confused.", flush=True)
            self.report("network_lost")

    def run(self):
        while not self.stopping.wait(self.interval):
            try:
                self.check()
            except Exception:
                pass  # Diagnostics must never stop the eyes or the conversation.

    def start(self):
        self.thread = threading.Thread(target=self.run, name="ember-network-watch", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
