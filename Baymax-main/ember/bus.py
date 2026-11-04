"""Typed, many-subscriber events, so features stop competing for single observer slots.

runtime_monitor keeps one `event_observer` and one `transcript_observer` (the
eyes use both), and a second feature that assigns either slot silently
replaces the first. The monitor now also publishes every state event and
transcript update here; any number of features can subscribe. With no
subscribers nothing changes.

Topics and arguments:
    "state"       (event)                 the same strings monitor.notify passes the eyes
    "transcript"  (speaker, {"id", "text"}) speaker is "user" or "ember"; text is the message so far
A subscriber that raises is dropped and logged once, like the monitor's observers.
"""
import threading

TOPICS = ("state", "transcript")


class Bus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = {topic: [] for topic in TOPICS}

    def subscribe(self, topic, callback):
        if topic not in self.subscribers:
            raise ValueError(f"Unknown topic {topic!r}; known: {', '.join(TOPICS)}")
        with self.lock:
            if callback not in self.subscribers[topic]:
                self.subscribers[topic].append(callback)

        def unsubscribe():
            with self.lock:
                if callback in self.subscribers[topic]:
                    self.subscribers[topic].remove(callback)
        return unsubscribe

    def publish(self, topic, *args):
        subscribers = self.subscribers.get(topic)
        if not subscribers:
            return
        for callback in list(subscribers):
            try:
                callback(*args)
            except Exception as error:
                with self.lock:
                    if callback in subscribers:
                        subscribers.remove(callback)
                print(f"[BUS] {getattr(callback, '__qualname__', callback)} disabled after an error on "
                      f"{topic!r}: {error}", flush=True)


bus = Bus()
