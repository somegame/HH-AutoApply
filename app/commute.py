import math
import re
from collections.abc import Mapping

import httpx

HEIGIT_API = "https://api.heigit.org"
ROUTING_PROFILES = {"car": "driving-car", "walk": "foot-walking", "cycle": "cycling-regular"}
# For each mode: street/straight distance ratio, low/high speed (km/h), low/high fixed minutes.
ESTIMATION_MODES = {
    "transit": (1.35, 14, 28, 6, 14),
    "car": (1.25, 16, 44, 2, 8),
    "walk": (1.20, 3.5, 5.5, 0, 0),
    "cycle": (1.25, 11, 20, 1, 4),
}
DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def _number(value):
    if type(value) not in (int, float) and (
        not isinstance(value, str) or len(value) > 32 or not DECIMAL.fullmatch(value.strip())
    ):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def point(value):
    """Validate geographic coordinates without copying unrelated data."""
    if not isinstance(value, Mapping):
        return None
    lat, lon = _number(value.get("lat")), _number(value.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return {"lat": lat, "lon": lon}


def _straight_distance_km(origin, destination):
    lat1, lat2 = math.radians(origin["lat"]), math.radians(destination["lat"])
    delta_lon = math.radians(destination["lon"] - origin["lon"])
    chord = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1, max(0, chord))))


def commute_estimate(origin, destination, mode, remote=False):
    """Estimate a range from straight-line distance. The range is independent of schedules."""
    if not isinstance(mode, str) or mode not in ESTIMATION_MODES:
        return None
    if remote:
        return {
            "minutes": 0,
            "min_minutes": 0,
            "max_minutes": 0,
            "kind": "remote",
            "distance_km": 0,
            "mode": mode,
        }
    start, end = point(origin), point(destination)
    if start is None or end is None:
        return None
    distance = _straight_distance_km(start, end)
    ratio, slow, fast, low_fixed, high_fixed = ESTIMATION_MODES[mode]
    lower = math.ceil(distance * ratio / fast * 60 + low_fixed) if distance > 0 else 0
    upper = math.ceil(distance * ratio / slow * 60 + high_fixed) if distance > 0 else 0
    return {
        "minutes": (lower + upper + 1) // 2,
        "min_minutes": lower,
        "max_minutes": upper,
        "kind": "estimate",
        "distance_km": round(distance, 2),
        "distance_kind": "straight",
        "mode": mode,
    }


class Commutes:
    """Optional, explicit calls to HeiGIT. A request failure leaves the estimate available."""

    def __init__(self, config, client):
        self.key = getattr(config, "ors_key", "")
        self.client = client

    @property
    def available(self):
        return bool(self.key)

    async def _request(self, method, path, **kwargs):
        try:
            response = await self.client.request(
                method,
                HEIGIT_API + path,
                headers={"Authorization": self.key},
                timeout=5.0,
                follow_redirects=False,
                **kwargs,
            )
            data = response.json() if response.is_success else None
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def route(self, origin, destination, mode):
        profile = ROUTING_PROFILES.get(mode) if isinstance(mode, str) else None
        start, end = point(origin), point(destination)
        if not self.available or profile is None or start is None or end is None:
            return None
        data = await self._request(
            "POST",
            f"/openrouteservice/v2/directions/{profile}/json",
            json={
                "coordinates": [[start["lon"], start["lat"]], [end["lon"], end["lat"]]],
                "geometry": False,
                "instructions": False,
            },
        )
        try:
            summary = data["routes"][0]["summary"]
            seconds, meters = _number(summary["duration"]), _number(summary["distance"])
        except (KeyError, IndexError, TypeError):
            return None
        if seconds is None or meters is None or seconds < 0 or meters < 0:
            return None
        minutes = math.ceil(seconds / 60)
        return {
            "minutes": minutes,
            "min_minutes": minutes,
            "max_minutes": minutes,
            "kind": "routed",
            "distance_km": round(meters / 1000, 2),
            "distance_kind": "route",
            "mode": mode,
        }

    async def locate(self, address, *, precise=False):
        if (
            not self.available
            or not isinstance(address, str)
            or not 3 <= len(address.strip()) <= 250
        ):
            return None
        params = {"text": address.strip(), "size": 1}
        if precise:
            params["layers"] = "address,venue"
        data = await self._request(
            "GET",
            "/pelias/v1/search",
            params=params,
        )
        try:
            feature = data["features"][0]
            lon, lat = feature["geometry"]["coordinates"][:2]
            match = point({"lat": lat, "lon": lon})
            label = feature["properties"]["label"]
            if precise and feature["properties"].get("layer") not in {"address", "venue"}:
                return None
        except (KeyError, IndexError, ValueError, TypeError):
            return None
        if match is None or not isinstance(label, str) or not label.strip():
            return None
        return match | {"label": " ".join(label.split())[:250]}
