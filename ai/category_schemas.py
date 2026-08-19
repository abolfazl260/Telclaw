"""Schemas and field allow-lists for AI category extraction."""

CATEGORIES = ("housinglist", "transferlist", "joblist")

CATEGORY_FIELDS = {
    "housinglist": (
        "property_type", "listing_type", "title", "description", "location",
        "price", "currency", "rent_period", "bedrooms", "bathrooms", "area",
        "area_unit", "furnished", "availability", "property_condition",
        "contact", "features",
    ),
    "transferlist": (
        "vehicle_type", "brand", "model", "trim", "year", "mileage",
        "mileage_unit", "price", "currency", "location", "transmission",
        "fuel_type", "condition", "engine", "color", "contact", "features",
    ),
    "joblist": (
        "job_title", "company", "location", "employment_type", "salary",
        "salary_currency", "salary_period", "experience", "education",
        "skills", "remote", "job_type", "description", "application_method",
        "contact",
    ),
}


def build_json_schema():
    """Build a strict top-level schema while allowing category-specific values."""
    category_enum = list(CATEGORIES)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "data"],
        "properties": {
            "category": {"type": "string", "enum": category_enum},
            "data": {
                "type": "object",
                "additionalProperties": True,
            },
        },
    }


def validate_result(result):
    if not isinstance(result, dict):
        raise ValueError("AI result must be an object")
    category = result.get("category")
    if category not in CATEGORIES:
        raise ValueError(f"Unsupported category: {category!r}")
    data = result.get("data")
    if not isinstance(data, dict):
        raise ValueError("AI result data must be an object")
    allowed = set(CATEGORY_FIELDS[category])
    return category, {key: value for key, value in data.items() if key in allowed}
