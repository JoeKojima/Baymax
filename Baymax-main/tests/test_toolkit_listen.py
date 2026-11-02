"""Radio and open recordings through the audiobook player; fake network/decoder, no devices."""
import socket
import types
import unittest
from unittest.mock import patch

import numpy as np

from features.toolkits import gemini_tools, listen
from features.toolkits.audiobooks import Player
from features.toolkits.listen import Listen, Recording, Station, license_name
from tests.test_toolkit_audiobooks import BOOK, FakeLibrary, FakeListener, chapters, wait_for

STATION = {"name": "Jazz FM", "url_resolved": "https://jazz.example/stream", "codec": "MP3",
           "hls": 0, "lastcheckok": 1, "countrycode": "GB", "tags": "jazz,smooth"}
ITEM = {"id": "rain", "title": "Rain Sounds", "author": "Nature", "license": "http://creativecommons.org/publicdomain/zero/1.0/",
        "sections": [{"title": "Light rain", "file": "1.mp3"}, {"title": "Storm", "file": "2.mp3"}]}


class Response:
    def __init__(self, status=200, kind="audio/mpeg", location=None):
        self.status_code, self.closed = status, False
        self.headers = {"Content-Type": kind, **({"Location": location} if location else {})}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def close(self):
        self.closed = True


def live(url, rate, cancelled):
    # Endless 4800-sample blocks of 2000 until the player cancels.
    while not cancelled():
        yield np.full(4800, 2000, dtype=np.int16)


def resolver(address):
    return lambda host, port, type=0: [(socket.AF_INET, type, 6, "", (address, port))]


class ListenTests(unittest.TestCase):
    def setUp(self):
        self.notes = []
        self.player = Player(source=FakeLibrary(), decode=chapters, volume=0.5,
                             listener_factory=FakeListener, notify=self.notes.append)
        self.addCleanup(self.player.control, "stop", {})
        self.opened = []

        def opener(url):
            self.opened.append(url)
            return Response(), "MP3"
        self.listen = Listen(player=self.player, stations=lambda query, country, station_id: [STATION],
                             recordings=lambda query: [ITEM], opener=opener, stream=live, archive=FakeLibrary())

    def play_radio(self):
        result = self.listen.execute({"action": "play_radio", "query": "jazz"})
        self.assertTrue(wait_for(lambda: len(self.player.buffer) >= 9600))
        return result

    def test_radio_plays_mixed_at_volume_with_wake_words_and_status(self):
        result = self.play_radio()
        self.assertEqual((result["status"], result["kind"], result["station"], result["wake_words"]),
                         ("starting", "radio", "Jazz FM", "ready"))
        self.assertIn("Hey Ember", result["how_to_pause"])
        self.assertEqual(self.opened, ["https://jazz.example/stream"])  # Checked before playing.
        self.assertIsNone(self.player.render(2048, voice_active=True))  # Ember speaking: radio holds.
        pcm = np.frombuffer(self.player.render(2048, voice_active=False), dtype=np.int16)
        self.assertTrue(np.all(pcm == 1000))
        mic = np.full(480, 7, dtype=np.int16)
        self.assertTrue(self.player.gate(mic))  # Mic withheld from Gemini, heard by wake words.
        status = self.listen.execute({"action": "status"})
        self.assertEqual((status["status"], status["kind"], status["country"], status["volume"]),
                         ("playing", "radio", "GB", 50))
        self.assertIn("error", self.listen.execute({"action": "next"}))

    def test_wake_word_pauses_live_radio_and_resume_reconnects(self):
        self.play_radio()
        generation = self.player.generation
        self.player.listener.on_wake("stop")
        self.assertTrue(self.player.paused)
        self.assertEqual(len(self.player.buffer), 0)  # Not buffering a live stream while paused.
        self.assertIn("[RADIO] The person said 'stop', so Jazz FM (live radio) is paused", self.notes[-1])
        self.assertFalse(self.player.gate(np.zeros(480, dtype=np.int16)))  # Paused: normal conversation.
        self.assertEqual(self.listen.execute({"action": "resume"})["status"], "playing")
        self.assertGreater(self.player.generation, generation + 1)  # A fresh connection to the live stream.
        self.assertTrue(wait_for(lambda: len(self.player.buffer) >= 2048))
        self.assertEqual(self.listen.execute({"action": "stop"}), {"status": "stopped"})

    def test_long_listening_pauses_to_check_in(self):
        with patch.dict("os.environ", {"BAYMAX_LISTEN_CHECK_MIN": "0.001"}):  # 0.06 s.
            self.play_radio()
        for _ in range(4):
            self.player.render(2048, False)
        self.assertTrue(self.player.paused)
        self.assertIn("has played for 0 minutes, so it is paused", self.notes[-1])
        with patch.dict("os.environ", {"BAYMAX_LISTEN_CHECK_MIN": "0"}):
            self.assertIsNone(Station(STATION).limit_seconds)  # 0 never checks in.

    def test_station_that_stops_answering_tells_ember(self):
        def dropping(url, rate, cancelled):
            yield np.full(4800, 2000, dtype=np.int16)
            raise ConnectionError("The station keeps dropping")
        self.listen.stream = dropping
        self.listen.execute({"action": "play_radio", "query": "jazz"})
        self.assertTrue(wait_for(lambda: not self.player.feeding))
        while self.player.render(4096, False):
            pass
        self.assertFalse(self.player.playing)
        self.assertIn("[RADIO] Jazz FM stopped because the station stopped answering", self.notes[-1])

    def test_unplayable_stations_are_skipped_with_a_clear_error(self):
        def refuse(url):
            raise listen.UnsupportedStream("stream type audio/aac")
        self.listen.opener = refuse
        result = self.listen.execute({"action": "play_radio", "query": "jazz"})
        self.assertIn("format Ember can play", result["error"])
        self.assertFalse(self.player.playing)
        self.listen.stations = lambda query, country, station_id: []
        self.assertIn("MP3, Ogg Vorbis and FLAC", self.listen.execute({"action": "play_radio", "query": "x"})["error"])

    def test_recording_plays_tracks_straight_through_with_attribution(self):
        result = self.listen.execute({"action": "play_recording", "query": "rain"})
        self.assertEqual((result["kind"], result["title"], result["creator"], result["license"], result["tracks"]),
                         ("recording", "Rain Sounds", "Nature", "public domain", 2))
        self.assertEqual(result["page"], "https://archive.org/details/rain")
        heard = []

        def step():
            chunk = self.player.render(4096, False)
            if chunk:
                heard.append(np.frombuffer(chunk, dtype=np.int16))
            return not self.player.playing
        self.assertTrue(wait_for(step, timeout=5))
        self.assertEqual(set(np.unique(np.concatenate(heard))) - {0}, {500, 1000})  # Both tracks, no pause.
        self.assertIn("[RECORDING] Rain Sounds by Nature has finished", self.notes[-1])
        self.assertEqual(len(self.notes), 1)

    def test_next_track_and_paused_note(self):
        self.listen.execute({"action": "play_recording", "query": "rain"})
        self.assertEqual(self.listen.execute({"action": "next"})["track"], 2)
        self.assertIn("error", self.listen.execute({"action": "next"}))
        self.assertTrue(wait_for(lambda: len(self.player.buffer) >= 2048))
        self.player.render(2048, False)
        self.player.listener.on_wake("hey ember")
        self.assertIn("Rain Sounds by Nature is paused on track 2 ('Storm')", self.notes[-1])

    def test_one_thing_plays_at_a_time(self):
        self.player.execute({"action": "play", "title": "alice"})
        self.assertEqual(self.player.status()["kind"], "audiobook")
        self.play_radio()
        self.assertEqual(self.player.status()["kind"], "radio")
        self.assertTrue(np.all(np.frombuffer(self.player.render(2048, False), dtype=np.int16) == 1000))
        self.player.execute({"action": "play", "title": "alice"})
        self.assertEqual(self.player.status()["title"], BOOK["title"])
        self.assertEqual(self.listen.execute({"action": "pause"})["status"], "paused")  # Controls the book too.

    def test_invalid_requests_and_missing_decoder(self):
        self.assertIn("error", self.listen.execute({"action": "dance"}))
        self.assertIn("error", self.listen.execute(None))
        self.assertEqual(self.listen.execute({"action": "status"}), {"status": "stopped"})
        self.assertIn("error", self.listen.execute({"action": "pause"}))
        self.listen.recordings = lambda query: []
        self.assertIn("No openly licensed", self.listen.execute({"action": "play_recording", "query": "x"})["error"])
        with patch.dict("sys.modules", {"miniaudio": None}):
            missing = Listen(player=Player(listener_factory=FakeListener))
            self.assertIn("not installed", missing.execute({"action": "play_radio", "query": "jazz"})["error"])
        self.assertIn("error", listen.execute({"action": "play_radio", "station_id": "not-a-uuid"}))


class SourceTests(unittest.TestCase):
    def test_only_open_licenses(self):
        self.assertEqual(license_name("http://creativecommons.org/publicdomain/zero/1.0/"), "public domain")
        self.assertEqual(license_name("http://creativecommons.org/publicdomain/mark/1.0/"), "public domain")
        self.assertEqual(license_name("http://creativecommons.org/licenses/publicdomain/"), "public domain")
        self.assertEqual(license_name("https://creativecommons.org/licenses/by/4.0/"), "CC BY")
        self.assertEqual(license_name("http://creativecommons.org/licenses/by-sa/3.0/us/"), "CC BY-SA")
        for closed in ("http://creativecommons.org/licenses/by-nc/3.0/", "http://creativecommons.org/licenses/by-nd/4.0/",
                       "http://creativecommons.org/licenses/by-nc-sa/3.0/", "", None, "https://example.com/by/"):
            self.assertIsNone(license_name(closed), closed)

    def test_stream_addresses_must_be_public(self):
        self.assertEqual(listen.public_url("http://radio.example/live", resolve=resolver("93.184.216.34")),
                         "http://radio.example/live")
        for address in ("127.0.0.1", "192.168.1.10", "10.0.0.2", "169.254.1.1", "::1"):
            with self.assertRaises(ValueError):
                listen.public_url("http://radio.example/live", resolve=resolver(address))
        for url in ("file:///etc/passwd", "ftp://radio.example/x", "http://user:pw@radio.example/", ""):
            with self.assertRaises(ValueError):
                listen.public_url(url, resolve=resolver("93.184.216.34"))

    def test_open_stream_checks_redirects_and_format(self):
        checked, replies = [], [Response(302, location="/live.mp3"), Response(kind="audio/mpeg; charset=x")]
        response, kind = listen.open_stream("https://radio.example/start", get=lambda url, **kw: replies.pop(0),
                                            check=checked.append)
        self.assertEqual((kind, checked), ("MP3", ["https://radio.example/start", "https://radio.example/live.mp3"]))
        for content, expected in (("application/ogg", "VORBIS"), ("audio/flac", "FLAC")):
            self.assertEqual(listen.open_stream("https://r.example", get=lambda url, **kw: Response(kind=content),
                                                check=lambda url: url)[1], expected)
        aac = Response(kind="audio/aacp")
        with self.assertRaises(listen.UnsupportedStream):
            listen.open_stream("https://r.example", get=lambda url, **kw: aac, check=lambda url: url)
        self.assertTrue(aac.closed)

        def refuse_private(url):
            if "192.168" in url:
                raise ValueError("Stream address is not on the public internet")
        with self.assertRaises(ValueError):  # A redirect cannot reach the home network either.
            listen.open_stream("https://r.example", get=lambda url, **kw: Response(302, location="http://192.168.1.1/"),
                               check=refuse_private)

    def test_stream_reconnects_then_gives_up(self):
        attempts, sleeps = [], []

        class Decoder:
            FileFormat = types.SimpleNamespace(MP3=3)
            SampleFormat = types.SimpleNamespace(SIGNED16=2)

            @staticmethod
            def stream_any(source, kind, **kwargs):
                yield np.ones(4800, dtype=np.int16)
                raise OSError("dropped")

        def opener(url):
            attempts.append(url)
            return Response(), "MP3"
        with patch.object(listen, "_miniaudio_source", lambda response, cancelled: (object(), Decoder)):
            frames = listen.stream_station("https://r.example", 48000, lambda: False, opener=opener, sleep=sleeps.append)
            got = 0
            with self.assertRaises(ConnectionError):
                for _ in frames:
                    got += 1
                    if got > 2:
                        Decoder.stream_any = staticmethod(lambda source, kind, **kwargs: iter(()))  # Nothing more arrives.
        self.assertEqual(len(attempts), 3 + listen.RECONNECTS)
        self.assertEqual(sleeps[-3:], [2, 4, 8])

    def test_stream_stops_when_cancelled(self):
        stop = [False]

        class Decoder:
            FileFormat = types.SimpleNamespace(MP3=3)
            SampleFormat = types.SimpleNamespace(SIGNED16=2)

            @staticmethod
            def stream_any(source, kind, **kwargs):
                while True:
                    yield np.ones(4800, dtype=np.int16)
        response = Response()
        with patch.object(listen, "_miniaudio_source", lambda r, cancelled: (object(), Decoder)):
            frames = listen.stream_station("https://r.example", 48000, lambda: stop[0], opener=lambda url: (response, "MP3"))
            next(frames)
            stop[0] = True
            frames.close()
        self.assertTrue(response.closed)

    def test_find_stations_filters_unplayable_and_falls_back_to_names(self):
        calls = []
        aac = {**STATION, "codec": "AAC+"}
        hls = {**STATION, "hls": 1}
        broken = {**STATION, "lastcheckok": 0}

        def fetch(path, params=None):
            calls.append((path, params))
            return [aac, hls, broken] if params and "tag" in params else [aac, STATION]
        self.assertEqual(listen.find_stations("Jazz FM", "gb", fetch=fetch), [STATION])
        self.assertEqual([("tag" in p, p.get("countrycode")) for _, p in calls], [(True, "GB"), (False, "GB")])
        uuid = "d1a54d2e-623e-4970-ab11-35f7b56c5ec3"
        listen.find_stations(station_id=uuid, fetch=fetch)
        self.assertEqual(calls[-1][0], f"/stations/byuuid/{uuid}")
        for bad in ({"station_id": "../x"}, {"query": "jazz", "country_code": "GBR"}, {"query": " "}):
            with self.assertRaises(ValueError):
                listen.find_stations(fetch=fetch, **bad)

    def test_find_recordings_keeps_only_open_items(self):
        nc = {"id": "nc", "title": "Rain", "author": "", "downloads": 9, "license": "http://creativecommons.org/licenses/by-nc/3.0/"}
        pd = {"id": "pd", "title": "Rain sounds", "author": "", "downloads": 1, "license": "http://creativecommons.org/publicdomain/zero/1.0/"}
        relabelled = {"id": "relabelled", "title": "Rain sounds two", "author": "", "downloads": 1,
                      "license": "http://creativecommons.org/licenses/by/3.0/"}
        queries = []

        def search(query, rows):
            queries.append(query)
            return [nc, pd, relabelled]

        def fetch(identifier):
            if identifier == "relabelled":
                return {**ITEM, "id": identifier, "license": "http://creativecommons.org/licenses/by-nc-nd/3.0/"}
            return {**ITEM, "id": identifier}
        self.assertEqual([i["id"] for i in listen.find_recordings("the rain sounds", search=search, fetch=fetch)], ["pd"])
        self.assertTrue(queries[0].endswith("(rain AND sounds)"))
        self.assertIn("-collection:librivoxaudio", queries[0])
        with self.assertRaises(ValueError):
            listen.find_recordings("the", search=search, fetch=fetch)

    def test_recording_downloads_tracks_like_chapters(self):
        library = FakeLibrary()
        program = Recording(ITEM, library, chapters, minutes=0)
        self.assertEqual(len(list(program.frames(1, 48000, lambda: False))), 3)
        self.assertEqual(library.downloads, [1])
        self.assertIsNone(program.limit_seconds)
        self.assertIsNone(program.checkpoint(0))


class WiringTests(unittest.TestCase):
    def test_tool_and_prompt_follow_the_switch(self):
        with patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_LISTEN_ENABLED": "1"}):
            config = gemini_tools.extend_config({"system_instruction": "base"})
            with patch.object(listen, "execute", return_value={"status": "stopped"}) as run:
                self.assertEqual(gemini_tools.execute("listen", {"action": "status"}), {"status": "stopped"})
            run.assert_called_once_with({"action": "status"})
        names = [d["name"] for t in config["tools"] for d in t.get("function_declarations", [])]
        self.assertIn("listen", names)
        self.assertIn("[RADIO AND RECORDINGS]", config["system_instruction"])
        with patch.dict("os.environ", {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_LISTEN_ENABLED": "0"}):
            config = gemini_tools.extend_config({"system_instruction": "base"})
            self.assertEqual(gemini_tools.execute("listen", {"action": "status"}), {"error": "Unknown tool"})
        names = [d["name"] for t in config["tools"] for d in t.get("function_declarations", [])]
        self.assertNotIn("listen", names)
        self.assertNotIn("[RADIO AND RECORDINGS]", config["system_instruction"])

    def test_shares_the_audiobook_player(self):
        from features.toolkits import audiobooks
        self.assertIs(listen.listen.player, audiobooks.player)


if __name__ == "__main__":
    unittest.main()
