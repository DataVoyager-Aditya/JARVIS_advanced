"""Web tools — search (Tavily, rotated keys) and weather (open-meteo, free, no key)."""

from __future__ import annotations

import logging

import httpx

from config import load_key_pool
from app.tools import tool

logger = logging.getLogger("jarvis.tools.web")

_TAVILY_KEYS = load_key_pool("TAVILY_API_KEY")


@tool(
    "Search the web for current, real-time information (news, facts, prices, people, events). "
    "Use whenever the answer needs up-to-date or external knowledge.",
    params={"query": {"type": "string", "description": "the search query"}},
    required=["query"],
    narration="Searching the web",
)
def web_search(query: str) -> str:
    if not _TAVILY_KEYS:
        return "[web_search unavailable: no TAVILY_API_KEY in .env]"
    last = None
    for key in _TAVILY_KEYS:
        try:
            r = httpx.post("https://api.tavily.com/search", timeout=12, json={
                "api_key": key, "query": query, "max_results": 3,
                "include_answer": "basic", "search_depth": "basic",
            })
            r.raise_for_status()
            data = r.json()
            # Tavily's own one-shot answer is usually enough — fastest path. Add a couple of
            # short sources so JARVIS can be specific without re-reading long pages.
            out = []
            if data.get("answer"):
                out.append(data["answer"])
            for res in data.get("results", [])[:3]:
                out.append(f"- {res.get('title','')}: {res.get('content','')[:140]}")
            return "\n".join(out) or "No results."
        except Exception as e:  # noqa: BLE001
            last = e
            logger.warning("tavily key failed (%s) — rotating", type(e).__name__)
    return f"[web_search failed: {last}]"


@tool(
    "Get the current weather for a place. Returns temperature and conditions.",
    params={"location": {"type": "string", "description": "city or place name, e.g. 'Bangalore'"}},
    required=["location"],
    narration="Checking the weather",
)
def get_weather(location: str) -> str:
    try:
        g = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                      params={"name": location, "count": 1}, timeout=15).json()
        if not g.get("results"):
            return f"Couldn't find a place called '{location}'."
        loc = g["results"][0]
        lat, lon = loc["latitude"], loc["longitude"]
        name = f"{loc['name']}, {loc.get('country', '')}".strip(", ")
        w = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        }).json()["current"]
        return (f"{name}: {w['temperature_2m']}°C (feels {w['apparent_temperature']}°C), "
                f"{_WMO.get(w['weather_code'], 'unknown')}, humidity {w['relative_humidity_2m']}%, "
                f"wind {w['wind_speed_10m']} km/h.")
    except Exception as e:  # noqa: BLE001
        return f"[weather lookup failed: {e}]"


# open-meteo WMO weather codes -> words
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with hail",
}
