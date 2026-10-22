import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import requests
from features.toolkits.location import Location
from features.toolkits.weather import Weather
from features.toolkits import gemini_tools

IP = {"success": True, "city": "Sampletown", "region": "State", "country": "Country", "country_code": "US", "latitude": 40., "longitude": -75., "ip": "private-unused-value"}
CITY = {"name": "Elsewhere", "admin1": "Other State", "country": "Country", "country_code": "US", "latitude": 41., "longitude": -76.}
WEATHER = {"current": {"time": "2026-10-07T12:00", "temperature_2m": 20, "weather_code": 0}, "daily": {"time": ["2026-10-07"], "temperature_2m_max": [22]}, "timezone": "America/New_York", "current_units": {"temperature_2m": "°C"}}


class ToolkitTests(unittest.TestCase):
    def test_location_cache_expires_and_never_returns_ip(self):
        clock = [0]
        calls = []
        def fetch(*args):
            calls.append(args)
            return IP
        location = Location(fetch, lambda: clock[0])
        self.assertTrue(location.get()["approximate"])
        self.assertNotIn("ip", location.get())
        self.assertEqual(len(calls), 1)
        clock[0] = 3601
        location.get()
        self.assertEqual(len(calls), 2)

    def test_ambiguous_city_does_not_replace_location(self):
        location = Location(lambda *args: {"results": [CITY, {**CITY, "admin1": "Another State"}]})
        result = location.set_city("Elsewhere")
        self.assertTrue(result["needs_clarification"])
        self.assertIsNone(location.confirmed)

    def test_region_selects_confirmed_city_without_ip_lookup(self):
        location = Location(lambda *args: {"results": [CITY, {**CITY, "admin1": "Another State"}]})
        result = location.set_city("Elsewhere", "US", "Other State")
        self.assertEqual(location.get(), result)
        self.assertEqual(result["latitude"], 41.)

    def test_location_failure_retries_after_short_cooldown(self):
        now = [0]
        calls = []
        def fetch(*args):
            calls.append(1)
            raise requests.Timeout()
        location = Location(fetch, lambda: now[0])
        self.assertIn("error", location.get())
        location.get()
        self.assertEqual(len(calls), 1)
        now[0] = 31
        location.get()
        self.assertEqual(len(calls), 2)

    def test_weather_cache_units_and_city_change(self):
        location = Location(lambda *args: IP)
        calls = []
        def fetch(url, params):
            calls.append(params)
            return WEATHER
        weather = Weather(location, fetch)
        self.assertFalse(weather.get()["cached"])
        self.assertTrue(weather.get()["cached"])
        weather.get("fahrenheit")
        self.assertEqual(calls[-1]["temperature_unit"], "fahrenheit")
        location.confirmed = {**CITY, "city": "Elsewhere"}
        weather.get()
        self.assertEqual(calls[-1]["latitude"], 41.)
        self.assertEqual(len(calls), 3)

    def test_weather_failure_has_no_invented_conditions(self):
        weather = Weather(Location(lambda *args: IP), lambda *args: {})
        self.assertIn("error", weather.get())
        self.assertNotIn("current", weather.get())

    def test_other_city_weather_does_not_move_user(self):
        location = Location(lambda url, params: {"results": [CITY]} if 'geocoding' in url else IP)
        weather = Weather(location, lambda *args: WEATHER)
        self.assertEqual(weather.get(city="Elsewhere")["location"]["latitude"], 41.)
        self.assertIsNone(location.confirmed)
        self.assertEqual(location.get()["city"], "Sampletown")

    def test_invalid_coordinates_are_rejected(self):
        self.assertIn("error", Location(lambda *args: {**IP, "latitude": float('nan')}).get())

    def test_disabled_toolkit_preserves_original_config(self):
        with patch.dict('os.environ', {"BAYMAX_TOOLKIT_ENABLED": "0"}), \
                patch.object(gemini_tools.monitor, 'eye_expression_handler', None):
            config = {"system_instruction": "base"}
            result = gemini_tools.extend_config(config)
        # Only the persona's voice is applied; no toolkit tools or instructions.
        self.assertEqual(config, {"system_instruction": "base"})
        self.assertEqual(result["system_instruction"], "base")
        self.assertNotIn("tools", result)

    def test_google_search_grounding_is_added_once_and_can_be_disabled(self):
        base = {"system_instruction": "base", "tools": []}
        with patch.dict('os.environ', {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_GOOGLE_SEARCH": "1"}):
            result = gemini_tools.extend_config(base)
            again = gemini_tools.extend_config(result)
        self.assertEqual([t for t in result["tools"] if "google_search" in t], [{"google_search": {}}])
        self.assertEqual(sum("google_search" in t for t in again["tools"]), 1)
        self.assertIn("[GOOGLE SEARCH]", result["system_instruction"])
        self.assertEqual(base["tools"], [])
        with patch.dict('os.environ', {"BAYMAX_TOOLKIT_ENABLED": "1", "BAYMAX_GOOGLE_SEARCH": "0"}):
            result = gemini_tools.extend_config(base)
        self.assertFalse(any("google_search" in t for t in result["tools"]))
        self.assertNotIn("[GOOGLE SEARCH]", result["system_instruction"])

    def test_tool_response_keeps_gemini_call_id(self):
        session = SimpleNamespace(send_tool_response=AsyncMock())
        call = SimpleNamespace(function_calls=[SimpleNamespace(name="get_location", id="call-42", args={})])
        with patch.object(gemini_tools, 'execute', return_value={"city": "Sampletown"}):
            asyncio.run(gemini_tools.handle_tool_call(session, call))
        reply = session.send_tool_response.call_args.kwargs['function_responses'][0]
        self.assertEqual(reply.id, "call-42")
        self.assertEqual(reply.response, {"city": "Sampletown"})


if __name__ == '__main__':
    unittest.main()
