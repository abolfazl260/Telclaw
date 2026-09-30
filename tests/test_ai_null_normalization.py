"""Regression tests for AI null-like sentinel normalization."""

import json

from ai.extractor import _normalize_null_like_values
from ai.providers.cloudflare import CloudflareProvider
from ai.providers.groq import GroqClient


class FakeRateLimiter:
    def wait(self):
        return None

    def slot(self):
        class Slot:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        return Slot()


class FakeResponse:
    ok = True
    status_code = 200
    text = ""
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _transfer_payload():
    return {
        "category": "transferlist",
        "data": {
            "transferlist": {
                "title": "Small package delivery",
                "description": "Need someone to carry a small package.",
                "origin_city": " null ",
                "origin_province": "NONE",
                "origin_country": "N/A",
                "destination_city": "Tehran",
                "destination_province": "unknown",
                "destination_country": "IR",
                "airline": "not provided",
                "flight_number": "-",
                "departure_date": "null",
                "departure_time": "NA",
                "arrival_date": None,
                "arrival_time": "",
                "cargo_type": "package",
                "weight": 4.5,
                "weight_unit": "kg",
                "quantity": None,
                "volume": None,
                "volume_unit": None,
                "price": None,
                "currency": None,
                "contact": "none",
                "features": ["fragile", "null", "  keep dry  "],
            }
        },
    }


def test_null_like_values_become_none_and_meaningful_values_are_preserved():
    result = _normalize_null_like_values(_transfer_payload())
    data = result["data"]["transferlist"]

    assert data["origin_city"] is None
    assert data["origin_province"] is None
    assert data["origin_country"] is None
    assert data["destination_city"] == "Tehran"
    assert data["destination_country"] == "IR"
    assert data["airline"] is None
    assert data["flight_number"] is None
    assert data["departure_date"] is None
    assert data["departure_time"] is None
    assert data["arrival_date"] is None
    assert data["arrival_time"] is None
    assert data["contact"] is None
    assert data["features"] == ["fragile", None, "keep dry"]


def test_groq_extraction_returns_real_none_for_null_like_strings(monkeypatch):
    payload = {
        "choices": [{
            "message": {
                "content": json.dumps(_transfer_payload()),
            }
        }]
    }
    monkeypatch.setattr(
        "ai.providers.groq.requests.post",
        lambda *args, **kwargs: FakeResponse(payload),
    )
    client = GroqClient(
        api_key="test-key",
        model="test-model",
        rate_limiter=FakeRateLimiter(),
    )

    result = client.extract("I have a small package to Tehran", category="transferlist")
    data = result["data"]["transferlist"]

    assert data["origin_city"] is None
    assert data["departure_date"] is None
    assert data["contact"] is None
    assert data["destination_city"] == "Tehran"


def test_cloudflare_extraction_returns_real_none_for_null_like_strings(monkeypatch):
    provider = CloudflareProvider(
        providers=[{
            "account_id": "account",
            "api_token": "token",
            "model": "model",
        }],
        rate_limiter=FakeRateLimiter(),
    )
    monkeypatch.setattr(provider, "_request", lambda _messages: _transfer_payload())

    result = provider.extract("I have a small package to Tehran", category="transferlist")
    data = result["data"]["transferlist"]

    assert data["origin_city"] is None
    assert data["departure_date"] is None
    assert data["contact"] is None
    assert data["destination_city"] == "Tehran"
