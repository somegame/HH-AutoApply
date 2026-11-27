import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.commute import Commutes, commute_estimate, point

HOME = {"lat": 55.75, "lon": 37.60, "address": "Синтетический адрес"}
OFFICE = {"lat": 55.80, "lon": 37.60}


@pytest.mark.parametrize(
    "candidate",
    [
        [55, 37],
        {"lat": 55},
        {"lat": True, "lon": 37},
        {"lat": 55, "lon": False},
        {"lat": -91, "lon": 37},
        {"lat": 55, "lon": 181},
        {"lat": 55, "lon": "nan"},
        {"lat": float("inf"), "lon": 37},
        {"lat": 10**1000, "lon": 37},
        {"lat": 55.75, "lon": "37,60"},
    ],
)
def test_inaccurate_or_unexpected_coordinate_input_is_rejected(candidate):
    assert point(candidate) is None


def test_coordinates_only_and_estimates_distinguish_remote_from_unknown():
    assert point(HOME) == {"lat": 55.75, "lon": 37.60}
    assert point({"lat": " -55.75 ", "lon": "+.250"}) == {"lat": -55.75, "lon": 0.25}
    assert commute_estimate(HOME, None, "car") is None
    assert commute_estimate(HOME, OFFICE, "unsupported") is None
    remote = commute_estimate(None, None, "transit", remote=True)
    assert remote["kind"] == "remote"
    assert remote["min_minutes"] == remote["minutes"] == remote["max_minutes"] == 0

    walking, driving, transit = [
        commute_estimate(HOME, OFFICE, m) for m in ("walk", "car", "transit")
    ]
    for result in (walking, driving, transit):
        assert result["kind"] == "estimate"
        assert result["distance_kind"] == "straight"
        assert result["distance_km"] == pytest.approx(5.56)
        assert 0 < result["min_minutes"] <= result["minutes"] <= result["max_minutes"]
    assert walking["minutes"] > transit["minutes"] > driving["minutes"]


def test_provider_only_receives_explicit_request_data_and_supports_offline_fallback():
    finished = []

    def respond(request):
        assert request.url.host == "api.heigit.org"
        assert request.headers["authorization"] == "test-placeholder"
        assert "api_key" not in request.url.params
        finished.append(request.url.path)
        if request.url.path == "/openrouteservice/v2/directions/driving-car/json":
            assert request.method == "POST"
            assert not request.url.query
            assert json.loads(request.content) == {
                "coordinates": [[37.60, 55.75], [37.60, 55.80]],
                "geometry": False,
                "instructions": False,
            }
            return httpx.Response(
                200, json={"routes": [{"summary": {"duration": 1150, "distance": 6725}}]}
            )
        assert request.url.path == "/pelias/v1/search"
        assert request.method == "GET"
        assert request.url.params == httpx.QueryParams(
            {"text": "Москва, общественная точка", "size": 1}
        )
        return httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {"coordinates": [37.61, 55.71]},
                        "properties": {"label": "Москва, общественная точка"},
                    }
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            disabled = Commutes(SimpleNamespace(), client)
            assert not disabled.available
            assert await disabled.route(HOME, OFFICE, "car") is None
            assert await disabled.locate("Москва") is None
            enabled = Commutes(SimpleNamespace(ors_key="test-placeholder"), client)
            assert enabled.available
            assert await enabled.route(HOME, OFFICE, "transit") is None
            assert await enabled.route(None, OFFICE, "walk") is None
            assert await enabled.locate(" ") is None
            assert not finished
            result = await enabled.route(HOME, OFFICE, "car")
            assert result == {
                "minutes": 20,
                "min_minutes": 20,
                "max_minutes": 20,
                "kind": "routed",
                "distance_km": 6.72,
                "distance_kind": "route",
                "mode": "car",
            }
            assert await enabled.locate("  Москва, общественная точка  ") == {
                "lat": 55.71,
                "lon": 37.61,
                "label": "Москва, общественная точка",
            }

    asyncio.run(run())
    assert len(finished) == 2


def test_provider_errors_and_invalid_results_are_unknown_not_zero_minutes():
    def respond(request):
        path = request.url.path
        if path.endswith("foot-walking/json"):
            return httpx.Response(429)
        if path.endswith("cycling-regular/json"):
            return httpx.Response(
                200, json={"routes": [{"summary": {"duration": False, "distance": 0}}]}
            )
        if path.endswith("driving-car/json"):
            raise httpx.ReadTimeout("Synthetic timeout")
        return httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {"coordinates": [370.61, 55.71]},
                        "properties": {"label": "x"},
                    }
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = Commutes(SimpleNamespace(ors_key="test-placeholder"), client)
            assert await provider.route(HOME, OFFICE, "walk") is None
            assert await provider.route(HOME, OFFICE, "cycle") is None
            assert await provider.route(HOME, OFFICE, "car") is None
            assert await provider.locate("Синтетический адрес") is None
        assert commute_estimate(HOME, OFFICE, "walk")["kind"] == "estimate"

    asyncio.run(run())


def test_precise_office_lookup_never_uses_the_city_center():
    def respond(request):
        assert request.url.params["layers"] == "address,venue"
        layer = "locality" if request.url.params["text"] == "Москва" else "address"
        return httpx.Response(
            200,
            json={
                "features": [
                    {
                        "geometry": {"coordinates": [37.62, 55.75]},
                        "properties": {"label": "Синтетическая точка", "layer": layer},
                    }
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = Commutes(SimpleNamespace(ors_key="test-placeholder"), client)
            assert await provider.locate("Москва", precise=True) is None
            assert (await provider.locate("Москва, улица Тестовая, 1", precise=True))[
                "lat"
            ] == 55.75

    asyncio.run(run())
