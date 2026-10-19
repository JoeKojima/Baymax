import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from toolkit import experimental as public
from toolkit.feeds import Headlines, SOURCES, matches, parse_feed
from toolkit.preferences import Preferences
from toolkit.public_sources import PublicSources
from toolkit.transport import MAX_BYTES, NoRedirect, PublicClient, read_public

NOW = datetime(2026, 10, 19, 3, tzinfo=timezone.utc)


def rss(items):
    return ("<rss><channel>" + "".join(
        f"<item><title>{title}</title><link>{url}</link><pubDate>{date}</pubDate></item>"
        for title, url, date in items) + "</channel></rss>").encode()


FRESH = "Mon, 19 Oct 2026 02:00:00 GMT"
FEED = rss([("Artificial intelligence chips", "https://example.org/ai?utm_source=x", FRESH),
            ("Bitcoin prices", "https://example.org/crypto", FRESH),
            ("Rail services resume", "https://example.org/rail", FRESH),
            ("Old news", "https://example.org/old", "Mon, 12 Oct 2026 02:00:00 GMT")])


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "prefs.json"
        self.prefs = Preferences(self.path)

    def test_profiles_survive_restart_without_cross_contamination(self):
        self.prefs.update("alex", categories=["technology"], interests=["AI"])
        self.prefs.update("sam", categories=["sports"])
        restarted = Preferences(self.path)
        self.assertEqual(restarted.get("alex")["interests"], ["ai"])
        self.assertEqual(restarted.get("sam")["interests"], [])

    def test_partial_update_and_explicit_clear(self):
        self.prefs.update("alex", interests=["AI"], avoid=["crypto"])
        self.prefs.update("alex", interests=[])
        self.assertEqual(self.prefs.get("alex")["avoid"], ["crypto"])
        self.assertEqual(self.prefs.get("alex")["interests"], [])

    def test_forget_removes_only_requested_profile(self):
        self.prefs.update("alex", interests=["AI"])
        self.prefs.update("sam", interests=["books"])
        self.prefs.update("alex", forget=True)
        self.assertFalse(self.prefs.get("alex")["saved"])
        self.assertTrue(self.prefs.get("sam")["saved"])

    def test_unknown_read_does_not_create_file(self):
        self.assertFalse(self.prefs.get("alex")["saved"])
        self.assertFalse(self.path.exists())

    def test_invalid_updates_leave_saved_data_unchanged(self):
        self.prefs.update("alex", interests=["AI"])
        before = self.path.read_bytes()
        for args in ({"categories": ["bad"]}, {"interests": "AI"}, {"strict": "yes"},
                     {"interests": ["x"] * 21}, {"avoid": ["x" * 61]}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.prefs.update("alex", **args)
        self.assertEqual(before, self.path.read_bytes())

    def test_path_like_profile_is_rejected(self):
        for name in ("../alex", "Alex", "", "a" * 41, None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.prefs.update(name)

    def test_corrupt_storage_is_not_silently_overwritten(self):
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.prefs.update("alex")
        self.assertEqual(self.path.read_text(), "broken")

    def test_atomic_save_failure_retains_old_profile(self):
        self.prefs.update("alex", interests=["AI"])
        with patch("toolkit.preferences.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.prefs.update("alex", interests=["sports"])
        self.assertEqual(self.prefs.get("alex")["interests"], ["ai"])
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.prefs = Preferences(Path(self.directory.name) / "prefs.json")
        self.client = PublicClient(fetch=lambda *args: FEED)
        self.feed = Headlines(self.client, self.prefs, now=lambda: NOW)

    def test_personalized_ranking_and_exclusions(self):
        self.prefs.update("alex", categories=["technology"], interests=["AI"], avoid=["crypto"])
        result = self.feed.get(profile="alex")
        self.assertEqual([h["title"] for h in result["headlines"]], ["Artificial intelligence chips", "Rail services resume"])
        self.assertEqual(result["headlines"][0]["matched_interests"], ["ai"])

    def test_strict_feed_does_not_fill_with_unrelated_stories(self):
        self.prefs.update("alex", interests=["unmatched topic"], strict=True)
        self.assertEqual(self.feed.get(profile="alex")["headlines"], [])

    def test_general_feed_does_not_read_or_write_profiles(self):
        with patch.object(self.prefs, "get", side_effect=AssertionError("Unexpected profile access")):
            self.assertTrue(self.feed.get()["headlines"])

    def test_missing_personal_profile_does_not_pretend_to_personalize(self):
        result = self.feed.get(profile="new-person")
        self.assertTrue(result["needs_preferences"])
        self.assertEqual(result["headlines"], [])

    def test_empty_profile_is_invalid(self):
        with self.assertRaises(ValueError):
            self.feed.get(profile="")

    def test_query_overrides_do_not_change_saved_categories(self):
        self.prefs.update("alex", categories=["sports"])
        self.feed.get(profile="alex", categories=["technology"])
        self.assertEqual(self.prefs.get("alex")["categories"], ["sports"])

    def test_partial_failure_keeps_other_sources(self):
        def fetch(url, params):
            if url == SOURCES["markets"][1]:
                raise TimeoutError()
            return FEED
        self.feed.client = PublicClient(fetch=fetch)
        result = self.feed.get(categories=["technology", "markets"])
        self.assertTrue(result["partial"])
        self.assertTrue(result["headlines"])
        self.assertEqual(len(result["unavailable_sources"]), 1)

    def test_total_failure_has_no_invented_headlines(self):
        self.feed.client = PublicClient(fetch=lambda *args: (_ for _ in ()).throw(TimeoutError()))
        result = self.feed.get(categories=["technology"])
        self.assertTrue(result["error"])
        self.assertEqual(result["headlines"], [])

    def test_deduplicates_and_filters_old_future_undated_items(self):
        raw = rss([("Same story", "https://example.org/same", FRESH),
                   ("Same story", "https://example.org/same?ref=x", FRESH),
                   ("Future story", "https://example.org/future", "Tue, 20 Oct 2026 02:00:00 GMT"),
                   ("No date", "https://example.org/undated", "")])
        self.feed.client = PublicClient(fetch=lambda *a: raw)
        result = self.feed.get(categories=["technology", "business"])
        self.assertEqual(len(result["headlines"]), 1)

    def test_ai_does_not_match_rail(self):
        self.assertFalse(matches("ai", "Rail services resume"))
        self.assertTrue(matches("ai", "Artificial intelligence advances"))

    def test_invalid_limits_and_categories(self):
        for kwargs in ({"limit": True}, {"limit": 11}, {"hours": 0}, {"categories": []}, {"categories": ["nope"]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.feed.get(**kwargs)

    def test_bad_xml_and_dtd_rejected(self):
        for raw in (b"broken", b"<!DOCTYPE rss><rss/>", b"<html/>"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_feed(raw, "technology")


class TransportTests(unittest.TestCase):
    def test_cache_expires_and_does_not_serve_stale_on_failure(self):
        clock, calls = [0], []
        def fetch(*args):
            calls.append(args)
            if len(calls) > 1:
                raise TimeoutError()
            return b'{"ok":true}'
        client = PublicClient(fetch, clock=lambda: clock[0])
        result = client.get("https://example.org")
        result["data"]["ok"] = False
        self.assertTrue(client.get("https://example.org")["data"]["ok"])
        clock[0] = 601
        with self.assertRaises(TimeoutError):
            client.get("https://example.org")

    def test_unsupported_urls_never_open_network(self):
        with patch("toolkit.transport.build_opener", side_effect=AssertionError("network")):
            for url in ("http://api.open-meteo.com/", "https://localhost/", "https://api.open-meteo.com.evil.org/",
                        "https://user:secret@api.open-meteo.com/", "https://api.open-meteo.com:444/"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    read_public(url)

    def test_redirect_is_rejected(self):
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "http://localhost")

    def test_size_and_json_shape_limits(self):
        for raw in (b"x" * (MAX_BYTES + 1), b"null", b"42", b"bad json"):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                PublicClient(fetch=lambda *args: raw).get("https://example.org")

    def test_http_read_has_timeout_user_agent_and_byte_limit(self):
        response = SimpleNamespace(read=lambda size: b'{}')
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("toolkit.transport.build_opener", return_value=opener):
            self.assertEqual(read_public("https://openlibrary.org/search.json", {"q": "a & b"}), b"{}")
        request = opener.open.call_args.args[0]
        self.assertIn("q=a+%26+b", request.full_url)
        self.assertIn("Ember", request.get_header("User-agent"))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)


class SourceTests(unittest.TestCase):
    def source(self, value):
        return PublicSources(PublicClient(fetch=lambda *args: json.dumps(value).encode()))

    def test_city_ambiguity_returns_choices(self):
        result = self.source({"results": [{"name": "Springfield"}, {"name": "Springfield"}]}).find_place("Springfield")
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(len(result["places"]), 2)

    def test_weather_units_timestamps_and_forecast(self):
        result = self.source({"current": {"time": "2026-10-19T00:00", "temperature_2m": 20},
                              "current_units": {"temperature_2m": "°C"}, "daily": {"time": ["2026-10-19"]}}).weather(40, -75)
        self.assertEqual(result["current_units"]["temperature_2m"], "°C")
        self.assertIn("fetched_at", result)

    def test_air_quality_missing_data_errors(self):
        with self.assertRaises(ValueError):
            self.source({}).air_quality(40, -75)

    def test_currency_is_dated_reference_rate(self):
        result = self.source({"rates": {"EUR": 0.9}, "date": "2026-10-16"}).exchange_rate("usd", "eur")
        self.assertEqual(result["rate_date"], "2026-10-16")
        self.assertIn("not a live", result["notice"])

    def test_holiday_keeps_regional_applicability(self):
        result = self.source([{"name": "Regional holiday", "counties": ["US-CA"], "global": False}]).holidays("US", 2026)
        self.assertEqual(result["holidays"][0]["counties"], ["US-CA"])

    def test_earthquakes_newest_first_and_no_alert_claim(self):
        result = self.source({"features": [{"properties": {"time": 1000}}, {"properties": {"time": 2000}}]}).earthquakes(limit=1)
        self.assertEqual(result["earthquakes"][0]["time"], "1970-01-01T00:00:02+00:00")
        self.assertIn("not an emergency", result["notice"])

    def test_knowledge_excerpt_is_bounded(self):
        result = self.source({"query": {"pages": [{"title": "AI", "extract": "x" * 1000}]}}).knowledge("AI")
        self.assertEqual(len(result["results"][0]["excerpt"]), 500)

    def test_book_search_returns_links_not_playback(self):
        result = self.source({"docs": [{"key": "/works/OL123W", "title": "Robots"}, {"key": "//evil.org"}]}).books("robotics")
        self.assertEqual(len(result["books"]), 1)
        self.assertEqual(result["books"][0]["url"], "https://openlibrary.org/works/OL123W")

    def test_invalid_inputs_do_not_fetch(self):
        source = PublicSources(PublicClient(fetch=lambda *args: self.fail("Unexpected network")))
        calls = [lambda: source.weather(float("nan"), 0), lambda: source.weather(91, 0),
                 lambda: source.air_quality(True, 0), lambda: source.knowledge(""),
                 lambda: source.books("x" * 161), lambda: source.exchange_rate("U", "EUR"),
                 lambda: source.holidays("../US", 2026), lambda: source.holidays("US", True)]
        for call in calls:
            with self.assertRaises(ValueError):
                call()


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"EMBER_PUBLIC_TOOLKIT": "1", "EMBER_PUBLIC_TOOL_GROUPS": ",".join(public.GROUPS)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_disabled_tools_do_no_work(self):
        toolkit = public.Toolkit(client=SimpleNamespace(get=lambda *a: self.fail("network")))
        with patch.dict(os.environ, {"EMBER_PUBLIC_TOOLKIT": "0"}):
            self.assertIn("error", toolkit.execute("public_weather", {"latitude": 0, "longitude": 0}))
            config = {"system_instruction": "base"}
            self.assertIs(public.extend_config(config), config)

    def test_group_switches_and_invalid_config_fail_closed(self):
        with patch.dict(os.environ, {"EMBER_PUBLIC_TOOL_GROUPS": "feeds"}):
            self.assertIn("public_headlines", public.enabled_names())
            self.assertNotIn("public_weather", public.enabled_names())
        with patch.dict(os.environ, {"EMBER_PUBLIC_TOOL_GROUPS": "unknown"}):
            self.assertEqual(public.enabled_names(), set())

    def test_existing_tools_survive_without_mutating_original(self):
        config = {"system_instruction": "base", "tools": [{"function_declarations": [{"name": "remember_person"}]}]}
        result = public.extend_config(config)
        names = [d["name"] for t in result["tools"] for d in t.get("function_declarations", [])]
        self.assertIn("remember_person", names)
        self.assertIn("public_headlines", names)
        self.assertEqual(len(config["tools"]), 1)
        self.assertEqual(public.extend_config(result), result)

    def test_batch_responses_keep_ids_and_event_loop_is_responsive(self):
        calls = SimpleNamespace(function_calls=[SimpleNamespace(name="public_capabilities", args={}, id="call-42")])
        session = SimpleNamespace(send_tool_response=AsyncMock())
        async def run():
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await public.handle_tool_call(session, calls, response_factory=SimpleNamespace)
            self.assertTrue(heartbeat.is_set())
        asyncio.run(run())
        reply = session.send_tool_response.call_args.kwargs["function_responses"][0]
        self.assertEqual(reply.id, "call-42")
        self.assertIn("enabled_tools", reply.response)

    def test_bad_arguments_and_provider_failure_are_results(self):
        toolkit = public.Toolkit(client=SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(OSError("secret"))))
        self.assertIn("error", toolkit.execute("public_weather", {}))
        self.assertIn("error", toolkit.execute("public_weather", {"latitude": 0, "longitude": 0, "evil": True}))
        result = toolkit.execute("public_weather", {"latitude": 0, "longitude": 0})
        self.assertIn("error", result)
        self.assertNotIn("secret", str(result))

    def test_preferences_action_cannot_silently_change_during_read(self):
        result = public.Toolkit().execute("public_feed_preferences", {"profile": "alex", "action": "get", "interests": ["AI"]})
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
