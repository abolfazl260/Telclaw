"""Schemas and field allow-lists for AI category extraction."""

CATEGORIES = ("housinglist", "transferlist", "joblist")

CATEGORY_FIELDS = {
    "housinglist": (
        "property_type", "listing_type", "title", "description", "location",
        "country_code", "province", "city", "neighborhood", "price", "currency",
        "rent_period", "bedrooms", "bathrooms", "area", "area_unit", "furnished",
        "availability", "property_condition", "contact", "features",
    ),
    # transferlist is used for air-transport / cargo transfer requests.
    "transferlist": (
        "title", "description", "origin_city", "origin_province", "origin_country",
        "destination_city", "destination_province", "destination_country", "airline",
        "flight_number", "departure_date", "departure_time", "arrival_date", "arrival_time",
        "transport_type", "cargo_type", "weight", "weight_unit", "quantity",
        "price", "currency", "contact", "features",
    ),
    "joblist": (
        "job_title", "company", "location", "employment_type", "salary",
        "salary_currency", "salary_period", "experience", "education",
        "skills", "remote", "job_type", "description", "application_method",
        "contact",
    ),
}


def _field_schema(field):
    """Use a nullable JSON value so the model can explicitly mark unknown data."""
    if field in {"features", "skills"}:
        return {"type": ["array", "null"], "items": {"type": "string"}}
    if field in {"price", "bedrooms", "bathrooms", "area", "year", "mileage", "weight", "quantity"}:
        return {"type": ["number", "string", "null"]}
    if field == "remote":
        return {"type": ["boolean", "string", "null"]}
    return {"type": ["string", "number", "boolean", "null"]}


def build_json_schema():
    """Build a strict schema compatible with Groq JSON Schema structured outputs."""
    category_data = {}
    for category, fields in CATEGORY_FIELDS.items():
        category_data[category] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {field: _field_schema(field) for field in fields},
            "required": list(fields),
        }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "data": {
                "type": "object",
                "additionalProperties": False,
                "properties": {category: {**category_data[category]} for category in CATEGORIES},
                "required": list(CATEGORIES),
            },
        },
        "required": ["category", "data"],
    }


def validate_result(result):
    if not isinstance(result, dict):
        raise ValueError("AI result must be an object")
    category = result.get("category")
    if category not in CATEGORIES:
        raise ValueError(f"Unsupported category: {category!r}")
    data_container = result.get("data")
    if not isinstance(data_container, dict):
        raise ValueError("AI result data must be an object")

    data = data_container.get(category)
    if not isinstance(data, dict):
        raise ValueError(f"AI result data.{category} must be an object")

    allowed = set(CATEGORY_FIELDS[category])
    return category, {key: value for key, value in data.items() if key in allowed}
