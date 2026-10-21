"""Bounded, cached HTTPS reads of explicitly supported public providers."""
import copy
import json
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOSTS = frozenset({
    "opentdb.com", "api.tvmaze.com", "poetrydb.org",
    "de1.api.radio-browser.info", "www.themealdb.com",
    "feeds.bbci.co.uk", "www.nasa.gov", "www.cnbc.com",
    "api.open-meteo.com", "geocoding-api.open-meteo.com",
    "air-quality-api.open-meteo.com", "api.frankfurter.dev",
    "date.nager.at", "earthquake.usgs.gov", "en.wikipedia.org", "openlibrary.org",
})
MAX_BYTES = 2_000_000
USER_AGENT = "Ember-Experimental-Toolkit/0.1 (+https://github.com/JoeKojima/Baymax)"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Provider redirected; source configuration needs review")


def read_public(url, params=None):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in HOSTS
            or parts.port not in (None, 443) or parts.username or parts.password):
        raise ValueError("Unsupported public source")
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    with build_opener(NoRedirect()).open(request, timeout=5) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Provider response too large")
    return data


class PublicClient:
    def __init__(self, fetch=read_public, clock=time.monotonic):
        self.fetch, self.clock = fetch, clock
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, url, params=None, ttl=600, xml=False):
        key = (url, urlencode(sorted((params or {}).items())), xml)
        with self.lock:
            entry = self.cache.get(key)
            if entry and self.clock() < entry[0]:
                return copy.deepcopy(entry[1])
        raw = self.fetch(url, params)
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            raise ValueError("Invalid provider response")
        value = raw if xml else json.loads(raw)
        if not xml and not isinstance(value, (dict, list)):
            raise ValueError("Invalid JSON response")
        result = {"data": value, "fetched_at": utc_now()}
        with self.lock:
            if len(self.cache) >= 64:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (self.clock() + ttl, result)
        return copy.deepcopy(result)
