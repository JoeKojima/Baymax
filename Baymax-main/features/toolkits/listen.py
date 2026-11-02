"""Live internet radio and openly licensed recordings, played like audiobooks.

Both run through the audiobook `Player`: mixed into Ember's own speaker stream,
held while Ember speaks, the mic withheld from Gemini with the offline wake words
("Hey Ember", "stop", "pause") pausing playback, one program at a time.

Radio: stations come from Radio Browser (community directory; only the search
terms or a station ID are sent). The stream itself is read on the feeder thread
and decoded incrementally by miniaudio, which handles MP3, Ogg Vorbis and FLAC;
AAC and HLS stations are skipped. A station that drops is reconnected a few
times before Ember is told it stopped. Paused radio is not buffered: resume
reconnects to the live stream. After `BAYMAX_LISTEN_CHECK_MIN` minutes (default
30) playback pauses and Ember asks whether they want to keep listening.

Recordings: Internet Archive audio items whose license is public domain, CC0,
CC BY or CC BY-SA (the Open Definition licenses; NC/ND items are skipped).
Tracks download one at a time like audiobook chapters and play without pausing
between them. Licenses are as stated by each uploader.
"""
import ipaddress
import os
import re
import socket
import time
from urllib.parse import urljoin, urlsplit

import requests

import runtime_monitor as monitor
from . import librivox
from .audiobooks import player as shared_player
from .transport import USER_AGENT

NAME = "listen"
ACTIONS = ["play_radio", "play_recording", "pause", "resume", "stop", "next", "status", "volume"]
RADIO_API = "https://de1.api.radio-browser.info/json"
PLAYABLE_CODECS = {"MP3", "OGG", "FLAC"}
STREAM_FORMATS = {"audio/mpeg": "MP3", "audio/mp3": "MP3", "audio/mpeg3": "MP3",
                  "audio/ogg": "VORBIS", "application/ogg": "VORBIS", "audio/vorbis": "VORBIS",
                  "audio/flac": "FLAC", "audio/x-flac": "FLAC"}
OPEN_LICENSES = (("/publicdomain/", "public domain"), ("/licenses/publicdomain", "public domain"),
                 ("/licenses/by/", "CC BY"), ("/licenses/by-sa/", "CC BY-SA"))
HTTP_TIMEOUT = (5, 10)   # Connect, and seconds without stream data before reconnecting.
RECONNECTS = 3
MAX_REDIRECTS = 3
MAX_TRACKS = 100
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

DECLARATION = {
    "name": NAME,
    "description": "Play live internet radio or openly licensed recordings (music, nature sounds, old radio "
                   "shows) and control whatever is playing, including an audiobook.",
    "parameters": {"type": "OBJECT", "properties": {
        "action": {"type": "STRING", "enum": ACTIONS},
        "query": {"type": "STRING", "description": "Genre, station name or what to hear, for play_radio/play_recording."},
        "station_id": {"type": "STRING", "description": "A stationuuid from public_radio, for play_radio (optional)."},
        "country_code": {"type": "STRING", "description": "Two-letter station country, for play_radio (optional)."},
        "level": {"type": "NUMBER", "description": "Volume 0-100 percent, for volume."}},
        "required": ["action"]},
}
INSTRUCTION = """
[RADIO AND RECORDINGS]
With the listen tool you can play live internet radio (play_radio with a genre
or station name, or a stationuuid from public_radio as station_id, optional
country_code) and openly licensed or public-domain recordings from the Internet
Archive (play_recording with what they want to hear: a composer, a style, nature
sounds, old radio shows). pause, resume, stop, next (recordings only), status
and volume (0-100) control whatever is playing, including an audiobook; starting
one stops the other. When something starts, name the station, or the recording
with its creator and license ("public domain", "CC BY"), and say they can say
"Hey Ember" or "stop" any time. Recordings are volunteer uploads: do not promise
a famous performance, and popular commercial songs are usually not there; say so
kindly and offer a radio station instead.
While it plays you only hear those pause words. A message starting with
'[RADIO]' or '[RECORDING]' tells you it paused or stopped and why.
"""


def enabled():
    return os.getenv("BAYMAX_LISTEN_ENABLED", "1") != "0"


def check_minutes():
    """BAYMAX_LISTEN_CHECK_MIN (default 30; 0 never pauses for a check-in)."""
    try:
        return max(0.0, float(os.getenv("BAYMAX_LISTEN_CHECK_MIN", "30")))
    except ValueError:
        return 30.0


def license_name(url):
    """'public domain', 'CC BY' or 'CC BY-SA' for an open license URL, else None."""
    url = (url or "").lower()
    if "creativecommons.org" not in url or "-nc" in url or "-nd" in url:
        return None
    return next((name for marker, name in OPEN_LICENSES if marker in url), None)


# ── Stream access ─────────────────────────────────────────────────────────────
def public_url(url, resolve=socket.getaddrinfo):
    """Refuse non-HTTP URLs and hosts on this robot's own network (station URLs are third-party data)."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Unsupported stream address")
    addresses = {info[4][0] for info in resolve(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)}
    if not addresses or not all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses):
        raise ValueError("Stream address is not on the public internet")
    return url


class UnsupportedStream(ValueError):
    pass


def open_stream(url, get=requests.get, check=public_url):
    """(response, miniaudio format name) for a playable stream; follows a few checked redirects."""
    for _ in range(MAX_REDIRECTS + 1):
        check(url)
        response = get(url, stream=True, timeout=HTTP_TIMEOUT, allow_redirects=False,
                       headers={"User-Agent": USER_AGENT})
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
            response.close()
            url = urljoin(url, response.headers["Location"])
            continue
        response.raise_for_status()
        kind = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if kind not in STREAM_FORMATS:
            response.close()
            raise UnsupportedStream(f"stream type {kind or 'unknown'} (Ember plays MP3, Ogg Vorbis and FLAC)")
        return response, STREAM_FORMATS[kind]
    raise ValueError("Too many redirects")


def _miniaudio_source(response, cancelled):
    import miniaudio

    class Source(miniaudio.StreamableSource):
        def read(self, num_bytes):
            # Runs on the feeder thread inside the decoder; empty bytes end the stream.
            if cancelled():
                return b""
            return response.raw.read(num_bytes)

    return Source(), miniaudio


def stream_station(url, rate, cancelled, opener=open_stream, sleep=time.sleep):
    """Decoded live audio until cancelled; reconnects when the station drops."""
    failures = 0
    while not cancelled():
        response = None
        try:
            response, kind = opener(url)
            source, miniaudio = _miniaudio_source(response, cancelled)
            for frames in miniaudio.stream_any(source, getattr(miniaudio.FileFormat, kind),
                                               output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1,
                                               sample_rate=rate, frames_to_read=4800):
                failures = 0
                yield frames
        except UnsupportedStream:
            raise
        except Exception as error:
            if cancelled():
                return
            print(f"[RADIO] Stream dropped ({type(error).__name__}).", flush=True)
        finally:
            if response is not None:
                response.close()
        if cancelled():
            return
        failures += 1
        if failures > RECONNECTS:
            raise ConnectionError("The station keeps dropping")
        sleep(min(8, 2 ** failures))


def _radio_json(path, params=None):
    response = requests.get(RADIO_API + path, params=params, timeout=10, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("Invalid station list")
    return data


def playable(station):
    return (isinstance(station, dict) and str(station.get("codec", "")).upper() in PLAYABLE_CODECS
            and not station.get("hls") and station.get("lastcheckok", 1) == 1
            and str(station.get("url_resolved") or "").startswith(("http://", "https://")))


def find_stations(query=None, country_code=None, station_id=None, fetch=_radio_json):
    """Up to 5 playable stations, most listened first (Radio Browser)."""
    if station_id:
        if not UUID.match(str(station_id).lower()):
            raise ValueError("Invalid station ID")
        found = fetch(f"/stations/byuuid/{str(station_id).lower()}")
    else:
        query = re.sub(r"\s+", " ", str(query or "")).strip()[:80]
        if not query:
            raise ValueError("Give a genre or station name")
        params = {"limit": 30, "hidebroken": "true", "order": "clickcount", "reverse": "true"}
        if country_code:
            if not re.fullmatch(r"[A-Za-z]{2}", str(country_code)):
                raise ValueError("Invalid country code")
            params["countrycode"] = str(country_code).upper()
        found = fetch("/stations/search", {**params, "tag": query.lower()})
        if not any(playable(s) for s in found):
            found = fetch("/stations/search", {**params, "name": query})
    return [s for s in found if playable(s)][:5]


def find_recordings(query, search=librivox._ia_search, fetch=librivox.fetch_book):
    """Openly licensed Internet Archive audio items with MP3 tracks, best first (at most 3 tried)."""
    # Plain words: archive.org full-text search ignores the wildcards the audiobook title search uses.
    words = [w for w in librivox._clean_query(query).lower().split() if w not in librivox._STOPWORDS][:6]
    if not words:
        raise ValueError("Say what to play")
    found = search("mediatype:audio AND -collection:librivoxaudio AND licenseurl:*creativecommons* AND ("
                   + " AND ".join(words) + ")", rows=50)
    items = []
    for result in librivox._rank([r for r in found if license_name(r.get("license"))], query)[:3]:
        try:
            item = fetch(result["id"])
        except Exception:
            continue  # No MP3 files (FLAC/Ogg-only items) or metadata unavailable.
        if license_name(item.get("license")):  # The item's own metadata decides.
            items.append({**item, "sections": item["sections"][:MAX_TRACKS]})
    return items


# ── Programs for the shared player ────────────────────────────────────────────
class Station:
    kind, label, live = "radio", "RADIO", True
    no_next = "Live radio has no next track; play another station instead"

    def __init__(self, station, stream=stream_station, minutes=None):
        self.station, self.stream = station, stream
        self.name = re.sub(r"\s+", " ", str(station.get("name") or "the station")).strip()[:80]
        self.sections = [{"title": self.name}]
        self.limit_seconds = (check_minutes() if minutes is None else minutes) * 60 or None

    def frames(self, index, rate, cancelled):
        return self.stream(self.station["url_resolved"], rate, cancelled)

    def checkpoint(self, index):
        return None

    def paused_at(self, index):
        return f"{self.name} (live radio) is paused"

    def finished(self, error=None):
        why = "the station stopped answering" if error is not None else "the station ended its stream"
        return f"[RADIO] {self.name} stopped because {why}. Tell them briefly and offer another station."

    def still_listening(self, minutes):
        return (f"[RADIO] {self.name} has played for {minutes} minutes, so it is paused. "
                "Briefly ask whether they want to keep listening.")

    def status(self, index):
        return {"kind": self.kind, "station": self.name,
                "country": str(self.station.get("countrycode", ""))[:2],
                "tags": str(self.station.get("tags", ""))[:120], "source": "Radio Browser"}


class Recording:
    kind, label, live = "recording", "RECORDING", False
    no_next = "Already on the last track"

    def __init__(self, item, source, decode, minutes=None):
        self.item, self.source, self.decode = item, source, decode
        self.sections = item["sections"]
        self.license = license_name(item.get("license"))
        self.limit_seconds = (check_minutes() if minutes is None else minutes) * 60 or None

    def frames(self, index, rate, cancelled):
        return self.decode(self.source.download(self.item, index, cancelled=cancelled), rate)

    def checkpoint(self, index):
        return None  # Albums play straight through; the long-listening check-in still applies.

    def _named(self):
        return f"{self.item['title']}" + (f" by {self.item['author']}" if self.item.get("author") else "")

    def paused_at(self, index):
        return f"{self._named()} is paused on track {index + 1} ('{self.sections[index]['title']}')"

    def finished(self, error=None):
        if error is not None:
            return (f"[RECORDING] {self._named()} stopped because it could not be downloaded or played. "
                    "Tell them briefly and offer something else.")
        return f"[RECORDING] {self._named()} has finished. Ask whether they would like something else."

    def still_listening(self, minutes):
        return (f"[RECORDING] {self._named()} has played for {minutes} minutes, so it is paused. "
                "Briefly ask whether they want to keep listening.")

    def status(self, index):
        return {"kind": self.kind, "title": self.item["title"], "creator": self.item.get("author", ""),
                "license": self.license, "track": index + 1, "tracks": len(self.sections),
                "track_title": self.sections[index]["title"], "source": "Internet Archive",
                "page": f"https://archive.org/details/{self.item['id']}"}


class Listen:
    def __init__(self, player=shared_player, stations=find_stations, recordings=find_recordings,
                 opener=open_stream, stream=stream_station, archive=librivox):
        self.player, self.stations, self.recordings = player, stations, recordings
        self.opener, self.stream, self.archive = opener, stream, archive

    def execute(self, args):
        if not isinstance(args, dict) or args.get("action") not in ACTIONS:
            return {"error": "Expected an action: " + ", ".join(ACTIONS)}
        action = args["action"]
        if action not in ("play_radio", "play_recording"):
            return self.player.control(action, args)
        if self.player.decoder_missing():
            return {"error": "Audio playback is not installed on this robot"}
        if action == "play_radio":
            return self.play_radio(args)
        return self.play_recording(args)

    def play_radio(self, args):
        stations = self.stations(args.get("query"), args.get("country_code"), args.get("station_id"))
        skipped = []
        for station in stations[:3]:
            try:
                response, _ = self.opener(station["url_resolved"])  # Check it answers with audio we can decode.
                response.close()
            except Exception as error:
                skipped.append(f"{str(station.get('name', ''))[:60]}: {type(error).__name__}")
                continue
            program = Station(station, self.stream)
            wake_words = self.player.play(program)
            monitor.record("radio", "Playing " + program.name)
            return {"status": "starting", **program.status(0), "codec": station.get("codec"),
                    "how_to_pause": "say 'Hey Ember' or 'stop'", "wake_words": wake_words}
        if skipped:
            return {"error": "Those stations are not answering in a format Ember can play", "tried": skipped}
        return {"error": "No playable station found (Ember plays MP3, Ogg Vorbis and FLAC streams, not AAC or HLS)"}

    def play_recording(self, args):
        items = self.recordings(args.get("query"))
        if not items:
            return {"error": "No openly licensed recording found", "query": str(args.get("query", ""))[:80]}
        program = Recording(items[0], self.archive, self.player.decode)
        wake_words = self.player.play(program)
        monitor.record("recording", "Playing " + program.item["title"][:60])
        return {"status": "starting", **program.status(0), "how_to_pause": "say 'Hey Ember' or 'stop'",
                "wake_words": wake_words}


listen = Listen()


def execute(args):
    try:
        return listen.execute(args)
    except ValueError as error:
        return {"error": str(error)[:120]}
