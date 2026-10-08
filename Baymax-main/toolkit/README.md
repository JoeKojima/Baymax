# Ember toolkit

Reusable features live here; they never open microphones or cameras.

## Location and weather

- `location.py`: `Location.get()` estimates city/region via HTTPS IPWho (`ipwho.is`), cached for one hour. It does not return or save the public IP. `set_city(city, country_code, region)` resolves a city explicitly supplied by the user through Open-Meteo geocoding. Multiple results require clarification; nothing changes until one place matches.
- `weather.py`: `Weather.get(units)` returns Open-Meteo current model-based conditions and today's forecast with timestamps, units and provider attribution. Cache lasts ten minutes; new coordinates or units have separate entries. Service failures return an error, not invented or expired conditions.
- `gemini_tools.py`: declares `get_location`, `set_location`, and `get_weather` for Gemini Live, dispatches HTTP work off the audio event loop, and preloads default location/weather in a background thread at startup.

No extra API key or package is required beyond the existing `requests` and Gemini SDK. No location data is written to disk by these tools. Confirmed cities last for the current process only. Gemini's existing transcript/recording behavior still applies.

IP location is approximate and can be wrong, particularly with a VPN or mobile network. It cannot establish an address, indoor room, GPS position or exact user whereabouts. A confirmed city refers to the city centre. The toolkit instruction overrides the upstream scripted Temple University location for actual location questions.

### Data sent to services

IPWho sees the requesting device's public IP. Open-Meteo receives approximate coordinates for weather and user-provided city/region queries for geocoding. Tool results (including approximate coordinates) go to Gemini so it can speak the answer. Tools do not send audio, images, Gemini keys or email credentials to these providers.

### Try it

Start Ember normally and ask “Where am I?” or “What's the weather?” Correct the estimate with “I'm in [city], [state/region], [country]”, then ask about weather again. Ask for Celsius or Fahrenheit as desired. Stop Ember using the existing stop procedure.

Run offline tests: `python -m unittest discover -s tests`.

Set `BAYMAX_TOOLKIT_ENABLED=0` in the private `.env` to disable tool declarations, added instructions and preloading. This preserves the prior conversation config. The Gemini model and original audio/video pipeline have not changed.

### Provider references and deployment

- [IPWho API documentation](https://ipwhois.io/documentation)
- [Open-Meteo weather API](https://open-meteo.com/en/docs)
- [Open-Meteo geocoding API](https://open-meteo.com/en/docs/geocoding-api)
- [Gemini Live function calling](https://ai.google.dev/gemini-api/docs/live-api/tools)

The Open-Meteo public endpoint is for non-commercial use under its current service terms. A commercial Ember deployment needs the appropriate provider plan and deployment configuration; this developer implementation uses the public endpoints. Do not silently switch a fleet to the development endpoint. Review provider terms and rate limits before production use.

### Feature review protocol

Every added/changed toolkit follows [TESTING_PROTOCOL.md](../TESTING_PROTOCOL.md). Add `tests/test_toolkit*.py`, run `python check_release.py`, and document normal, invalid/ambiguous, failure/timeout, core integration and disabled behavior. Provide separate core and toolkit results in the PR.
