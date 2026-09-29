import json

from delivery.advertio_service import AdvertioDeliveryService, AdvertioMappingError


def _service():
    class FakeClient:
        pass
    return AdvertioDeliveryService(client=FakeClient())


def _record():
    return {
        "id": 1,
        "message_id": 101,
        "channel_username": "test",
        "message_link": "https://t.me/test/101",
        "sender_username": "tester",
    }


def _housing(**overrides):
    data = {
        "listing_type": "rent",
        "property_type": "apartment",
        "title": "2 Bedroom Apartment in Toronto",
        "description": "Apartment for rent",
        "country_code": "CA",
        "province": "Ontario",
        "city": "Toronto",
        "bedrooms": "2",
        "price": 2100,
        "currency": "CAD",
    }
    data.update(overrides)
    return data


def test_attributes_json_is_a_json_string_containing_an_object():
    payload = _service().build_payload(_record(), _housing(amenities=["parking", "balcony"]))
    assert isinstance(payload["attributesJson"], str)
    parsed = json.loads(payload["attributesJson"])
    assert isinstance(parsed, dict)
    assert parsed["listing_type"] == "rent"
    assert parsed["property_type"] == "apartment"
    assert parsed["bedrooms"] == "2"
    assert parsed["price"] == 2100
    assert parsed["amenities"] == ["balcony", "parking"]


def test_bedrooms_are_advertio_strings_not_numbers():
    payload = _service().build_payload(_record(), _housing(bedrooms=2))
    assert json.loads(payload["attributesJson"])["bedrooms"] == "2"


def test_required_housing_fields_are_rejected_when_missing():
    for field in ("listing_type", "bedrooms", "price", "currency", "province", "city"):
        data = _housing()
        data[field] = None
        try:
            _service().build_payload(_record(), data)
        except AdvertioMappingError as exc:
            assert field in str(exc)
        else:
            raise AssertionError(f"missing {field} should be rejected")


def test_invalid_optional_enum_is_not_sent():
    payload = _service().build_payload(_record(), _housing(amenities=["parking", "not-an-advertio-value"]))
    assert json.loads(payload["attributesJson"])["amenities"] == ["parking"]


def test_roommate_age_range_and_gender_use_documented_shapes():
    payload = _service().build_payload(
        _record(),
        _housing(listing_type="roommate", gender_preference="female", age_range=[20, 35]),
    )
    attrs = json.loads(payload["attributesJson"])
    assert attrs["gender_preference"] == "female"
    assert attrs["age_range"] == [20, 35]


def test_canonical_rent_period_maps_to_advertio_rental_duration():
    short_payload = _service().build_payload(_record(), _housing(rent_period="short_term"))
    long_payload = _service().build_payload(_record(), _housing(rent_period="long_term"))
    daily_payload = _service().build_payload(_record(), _housing(rent_period="daily"))

    assert json.loads(short_payload["attributesJson"])["rental_duration"] == "short_term"
    assert json.loads(long_payload["attributesJson"])["rental_duration"] == "long_term"
    assert json.loads(daily_payload["attributesJson"])["rental_duration"] == "daily"


def test_advertio_accepts_all_canonical_bedroom_values():
    for bedrooms in ("0", "1", "2", "3", "4+"):
        payload = _service().build_payload(_record(), _housing(bedrooms=bedrooms))
        assert json.loads(payload["attributesJson"])["bedrooms"] == bedrooms


def test_normalized_bathroom_count_reaches_advertio_payload():
    payload = _service().build_payload(_record(), _housing(bathrooms=3))
    assert json.loads(payload["attributesJson"])["bathrooms_count"] == 3


def test_advertio_converts_square_feet_to_square_metres():
    payload = _service().build_payload(
        _record(), _housing(area=1000, area_unit="Sq.Ft")
    )
    assert json.loads(payload["attributesJson"])["area"] == 92.9


def test_advertio_omits_area_when_unit_is_unknown():
    payload = _service().build_payload(
        _record(), _housing(area=100, area_unit=None)
    )
    assert "area" not in json.loads(payload["attributesJson"])


def test_advertio_accepts_canonical_square_metre_area():
    payload = _service().build_payload(
        _record(), _housing(area=100, area_unit="sqm")
    )
    assert json.loads(payload["attributesJson"])["area"] == 100
