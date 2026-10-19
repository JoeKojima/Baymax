"""Small public-data adapters. Inputs are explicit; no automatic location lookup."""
import math
import re
from datetime import datetime, timezone

from .transport import utc_now


def bounded_text(value, name="query"):
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError(f"{name} must contain 1-160 characters")
    return value.strip()


def number(value, low, high, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a number between {low} and {high}")
    return value


def code(value, size, name):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{%d}" % size, value):
        raise ValueError(f"Use a {size}-letter {name} code")
    return value.upper()


class PublicSources:
    def __init__(self, client):
        self.client = client

    def _json(self, url, params=None, ttl=600):
        result = self.client.get(url, params, ttl=ttl)
        return result["data"], {"source_url": url, "fetched_at": result["fetched_at"]}

    def find_place(self, city, country_code=None):
        params = {"name": bounded_text(city, "city"), "count": 5, "language": "en", "format": "json"}
        if country_code is not None:
            params["countryCode"] = code(country_code, 2, "country")
        data, info = self._json("https://geocoding-api.open-meteo.com/v1/search", params, 3600)
        places = [{k: p.get(k) for k in ("name", "admin1", "country", "country_code", "latitude", "longitude", "timezone")}
                  for p in data.get("results", [])[:5]]
        return {**info, "source": "Open-Meteo geocoding", "places": places,
                "needs_clarification": len(places) != 1,
                "notice": "Ask which place if ambiguous. Coordinates describe a city centre, not the user's exact position."}

    def weather(self, latitude, longitude, units="celsius"):
        if units not in ("celsius", "fahrenheit"):
            raise ValueError("Use celsius or fahrenheit")
        params = {"latitude": number(latitude, -90, 90, "latitude"),
                  "longitude": number(longitude, -180, 180, "longitude"),
                  "temperature_unit": units, "timezone": "auto", "forecast_days": 3,
                  "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                  "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"}
        data, info = self._json("https://api.open-meteo.com/v1/forecast", params)
        if not data.get("current") or not data.get("daily"):
            raise ValueError("No weather returned")
        return {**info, "source": "Open-Meteo", **{k: data.get(k) for k in
                ("latitude", "longitude", "timezone", "current", "current_units", "daily", "daily_units")},
                "notice": "Model-based conditions and three-day forecast, not a local sensor reading."}

    def air_quality(self, latitude, longitude):
        params = {"latitude": number(latitude, -90, 90, "latitude"),
                  "longitude": number(longitude, -180, 180, "longitude"),
                  "current": "us_aqi,european_aqi,pm2_5,pm10", "timezone": "auto"}
        data, info = self._json("https://air-quality-api.open-meteo.com/v1/air-quality", params)
        if not data.get("current"):
            raise ValueError("No air quality returned")
        return {**info, "source": "Open-Meteo / CAMS", **{k: data.get(k) for k in
                ("latitude", "longitude", "timezone", "current", "current_units")},
                "notice": "Model-based air quality; specify which AQI scale is used. Not a medical assessment."}

    def exchange_rate(self, base, quote):
        base, quote = code(base, 3, "currency"), code(quote, 3, "currency")
        if base == quote:
            raise ValueError("Choose two different currencies")
        data, info = self._json("https://api.frankfurter.dev/v1/latest", {"base": base, "symbols": quote}, 3600)
        rate = data.get("rates", {}).get(quote)
        number(rate, 0.000000001, 1e12, "rate")
        if not data.get("date"):
            raise ValueError("Missing rate date")
        return {**info, "source": "Frankfurter", "base": base, "quote": quote,
                "rate": rate, "rate_date": data["date"],
                "notice": "Daily reference rate, not a live/tradable quote. Weekends and holidays may use an earlier business day."}

    def holidays(self, country_code, year):
        country_code = code(country_code, 2, "country")
        if type(year) is not int or not 2000 <= year <= 2100:
            raise ValueError("Use a year between 2000 and 2100")
        data, info = self._json(f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}", ttl=86400)
        if not isinstance(data, list):
            raise ValueError("Invalid holiday response")
        return {**info, "source": "Nager.Date", "country_code": country_code, "year": year,
                "holidays": [{k: d.get(k) for k in ("date", "name", "localName", "global", "counties", "types")}
                             for d in data[:60]],
                "notice": "Public holidays; regional applicability is shown. Not a personal calendar."}

    def earthquakes(self, limit=5):
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("Use limit 1-10")
        url = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
        data, info = self._json(url, ttl=300)
        if not isinstance(data.get("features"), list):
            raise ValueError("Invalid earthquake response")
        items = []
        for feature in data["features"]:
            p = feature.get("properties", {})
            stamp = p.get("time")
            if not isinstance(stamp, (int, float)):
                continue
            items.append({"magnitude": p.get("mag"), "place": str(p.get("place", ""))[:160],
                          "time": datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat(),
                          "url": p.get("url")})
        items.sort(key=lambda x: x["time"], reverse=True)
        return {**info, "source": "USGS", "earthquakes": items[:limit],
                "notice": "Recent worldwide M2.5+ events from the past day, not an emergency warning service."}

    def knowledge(self, query):
        params = {"action": "query", "generator": "search", "gsrsearch": bounded_text(query),
                  "gsrlimit": 3, "prop": "extracts|info", "exintro": 1, "explaintext": 1,
                  "exchars": 500, "inprop": "url", "format": "json", "formatversion": 2}
        data, info = self._json("https://en.wikipedia.org/w/api.php", params, 3600)
        return {**info, "source": "Wikipedia", "results": [
            {"title": str(p.get("title", ""))[:160], "excerpt": str(p.get("extract", ""))[:500], "url": p.get("fullurl")}
            for p in data.get("query", {}).get("pages", [])[:3]],
            "notice": "Encyclopedia search, not breaking news. Matches may be ambiguous."}

    def books(self, query):
        data, info = self._json("https://openlibrary.org/search.json",
                                {"q": bounded_text(query), "limit": 5,
                                 "fields": "key,title,author_name,first_publish_year"}, 3600)
        result = []
        for d in data.get("docs", [])[:5]:
            key = d.get("key", "")
            if not re.fullmatch(r"/works/OL\d+W", key):
                continue
            result.append({"title": str(d.get("title", ""))[:160],
                           "authors": [str(a)[:100] for a in d.get("author_name", [])[:3]],
                           "first_publish_year": d.get("first_publish_year"), "url": "https://openlibrary.org" + key})
        return {**info, "source": "Open Library", "books": result,
                "notice": "Book discovery only; this does not download or play books."}
