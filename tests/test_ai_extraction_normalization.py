import pytest

from ai.category_schemas import validate_result
from ai.extractor import _normalize_selected_category_data


@pytest.mark.parametrize("category", ["housinglist", "transferlist", "joblist"])
def test_singleton_category_list_is_unwrapped(category):
    item = {"title": "Example"}
    result = {"category": category, "data": {category: [item]}}

    normalized = _normalize_selected_category_data(result)

    assert normalized["data"][category] is item
    assert validate_result(normalized) == (category, item)


def test_category_dict_is_unchanged():
    item = {"title": "Example"}
    result = {"category": "housinglist", "data": {"housinglist": item}}

    normalized = _normalize_selected_category_data(result)

    assert normalized["data"]["housinglist"] is item
    assert validate_result(normalized) == ("housinglist", item)


@pytest.mark.parametrize(
    "category_data",
    [[], [{"title": "one"}, {"title": "two"}], ["invalid"], None, "text", 123],
)
def test_malformed_category_data_is_not_silently_accepted(category_data):
    result = {"category": "housinglist", "data": {"housinglist": category_data}}

    normalized = _normalize_selected_category_data(result)

    with pytest.raises(ValueError, match=r"AI result data\.housinglist must be an object"):
        validate_result(normalized)


def test_invalid_category_is_not_normalized_and_validation_error_is_preserved():
    result = {
        "category": "unknown",
        "data": {"unknown": [{"title": "Example"}]},
    }

    normalized = _normalize_selected_category_data(result)

    assert normalized is result
    with pytest.raises(ValueError, match=r"Unsupported category: 'unknown'"):
        validate_result(normalized)


def test_non_dict_data_is_not_normalized_and_validation_error_is_preserved():
    result = {"category": "housinglist", "data": [{"title": "Example"}]}

    normalized = _normalize_selected_category_data(result)

    assert normalized is result
    with pytest.raises(ValueError, match="AI result data must be an object"):
        validate_result(normalized)
