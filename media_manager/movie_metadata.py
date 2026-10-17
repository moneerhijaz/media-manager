"""Bounded TMDB identity lookup. Credentials are read from the environment only."""
import json
import os
import re
from datetime import date, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .http_policy import open_with_retry, ProviderDeferred


VERSION = "tmdb-identity-v1"


def lookup(row, region="US"):
    token = os.environ.get("TMDB_READ_TOKEN")
    if not token or row["media_type"] != "movie":
        return {"version": VERSION, "verified": False, "reason": "TMDB token unavailable or not a movie"}

    def get(endpoint, **params):
        request = Request("https://api.themoviedb.org/3/" + endpoint + "?" + urlencode(params),
                          headers={"Authorization": "Bearer " + token})
        with open_with_retry(request, opener=urlopen, timeout=15) as response:
            return json.loads(response.read(2_000_000))

    def normalized(value):
        return re.sub(r"[^\w]", "", str(value).casefold())

    try:
        # Stable IDs embedded in file names take precedence over fuzzy guesses.
        path = row["rel_path"]
        tmdb = re.search(r"tmdb[=-](\d+)", path, re.I)
        imdb = re.search(r"\btt\d{7,10}\b", path)
        if tmdb:
            movie_id = int(tmdb.group(1))
        else:
            if imdb:
                matches = get("find/" + imdb.group(), external_source="imdb_id").get("movie_results", [])
            else:
                matches = get("search/movie", query=row["title"], year=row["year"] or "").get("results", [])
                matches = [m for m in matches if row["year"] and str(m.get("release_date", ""))[:4] == str(row["year"])
                           and normalized(row["title"]) in {normalized(m.get("title")), normalized(m.get("original_title"))}]
            if len(matches) != 1:
                return {"version": VERSION, "verified": False, "reason": "Ambiguous metadata identity"}
            movie_id = int(matches[0]["id"])
        movie = get(f"movie/{movie_id}", append_to_response="release_dates")
        dates = {item["release_date"][:10] for group in movie.get("release_dates", {}).get("results", [])
                 if group.get("iso_3166_1") == region.upper() for item in group.get("release_dates", [])
                 if item.get("type") == 4 and item.get("release_date")}
        release = next(iter(dates)) if len(dates) == 1 else None
        return {"version": VERSION, "verified": True, "source": "TMDB", "tmdb_id": movie_id,
                "imdb_id": movie.get("imdb_id"), "original_language": movie.get("original_language"),
                "animation_genre": any(g.get("id") == 16 for g in movie.get("genres", [])),
                "digital_release_date": release, "release_region": region,
                "subtitle_eligible_date": (date.fromisoformat(release) + timedelta(days=7)).isoformat() if release else None,
                "release_conflict": len(dates) > 1}
    except ProviderDeferred as exc:
        return {"version": VERSION, "verified": False, "reason": str(exc), "next_eligible_attempt": exc.retry_at}
    except Exception:
        # Never persist request headers, credentials or raw provider errors.
        return {"version": VERSION, "verified": False, "reason": "Metadata lookup unavailable"}


def reconcile_visual(visual, confidence, metadata):
    from .models import VisualType
    if not metadata.get("verified"):
        return visual, confidence
    if metadata.get("animation_genre"):
        if visual == VisualType.ANIMATION and confidence >= 0.8:
            return visual, confidence
        return VisualType.MIXED if visual == VisualType.LIVE_ACTION else VisualType.UNKNOWN, confidence
    if visual == VisualType.ANIMATION:
        return VisualType.UNKNOWN, confidence
    return visual, confidence
