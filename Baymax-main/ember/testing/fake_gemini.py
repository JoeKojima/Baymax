"""A scripted stand-in for google-genai's Live client: no network, no key.

`FakeClient().aio.live.connect(model=..., config=...)` behaves like the real
async context manager. The session records everything the runtime sends and
answers from a `Script`: rules that fire when enough microphone audio, a text
turn or a tool response has arrived. `receive()` ends after a turn_complete
message, exactly like google-genai, so the runtime's `while True` re-entry is
exercised. Rules can also drop the connection with a given close code.
"""
import asyncio
import copy
from types import SimpleNamespace


class StopRuntime(BaseException):
    """Raised on a connect past `max_connects`: escapes the runtime's `except Exception` reconnect loop."""


class Closed(Exception):
    """Mimics websockets' ConnectionClosedError text, e.g. 'received 1011 (internal error)'."""


def blob(data=None, mime_type=None):
    return SimpleNamespace(data=data, mime_type=mime_type)


def content(heard=None, said=None, audio=None, text=None, complete=False, interrupted=False):
    parts = []
    if text:
        parts.append(SimpleNamespace(text=text, inline_data=None))
    if audio is not None:
        parts.append(SimpleNamespace(text=None, inline_data=blob(audio, "audio/pcm;rate=24000")))
    server = SimpleNamespace(
        input_transcription=SimpleNamespace(text=heard) if heard is not None else None,
        output_transcription=SimpleNamespace(text=said) if said is not None else None,
        model_turn=SimpleNamespace(parts=parts) if parts else None,
        turn_complete=complete, interrupted=interrupted)
    return message(server_content=server)


def tool_call(name, args=None, call_id="call-1"):
    call = SimpleNamespace(id=call_id, name=name, args=args or {})
    return message(tool_call=SimpleNamespace(function_calls=[call]))


def resumption(handle):
    return message(session_resumption_update=SimpleNamespace(resumable=True, new_handle=handle))


def message(server_content=None, tool_call=None, session_resumption_update=None, go_away=None):
    return SimpleNamespace(server_content=server_content, tool_call=tool_call,
                           session_resumption_update=session_resumption_update, go_away=go_away)


def drop(code=1011):
    return ("drop", code)


class Rule:
    """When `trigger` matches (audio=N bytes since the last rule, text=substring, tool=name), reply."""

    def __init__(self, replies, audio=None, text=None, tool=None, after_connect=False):
        self.replies, self.audio, self.text, self.tool = list(replies), audio, text, tool
        self.after_connect = after_connect


class Script:
    def __init__(self, *rules):
        self.rules = list(rules)

    def next(self, kind, value, audio_since):
        if not self.rules:
            return None
        rule = self.rules[0]
        hit = ((kind == "connect" and rule.after_connect)
               or (kind == "audio" and rule.audio is not None and audio_since >= rule.audio)
               or (kind == "text" and rule.text is not None and rule.text in value)
               or (kind == "tool" and rule.tool is not None and rule.tool in value))
        if hit:
            return self.rules.pop(0)
        return None


class FakeSession:
    def __init__(self, script, record):
        self.script, self.record = script, record
        self.inbox = asyncio.Queue()
        self.audio_since = 0
        self.closed = None

    async def _fire(self, kind, value=None):
        rule = self.script.next(kind, value, self.audio_since)
        if rule is None:
            return
        self.audio_since = 0
        for reply in rule.replies:
            await self.inbox.put(reply)

    async def send_realtime_input(self, audio=None, video=None, **_):
        self._check()
        if audio is not None:
            self.record["audio_chunks"] += 1
            self.record["audio_bytes"] += len(audio.data)
            self.record["audio_mime"].add(audio.mime_type)
            self.audio_since += len(audio.data)
            await self._fire("audio")
        if video is not None:
            self.record["video"].append((len(video.data), video.mime_type))

    async def send_client_content(self, turns=None, turn_complete=True):
        self._check()
        text = "".join(part.get("text", "") for part in (turns or {}).get("parts", []))
        self.record["texts"].append(text)
        await self._fire("text", text)

    async def send_tool_response(self, function_responses=None):
        self._check()
        names = [r.name for r in function_responses or []]
        self.record["tool_responses"].append([(r.name, copy.deepcopy(r.response)) for r in function_responses or []])
        await self._fire("tool", ",".join(names))

    def _check(self):
        if self.closed:
            raise Closed(self.closed)

    async def receive(self):
        while True:
            reply = await self.inbox.get()
            if isinstance(reply, tuple) and reply[0] == "drop":
                self.closed = f"received {reply[1]} (internal error) Internal error occurred."
                raise Closed(self.closed)
            yield reply
            if reply.server_content is not None and reply.server_content.turn_complete:
                return


class _Connect:
    def __init__(self, client, model, config):
        self.client, self.model, self.config = client, model, config

    async def __aenter__(self):
        client = self.client
        client.connects.append({"model": self.model, "config": copy.deepcopy(self.config)})
        if client.max_connects is not None and len(client.connects) > client.max_connects:
            raise StopRuntime()
        if client.refuse:
            raise client.refuse.pop(0)
        script = client.scripts.pop(0) if client.scripts else Script()
        session = FakeSession(script, client.record)
        client.sessions.append(session)
        await session._fire("connect")
        return session

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    """One client across reconnects; `scripts[i]` drives the i-th connection."""

    def __init__(self, *scripts, refuse=(), max_connects=None):
        self.scripts = list(scripts)
        self.max_connects = max_connects
        self.refuse = list(refuse)
        self.connects, self.sessions = [], []
        self.record = {"audio_chunks": 0, "audio_bytes": 0, "audio_mime": set(), "video": [], "texts": [],
                       "tool_responses": []}
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=lambda model, config: _Connect(self, model, config)),
                                   models=None)
