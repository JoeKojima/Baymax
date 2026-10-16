"""Current model-based weather and today's forecast from Open-Meteo."""
import copy
import threading
import time
from datetime import datetime, timezone
import requests
from .location import Location, fetch_json


class Weather:
    def __init__(self, location, fetch=fetch_json, clock=time.monotonic):
        self.location, self.fetch, self.clock = location, fetch, clock
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, units="celsius", city="", country_code="", region=""):
        if units not in ("celsius", "fahrenheit"):
            return {"error": "Choose celsius or fahrenheit."}
        # A forecast for another city must not move the user's current location.
        place = (Location(self.location.fetch).set_city(city, country_code, region)
                 if city else self.location.get())
        if "error" in place or place.get("needs_clarification"):
            return place
        key = (place["latitude"], place["longitude"], units)
        with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock() < cached[0]:
                return {**copy.deepcopy(cached[1]), "location": place, "cached": True}
            try:
                data = self.fetch("https://api.open-meteo.com/v1/forecast", {
                    "latitude": key[0], "longitude": key[1], "temperature_unit": units,
                    "wind_speed_unit": "mph" if units == "fahrenheit" else "kmh",
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "auto", "forecast_days": 1})
                if not data.get("current") or not data.get("daily"):
                    raise ValueError("Weather unavailable")
                result = {"location": place, "current": data["current"], "current_units": data.get("current_units", {}),
                          "today": data["daily"], "daily_units": data.get("daily_units", {}),
                          "timezone": data.get("timezone"), "source": "Open-Meteo", "source_url": "https://open-meteo.com/",
                          "notice": "Model-based weather for these coordinates; IP-derived location may be wrong.",
                          "fetched_at": datetime.now(timezone.utc).isoformat(), "cached": False}
                ttl = 600
            except (requests.RequestException, ValueError, KeyError, TypeError):
                result = {"error": "Weather service is unavailable. Do not invent current conditions.", "location": place}
                ttl = 30
            # Bound the cache when a user visits many cities.
            if len(self.cache) >= 32:
                self.cache.clear()
            self.cache[key] = (self.clock() + ttl, result)
            return copy.deepcopy(result)
