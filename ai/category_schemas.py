"""Schemas and field allow-lists for AI category extraction."""

CATEGORIES = ("housinglist", "transferlist", "joblist")
CLASSIFICATION_CATEGORIES = (*CATEGORIES, "none")

CATEGORY_FIELDS = {
    "housinglist": (
        "property_type", "listing_type", "title", "description", "location",
        "country_code", "province", "city", "neighborhood", "price", "currency",
        "rent_period", "bedrooms", "bathrooms", "area", "area_unit", "furnished",
        "availability", "property_condition", "contact", "features",
    ),
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

_CANADIAN_CITY_PROVINCE = {
    "toronto": "Ontario", "mississauga": "Ontario", "brampton": "Ontario",
    "markham": "Ontario", "vaughan": "Ontario", "richmond hill": "Ontario",
    "ottawa": "Ontario", "hamilton": "Ontario", "london": "Ontario",
    "waterloo": "Ontario", "kitchener": "Ontario", "windsor": "Ontario",
    "vancouver": "British Columbia", "burnaby": "British Columbia",
    "richmond": "British Columbia", "surrey": "British Columbia",
    "coquitlam": "British Columbia", "kelowna": "British Columbia",
    "victoria": "British Columbia", "calgary": "Alberta", "edmonton": "Alberta",
    "red deer": "Alberta", "montreal": "Quebec", "laval": "Quebec",
    "quebec city": "Quebec", "gatineau": "Quebec", "winnipeg": "Manitoba",
    "halifax": "Nova Scotia", "saskatoon": "Saskatchewan", "regina": "Saskatchewan",
    "st. john's": "Newfoundland and Labrador", "st john's": "Newfoundland and Labrador",
    "fredericton": "New Brunswick", "moncton": "New Brunswick",
    "charlottetown": "Prince Edward Island", "yellowknife": "Northwest Territories",
    "whitehorse": "Yukon", "iqaluit": "Nunavut",
}

_CANADIAN_NEIGHBORHOODS = {
    "yonge": ("Toronto", "Ontario"), "eglinton": ("Toronto", "Ontario"),
    "yonge and eglinton": ("Toronto", "Ontario"), "midtown": ("Toronto", "Ontario"),
    "downtown toronto": ("Toronto", "Ontario"), "north york": ("Toronto", "Ontario"),
    "scarborough": ("Toronto", "Ontario"), "etobicoke": ("Toronto", "Ontario"),
    "leslieville": ("Toronto", "Ontario"), "liberty village": ("Toronto", "Ontario"),
    "the annex": ("Toronto", "Ontario"), "kensingston market": ("Toronto", "Ontario"),
    "downtown vancouver": ("Vancouver", "British Columbia"), "kitsilano": ("Vancouver", "British Columbia"),
    "yaletown": ("Vancouver", "British Columbia"), "gastown": ("Vancouver", "British Columbia"),
    "burnaby": ("Burnaby", "British Columbia"), "richmond bc": ("Richmond", "British Columbia"),
}


def _infer_housing_location(data):
    """Canonicalize housing location using Canada as the safe default."""
    if not isinstance(data, dict):
        return data
    city = str(data.get("city") or "").strip()
    province = str(data.get("province") or "").strip()
    country = str(data.get("country_code") or "").strip().upper()
    neighborhood = str(data.get("neighborhood") or "").strip().lower()
    location = str(data.get("location") or "").strip()

    if not country:
        country = "CA"
    elif country != "CA":
        country = "CA"

    city_key = city.lower()
    if city_key in _CANADIAN_CITY_PROVINCE:
        province = province or _CANADIAN_CITY_PROVINCE[city_key]

    if location:
        location_lower = location.lower()
        if not city:
            for known_city in sorted(_CANADIAN_CITY_PROVINCE, key=len, reverse=True):
                if known_city in location_lower:
                    city = known_city.title()
                    province = province or _CANADIAN_CITY_PROVINCE[known_city]
                    break
        if not province:
            province_aliases = {
                "on": "Ontario", "ontario": "Ontario", "bc": "British Columbia",
                "british columbia": "British Columbia", "ab": "Alberta", "alberta": "Alberta",
                "qc": "Quebec", "quebec": "Quebec", "mb": "Manitoba", "manitoba": "Manitoba",
                "ns": "Nova Scotia", "nova scotia": "Nova Scotia", "sk": "Saskatchewan",
                "saskatchewan": "Saskatchewan", "nb": "New Brunswick", "new brunswick": "New Brunswick",
                "nl": "Newfoundland and Labrador", "newfoundland and labrador": "Newfoundland and Labrador",
                "pei": "Prince Edward Island", "prince edward island": "Prince Edward Island",
                "nt": "Northwest Territories", "northwest territories": "Northwest Territories",
                "yt": "Yukon", "yukon": "Yukon", "nu": "Nunavut", "nunavut": "Nunavut",
            }
            for alias, canonical in sorted(province_aliases.items(), key=lambda item: len(item[0]), reverse=True):
                if alias in location_lower:
                    province = canonical
                    break

    if (not city or not province) and neighborhood:
        for name, (mapped_city, mapped_province) in _CANADIAN_NEIGHBORHOODS.items():
            if name == neighborhood or name in neighborhood:
                city = city or mapped_city
                province = province or mapped_province
                break

    data["country_code"] = country
    if province:
        data["province"] = province
    if city:
        data["city"] = city
    return data


def _field_schema(field):
    if field in {"features", "skills"}:
        return {"type": ["array", "null"], "items": {"type": "string"}}
    if field in {"price", "bedrooms", "bathrooms", "area", "year", "mileage", "weight", "quantity"}:
        return {"type": ["number", "string", "null"]}
    if field == "remote":
        return {"type": ["boolean", "string", "null"]}
    return {"type": ["string", "number", "boolean", "null"]}


def build_json_schema():
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
    normalized = {key: value for key, value in data.items() if key in allowed}
    if category == "housinglist":
        normalized = _infer_housing_location(normalized)
    return category, normalized
