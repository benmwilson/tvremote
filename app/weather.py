"""Current weather for the TV page, from Open-Meteo (free, no key). The place is looked up once when an admin saves a city, then remembered in the settings.
Only this app talks to Open-Meteo; the TV's browser gets the finished numbers."""
import json
import time
import urllib.parse
import urllib.request

_cache = {}  # (lat, lon, unit) -> (time, data)
TTL = 600


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tvremote"}), timeout=8) as r:
        return json.load(r)


def search(text, count=6):
    """Up to `count` places matching what was typed, best first: [{"name", "lat", "lon"}] ([] when nothing matches or Open-Meteo can't be reached)."""
    text = (text or "").strip()[:80]
    if len(text) < 2:
        return []
    try:
        rows = _get("https://geocoding-api.open-meteo.com/v1/search?language=en&count=%d&name=%s" % (count, urllib.parse.quote(text))).get("results") or []
    except (OSError, ValueError):
        return []
    out = []
    for r in rows:
        try:
            out.append({"name": ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country_code")) if x),
                        "lat": round(float(r["latitude"]), 3), "lon": round(float(r["longitude"]), 3)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def geocode(city):
    """{"name", "lat", "lon"} for a city name (the best match), or None when it is not found or Open-Meteo can't be reached."""
    return (search(city, 1) or [None])[0]


def current(place, unit="C"):
    """{"temp", "hi", "lo", "code", "day", "unit", "place", "offset"} for a saved place (a stale answer when Open-Meteo is down), or None."""
    if not place:
        return None
    unit = "F" if unit == "F" else "C"
    key = (place["lat"], place["lon"], unit)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    try:
        j = _get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto", "forecast_days": 1,
            "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
            "current": "temperature_2m,weather_code,is_day", "daily": "temperature_2m_max,temperature_2m_min"}))
        c, d = j["current"], j["daily"]
        out = {"temp": round(c["temperature_2m"]), "code": c["weather_code"], "day": bool(c.get("is_day", 1)), "hi": round(d["temperature_2m_max"][0]),
               "lo": round(d["temperature_2m_min"][0]), "unit": unit, "place": place["name"], "offset": j.get("utc_offset_seconds", 0)}
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return hit[1] if hit else None
    _cache[key] = (time.time(), out)
    return out
