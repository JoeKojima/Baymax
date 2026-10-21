"""TV, poetry and radio discovery; no stream playback."""
from urllib.parse import quote

from ..public_sources import PublicSources, bounded_text, code
from ..source_helpers import text, rows


class EntertainmentSources(PublicSources):
    def shows(self, query):
        data, info = self._json("https://api.tvmaze.com/search/shows", {"q": bounded_text(query)}, 3600)
        shows = []
        for item in rows(data)[:3]:
            show = item.get("show")
            if not isinstance(show, dict):
                raise ValueError("Invalid show record")
            shows.append({"name": text(show.get("name")), "id": show.get("id"),
                          "url": text(show.get("url", "")), "status": text(show.get("status", "")),
                          "summary": text(show.get("summary") or "", 700)})
        if len(data) == 1:
            show_id = shows[0]["id"]
            if type(show_id) is not int or show_id <= 0:
                raise ValueError("Invalid show ID")
            details, _ = self._json(f"https://api.tvmaze.com/shows/{show_id}",
                                    {"embed[]": "nextepisode"}, 3600)
            cast, _ = self._json(f"https://api.tvmaze.com/shows/{show_id}/cast", ttl=3600)
            episode = details.get("_embedded", {}).get("nextepisode")
            shows[0]["next_episode"] = ({k: episode.get(k) for k in
                ("name", "season", "number", "airstamp", "url")} if episode else None)
            shows[0]["cast"] = [text(c["person"]["name"], 120) for c in rows(cast)[:5]]
        return {**info, "source": "TVmaze (CC BY-SA)", "shows": shows,
                "notice": "Clarify ambiguous titles. Episode times come from TVmaze; absent next episode means schedule unknown."}

    def poem(self, author=None, title=None):
        fields = "/title,author,lines,linecount"
        if title is not None:  # "The Raven": exact poem, optionally narrowed by author.
            path = "title/" + quote(bounded_text(title, "title"), safe="") + fields
        elif author is not None:
            path = "author/" + quote(bounded_text(author, "author"), safe="") + fields
        else:
            path = "random/1"
        data, info = self._json("https://poetrydb.org/" + path, ttl=3600)
        if isinstance(data, dict) and data.get("status") == 404:
            data = []
        if title is not None and author is not None and isinstance(data, list):
            wanted = bounded_text(author, "author").lower()
            data = [p for p in data if isinstance(p, dict) and wanted in str(p.get("author", "")).lower()] or data
        poems = []
        for p in rows(data)[:1]:
            lines = p.get("lines")
            if not isinstance(lines, list):
                raise ValueError("Invalid poem lines")
            poems.append({"title": text(p.get("title")), "author": text(p.get("author")),
                          "lines": [text(line, 300) for line in lines[:16]], "excerpt": len(lines) > 16})
        return {**info, "source": "PoetryDB", "poems": poems,
                "notice": "Name the poet and title; label an excerpt as an excerpt."}

    def radio(self, genre, country_code=None):
        params = {"tag": bounded_text(genre, "genre"), "limit": 5, "hidebroken": "true"}
        if country_code is not None:
            params["countrycode"] = code(country_code, 2, "country")
        # shortcut: fixed audited mirror; add approved mirror failover if availability suffers.
        data, info = self._json("https://de1.api.radio-browser.info/json/stations/search", params, 3600)
        stations = [{k: text(s.get(k, ""), 300) for k in
                     ("name", "stationuuid", "countrycode", "language", "tags", "homepage")}
                    for s in rows(data)[:5]]
        return {**info, "source": "Radio Browser", "stations": stations,
                "notice": "Station discovery only. Ember cannot play these streams. Country is station origin, not listener location."}
